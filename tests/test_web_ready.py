"""`webReady`: whether an attachment's `webUrl` has a file behind it.

`webUrl` is minted for every attachment, but the web tier is only ever written for a video the
worker has finished transcoding. These tests put the real rendition file on disk (or leave it
out) and read the flag back through every endpoint whose payload carries `webUrl`.
"""

import asyncio
import os

import pytest
from app.config import settings
from app.models import Attachment
from app.services import asset_paths
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

# uid -> (mime, status, what is on disk under assets/web, expected webReady)
CASES = {
    "wr-vid-ready": ("video/mp4", "ready", "final", True),
    # status says ready but the rendition is gone (a reclaim unlinked it, or an import never
    # made one): readiness follows the file, not the row
    "wr-vid-gone": ("video/quicktime", "ready", None, False),
    # mid-transcode: only the worker's temp output exists
    "wr-vid-transcoding": ("video/quicktime", "processing", "tmp", False),
    # a picture never has a web tier; a stray file at its web path must not flip it
    "wr-img": ("image/jpeg", "ready", "final", False),
}


def _place(root, uid, what):
    web = root / "assets" / "web"
    web.mkdir(parents=True, exist_ok=True)
    if what == "final":
        (web / f"{uid}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    elif what == "tmp":
        (web / f"{uid}.tmp.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")


def _seed(subject):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        oid = c.execute(text("SELECT id FROM users WHERE subject=:s"), {"s": subject}).scalar_one()
        mid = c.execute(
            text(
                "INSERT INTO memos (uid, owner_id, content, visibility, pinned, created_at, "
                "updated_at) VALUES ('wr-memo',:o,'m','PUBLIC',false,now(),now()) RETURNING id"
            ),
            {"o": oid},
        ).scalar_one()
        for pos, (uid, (mime, status, _, _)) in enumerate(CASES.items()):
            c.execute(
                text(
                    "INSERT INTO attachments (uid, owner_id, memo_id, filename, mime, size, "
                    "status, storage_path, blurhash, position, latitude, longitude, "
                    "location_source) VALUES (:u,:o,:m,'x',:mime,1,"
                    "CAST(:st AS attachment_status),'/x','LKO2',:p,12.5,45.25,'exif')"
                ),
                {"u": uid, "o": oid, "m": mid, "mime": mime, "st": status, "p": pos},
            )
    eng.dispose()
    return mid


def _wipe(mid):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM attachments WHERE memo_id=:m"), {"m": mid})
        c.execute(text("DELETE FROM memos WHERE id=:m"), {"m": mid})
    eng.dispose()


@pytest.fixture
def seeded(logged_in_client, monkeypatch, tmp_path):
    client, subject = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    for uid, (_, _, what, _) in CASES.items():
        _place(tmp_path, uid, what)
    mid = _seed(subject)
    try:
        yield client
    finally:
        _wipe(mid)


def _flags(items):
    return {i["uid"]: i["webReady"] for i in items if i["uid"] in CASES}


EXPECTED = {uid: case[3] for uid, case in CASES.items()}


def test_memo_feed_attachment_refs_carry_web_ready(seeded):
    body = seeded.get("/api/memos").json()
    [memo] = [m for m in body["items"] if m["uid"] == "wr-memo"]
    assert _flags(memo["attachments"]) == EXPECTED


def test_single_memo_attachment_refs_carry_web_ready(seeded):
    memo = seeded.get("/api/memos/wr-memo").json()
    assert _flags(memo["attachments"]) == EXPECTED


def test_attachment_list_items_carry_web_ready(seeded):
    assert _flags(seeded.get("/api/attachments").json()) == EXPECTED


def test_attachment_meta_carries_web_ready(seeded):
    got = {uid: seeded.get(f"/api/attachments/{uid}").json()["webReady"] for uid in CASES}
    assert got == EXPECTED


def test_map_markers_carry_web_ready(seeded):
    markers = seeded.get("/api/map/markers").json()["markers"]
    # the map lists ready rows only, so the mid-transcode video is not a marker at all
    assert _flags(markers) == {u: v for u, v in EXPECTED.items() if u != "wr-vid-transcoding"}


def test_the_web_url_is_still_minted_for_every_attachment(seeded):
    """The flag is additive: `webUrl` stays a non-null string on every item, ready or not."""
    for item in seeded.get("/api/attachments").json():
        if item["uid"] in CASES:
            assert item["webUrl"].startswith(f"/api/attachments/{item['uid']}/web?")


def _att(uid, mime):
    return Attachment(uid=uid, mime=mime, owner_id=1, filename="x", size=1, storage_path="/x")


def test_no_candidates_means_no_thread_hop(monkeypatch, tmp_path):
    """Empty input and image-only input answer without leaving the event loop."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _place(tmp_path, "img-stray", "final")

    async def refuse(*_a, **_k):
        raise AssertionError("no filesystem hop expected")

    monkeypatch.setattr(asset_paths.to_thread, "run_sync", refuse)
    assert asyncio.run(asset_paths.web_ready_uids([])) == frozenset()
    assert asyncio.run(asset_paths.web_ready_uids([_att("img-stray", "image/jpeg")])) == frozenset()


def test_a_page_is_checked_in_one_thread_hop(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    real = asset_paths.to_thread.run_sync
    hops = []

    async def counting(fn, *args, **kwargs):
        hops.append(fn)
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(asset_paths.to_thread, "run_sync", counting)
    for n in range(5):
        _place(tmp_path, f"v{n}", "final" if n % 2 == 0 else None)
    atts = [_att(f"v{n}", "video/mp4") for n in range(5)]
    assert asyncio.run(asset_paths.web_ready_uids(atts)) == {"v0", "v2", "v4"}
    assert len(hops) == 1


def test_publish_by_rename_flips_the_flag(monkeypatch, tmp_path):
    """The worker writes `<uid>.tmp.mp4` and renames it into place: the flag is false for the
    temp file alone and true once the rename lands — never true on a half-written file."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    _place(tmp_path, "v-pub", "tmp")
    att = _att("v-pub", "video/mp4")
    assert asyncio.run(asset_paths.web_ready_uids([att])) == frozenset()
    web = tmp_path / "assets" / "web"
    os.replace(web / "v-pub.tmp.mp4", web / "v-pub.mp4")
    assert asyncio.run(asset_paths.web_ready_uids([att])) == {"v-pub"}


def test_a_directory_or_unreadable_path_is_not_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    (tmp_path / "assets" / "web" / "v-dir.mp4").mkdir(parents=True)
    # a path the stat cannot traverse (a file where a directory is expected) is not-ready,
    # never an exception that would fail the whole feed page
    blocked = tmp_path / "blocked"
    blocked.write_bytes(b"")
    atts = [_att("v-dir", "video/mp4")]
    assert asyncio.run(asset_paths.web_ready_uids(atts)) == frozenset()
    monkeypatch.setattr(settings, "DATA_ROOT", str(blocked))
    assert asyncio.run(asset_paths.web_ready_uids(atts)) == frozenset()
