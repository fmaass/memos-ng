"""Guest memos name their uploader and stay out of the home timeline (MEMNG-1164).

A guest batch becomes a PROTECTED memo owned by the pass issuer (`passes.create_guest_memo`), so
without a marker every reader lists it as the owner's own. `MemoOut.guest` carries who sent it
(`contributor_name`, copied onto the attachments) and through which pass (`pass_uid`); it is null
for every member memo and for a guest memo whose pass record is gone (the FK is SET NULL).
`GET /api/memos` leaves guest memos out unless `?guest=include`; the export keeps them.
"""

import io
import json
import zipfile

from app.database import async_session
from app.models import Collection, User
from sqlalchemy import select

from tests.test_attachments_taken import _sql
from tests.test_collections_api import _att
from tests.test_passes_api import _trip, lib  # noqa: F401  (`lib` is a fixture)


async def _guest(
    coll_uid: str, subject: str, att_uids: list[str], *, name: str | None, key: str = "trip:g1"
) -> tuple[str, str]:
    """A guest memo built by the real boundary, carrying the given ready attachments.
    Returns (memo uid, pass uid)."""
    from app.services import passes as pass_svc

    async def finalize(_memo) -> list[str]:
        return att_uids

    async with async_session() as db:
        owner = (await db.execute(select(User).where(User.subject == subject))).scalar_one()
        collection = (
            await db.execute(select(Collection).where(Collection.uid == coll_uid))
        ).scalar_one()
        pass_row, _ = await pass_svc.issue_pass(db, collection, owner, label="g")
        memo = await pass_svc.create_guest_memo(
            db,
            pass_row,
            caption="from the beach",
            contributor_name=name,
            idempotency_key=key,
            finalize=finalize,
        )
        await db.commit()
        return memo.uid, pass_row.uid


def _member(client) -> str:
    r = client.post("/api/memos", json={"content": "member memo", "visibility": "PROTECTED"})
    assert r.status_code == 201, r.text
    return r.json()["uid"]


def _listed(client, query: str = "") -> dict[str, dict]:
    r = client.get(f"/api/memos?limit=100{query}")
    assert r.status_code == 200, r.text
    return {m["uid"]: m for m in r.json()["items"]}


async def test_a_guest_memo_is_out_of_the_default_list_and_back_with_guest_include(lib):  # noqa: F811
    client, me = lib
    _att("g1a", me)
    guest_uid, _ = await _guest(_trip(client), me, ["g1a"], name="Anna")
    member_uid = _member(client)

    default = _listed(client)
    assert member_uid in default
    assert guest_uid not in default

    excluded = _listed(client, "&guest=exclude")
    assert member_uid in excluded
    assert guest_uid not in excluded

    included = _listed(client, "&guest=include")
    assert member_uid in included
    assert guest_uid in included


async def test_guest_names_the_contributor_and_the_pass_on_list_and_get(lib):  # noqa: F811
    client, me = lib
    _att("g2a", me)
    _att("g2b", me)
    guest_uid, pass_uid = await _guest(_trip(client), me, ["g2a", "g2b"], name="Anna")
    member_uid = _member(client)

    listed = _listed(client, "&guest=include")
    assert listed[guest_uid]["guest"] == {"contributor_name": "Anna", "pass_uid": pass_uid}
    assert listed[member_uid]["guest"] is None

    one = client.get(f"/api/memos/{guest_uid}")
    assert one.status_code == 200, one.text
    assert one.json()["guest"] == {"contributor_name": "Anna", "pass_uid": pass_uid}
    other = client.get(f"/api/memos/{member_uid}")
    assert other.status_code == 200, other.text
    assert other.json()["guest"] is None


async def test_the_contributor_is_the_first_named_attachment_and_may_be_absent(lib):  # noqa: F811
    client, me = lib
    coll = _trip(client)
    _att("g3a", me)
    _att("g3b", me)
    named_uid, _ = await _guest(coll, me, ["g3a", "g3b"], name="Bea", key="trip:g3")
    # The first attachment (in the memo's order) lost its name; the next one still names the guest.
    _sql("UPDATE attachments SET contributor_name = NULL WHERE uid = 'g3a'")
    _att("g4a", me)
    anon_uid, anon_pass = await _guest(coll, me, ["g4a"], name=None, key="trip:g4")
    bare_uid, bare_pass = await _guest(coll, me, [], name="Cleo", key="trip:g5")

    listed = _listed(client, "&guest=include")
    assert listed[named_uid]["attachments"][0]["uid"] == "g3a"
    assert listed[named_uid]["guest"]["contributor_name"] == "Bea"
    assert listed[anon_uid]["guest"] == {"contributor_name": None, "pass_uid": anon_pass}
    assert listed[bare_uid]["guest"] == {"contributor_name": None, "pass_uid": bare_pass}


async def test_a_guest_memo_whose_pass_record_is_gone_is_a_plain_memo(lib):  # noqa: F811
    client, me = lib
    _att("g6a", me)
    guest_uid, _ = await _guest(_trip(client), me, ["g6a"], name="Anna")
    for table in ("pass_sync_state", "pass_events", "pass_devices", "passes"):
        _sql(f"DELETE FROM {table}")
    assert _sql("SELECT pass_id FROM memos WHERE uid = :u", {"u": guest_uid})[0][0] is None

    default = _listed(client)
    assert default[guest_uid]["guest"] is None
    assert client.get(f"/api/memos/{guest_uid}").json()["guest"] is None


async def test_an_owner_edit_of_a_guest_memo_still_names_the_guest(lib):  # noqa: F811
    client, me = lib
    _att("g7a", me)
    guest_uid, pass_uid = await _guest(_trip(client), me, ["g7a"], name="Anna")
    r = client.patch(f"/api/memos/{guest_uid}", json={"pinned": True})
    assert r.status_code == 200, r.text
    assert r.json()["guest"] == {"contributor_name": "Anna", "pass_uid": pass_uid}


async def test_the_export_still_contains_the_guest_memo(lib):  # noqa: F811
    client, me = lib
    _att("g8a", me)
    guest_uid, _ = await _guest(_trip(client), me, ["g8a"], name="Anna")
    member_uid = _member(client)

    r = client.get("/api/export")
    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    exported = {m["uid"] for m in json.loads(zf.read("manifest.json"))}
    assert {guest_uid, member_uid} <= exported
    assert f"{guest_uid}.md" in zf.namelist()


def test_an_invalid_guest_value_is_422(lib):  # noqa: F811
    client, _ = lib
    for bad in ("only", "", "INCLUDE", "true"):
        r = client.get(f"/api/memos?guest={bad}")
        assert r.status_code == 422, (bad, r.text)
