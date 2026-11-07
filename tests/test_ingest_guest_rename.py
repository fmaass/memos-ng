"""A joined guest renames its own device: `PATCH /api/guest/device` (MEMNG-1279).

The real `ingest` app over ASGI against the test database, passes pushed through the real
ingest-home interface, exactly as tests/test_ingest_guest.py drives the door. Every success is read
back three ways: the door's own `GET /api/guest/me`, the `pass_devices` row, and the device feed
home pulls (`GET /ingest/devices`), because a rename home never sees is no rename at all.
"""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from app.database import async_session
from sqlalchemy import text

from tests.ingest_support import (
    SERVICE_TOKEN,
    bearer,
    door,  # noqa: F401
    home_iface,  # noqa: F401
    ingest_env,  # noqa: F401
    pass_state,
    push,
    redeem,
    seeded_pass,
)
from tests.test_attachments_taken import _sql
from tests.test_ingest_guest import _waiters

FLAG = "\U0001f1e8\U0001f1ed"  # the Swiss flag: ONE character, two code points
FAMILY = "\U0001f468‍\U0001f469‍\U0001f467"  # ONE character, five code points


async def _rename(client, token: str, name):
    return await client.patch(
        "/api/guest/device", json={"display_name": name}, headers=bearer(token)
    )


def _names() -> dict[str, str]:
    return dict(_sql("SELECT uid, display_name FROM pass_devices"))


def _cursor(uid: str) -> int:
    return _sql(f"SELECT change_seq FROM pass_devices WHERE uid = '{uid}'")[0][0]


async def test_a_guest_renames_itself_and_home_pulls_the_new_name(door, home_iface):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    anna = await redeem(door, secret, "Anna")
    ben = await redeem(door, secret, "Ben")
    before = {uid: _cursor(uid) for uid in (anna["device_id"], ben["device_id"])}

    r = await _rename(door, anna["device_token"], "Anna Marder")

    assert r.status_code == 200, r.text
    assert r.json() == {"display_name": "Anna Marder"}
    me = await door.get("/api/guest/me", headers=bearer(anna["device_token"]))
    assert me.json()["display_name"] == "Anna Marder"
    assert _names() == {anna["device_id"]: "Anna Marder", ben["device_id"]: "Ben"}
    # The rename draws a fresh change cursor, so home's `since=` pull sees the device again ...
    assert _cursor(anna["device_id"]) > max(before.values())
    feed = await home_iface.get("/ingest/devices", params={"since": max(before.values())})
    assert feed.status_code == 200, feed.text
    assert [(d["device_id"], d["display_name"]) for d in feed.json()["devices"]] == [
        (anna["device_id"], "Anna Marder")
    ]
    # ... and the other device on the same pass is left exactly as it was.
    assert _cursor(ben["device_id"]) == before[ben["device_id"]]
    ben_me = await door.get("/api/guest/me", headers=bearer(ben["device_token"]))
    assert ben_me.json()["display_name"] == "Ben"


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("  Zoë \n", "Zoë"),
        ("　Mia ", "Mia"),  # Unicode whitespace is trimmed, not only ASCII
        ("x", "x"),  # one code point: the floor
        ("a" * 64, "a" * 64),  # sixty-four: the ceiling
        ("a" * 62 + FLAG, "a" * 62 + FLAG),  # the ceiling, a two-scalar character across it
        ("a" * 59 + FAMILY, "a" * 59 + FAMILY),  # the ceiling, a five-scalar character across it
        ("  " + "a" * 64 + "  ", "a" * 64),  # the cap counts the trimmed name
    ],
)
async def test_the_name_is_trimmed_and_kept_whole_up_to_64_code_points(
    door,  # noqa: F811
    home_iface,  # noqa: F811
    sent,
    stored,
):
    _, secret = await seeded_pass(home_iface)
    anna = await redeem(door, secret, "Anna")
    r = await _rename(door, anna["device_token"], sent)
    assert r.status_code == 200, r.text
    assert r.json() == {"display_name": stored}
    assert _names() == {anna["device_id"]: stored}


@pytest.mark.parametrize(
    "sent",
    [
        "",
        "   ",
        "　\n\t",
        "a" * 65,
        "a" * 63 + FLAG,  # 65 code points: refused whole, never cut to half a flag
        "a" * 60 + FAMILY,  # 65 code points: refused whole, never cut inside the family
        "An\x00na",  # PostgreSQL cannot store a NUL, and home's puller would drop the device
        None,
        42,
    ],
)
async def test_a_name_outside_the_rule_is_422_and_changes_nothing(door, home_iface, sent):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    anna = await redeem(door, secret, "Anna")
    cursor = _cursor(anna["device_id"])
    r = await _rename(door, anna["device_token"], sent)
    assert r.status_code == 422, r.text
    assert _names() == {anna["device_id"]: "Anna"}
    assert _cursor(anna["device_id"]) == cursor


async def test_a_body_without_the_field_is_422(door, home_iface):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    anna = await redeem(door, secret, "Anna")
    auth = {**bearer(anna["device_token"]), "Content-Type": "application/json"}
    for body in ("{}", json.dumps({"name": "Ben"}), "[]"):
        r = await door.patch("/api/guest/device", content=body, headers=auth)
        assert r.status_code == 422, (body, r.text)
    assert _names() == {anna["device_id"]: "Anna"}


@pytest.mark.parametrize(
    "header",
    [
        {},
        {"Authorization": "Bearer trip_dev_" + "z" * 43},  # the right shape, no such device
        {"Authorization": "Bearer memos_pat_" + "a" * 43},
        {"Authorization": f"Bearer {SERVICE_TOKEN}"},
        {"Cookie": "memos_sid=abc"},
    ],
)
async def test_no_guest_credential_is_401_and_renames_nothing(door, home_iface, header):  # noqa: F811
    _, secret = await seeded_pass(home_iface)
    anna = await redeem(door, secret, "Anna")
    r = await door.patch("/api/guest/device", json={"display_name": "Mallory"}, headers=header)
    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "Not a valid guest credential"}
    assert _names() == {anna["device_id"]: "Anna"}


async def test_a_revoked_device_is_401_while_its_sibling_still_renames(door, home_iface):  # noqa: F811
    pass_uid, secret = await seeded_pass(home_iface)
    anna = await redeem(door, secret, "Anna")
    ben = await redeem(door, secret, "Ben")
    revoked = await home_iface.put(
        "/ingest/device-revocations",
        json={
            "revocations": [
                {
                    "device_id": anna["device_id"],
                    "pass_uid": pass_uid,
                    "version": 2,
                    "revoked_at": datetime.now(UTC).isoformat(),
                }
            ]
        },
    )
    assert revoked.status_code == 200, revoked.text
    assert (await _rename(door, anna["device_token"], "Anna 2")).status_code == 401
    assert (await _rename(door, ben["device_token"], "Ben 2")).status_code == 200  # control
    assert _names() == {anna["device_id"]: "Anna", ben["device_id"]: "Ben 2"}


@pytest.mark.parametrize("ended", ["suspended", "revoked", "expired"])
async def test_an_ended_pass_is_410_like_every_other_guest_route(door, home_iface, ended):  # noqa: F811
    uid, secret = await seeded_pass(home_iface)
    anna = await redeem(door, secret, "Anna")
    if ended == "suspended":
        _sql("UPDATE passes SET suspended_at = now()")
    elif ended == "revoked":
        await push(
            home_iface,
            pass_state(
                uid,
                secret,
                version=2,
                revoked_at=datetime.now(UTC).isoformat(),
                revoked_version=2,
            ),
        )
    else:
        _sql("UPDATE passes SET expires_at = now() - interval '1 minute'")
    r = await _rename(door, anna["device_token"], "Anna 2")
    me = await door.get("/api/guest/me", headers=bearer(anna["device_token"]))
    assert (r.status_code, me.status_code) == (410, 410), r.text
    assert r.json() == me.json() == {"detail": "This pass is no longer open"}
    assert _names() == {anna["device_id"]: "Anna"}


async def test_a_rename_fenced_by_a_revocation_writes_nothing(door, home_iface):  # noqa: F811
    """The credential is asked again under the device lock: a revocation that commits while the
    rename waits for that lock wins, and the rename answers 401 having written nothing."""
    _, secret = await seeded_pass(home_iface)
    anna = await redeem(door, secret, "Anna")
    cursor = _cursor(anna["device_id"])
    async with async_session() as blocker:
        await blocker.execute(text("SELECT 1 FROM pass_devices FOR UPDATE"))
        task = asyncio.ensure_future(_rename(door, anna["device_token"], "Anna 2"))
        await _waiters(1)
        await blocker.execute(
            text("UPDATE pass_devices SET revoked_at = now(), revoked_version = 2")
        )
        await blocker.commit()
    assert (await task).status_code == 401
    assert _names() == {anna["device_id"]: "Anna"}
    assert _cursor(anna["device_id"]) == cursor


async def test_overlapping_renames_take_turns_and_the_last_one_stands(door, home_iface):  # noqa: F811
    """Two renames of one device are serialized by its row lock: each is answered with what it
    wrote, each draws its own cursor, and the row ends on the one that committed second."""
    _, secret = await seeded_pass(home_iface)
    anna = await redeem(door, secret, "Anna")
    cursor = _cursor(anna["device_id"])
    async with async_session() as blocker:
        await blocker.execute(text("SELECT 1 FROM pass_devices FOR UPDATE"))
        first = asyncio.ensure_future(_rename(door, anna["device_token"], "One"))
        await _waiters(1)
        second = asyncio.ensure_future(_rename(door, anna["device_token"], "Two"))
        await _waiters(2)
        await blocker.commit()
    a, b = await first, await second
    assert (a.json(), b.json()) == ({"display_name": "One"}, {"display_name": "Two"})
    assert _names() == {anna["device_id"]: "Two"}
    assert _cursor(anna["device_id"]) >= cursor + 2
