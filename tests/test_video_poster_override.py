"""A video may bring its own poster frame: `MemoCreate.video_posters` (video uid → poster uid).

The worker picks a video's poster by seeking ~12% into the clip and taking the frame most unlike
its neighbours — right for a captured video, and exactly wrong for one that OPENS on a title card,
because the seek exists to skip an intro. A client that already knows which image belongs on the
tile uploads it with the memo; it is consumed as that video's poster tier and never becomes an
attachment of its own.

Three properties carry the feature and each is pinned below against a real request, not a stub:
the poster lands as the tier (and only as the tier), the worker leaves a supplied poster alone
while still extracting one when none was supplied, and a refusal costs the caller nothing.
"""

import asyncio
import io
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import get_args

import pytest
from app.config import settings
from app.models import Attachment
from app.routers.memos import VIDEO_POSTER_MESSAGES
from app.services import media, poster
from app.services import uploads as up_svc
from PIL import Image
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

# Not a real container: puremagic recognises nothing here, so `_sniff_mime` falls back to the
# declared mime — the same fixture shape tests/test_uploads.py posts videos with.
VIDEO_BYTES = bytes(range(256)) * 400


def _jpeg(color=(20, 40, 60), size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _png(color=(200, 30, 30), size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM frame_share_jobs"))
        c.execute(text("DELETE FROM media_jobs"))
        c.execute(text("DELETE FROM attachments"))
        c.execute(text("DELETE FROM uploads"))
    eng.dispose()


def _open(client, name, mime, size, mtime=1609459200000) -> str:
    """Open an upload session and return its uid (no bytes sent yet)."""
    r = client.post(
        "/api/uploads",
        json={"filename": name, "mime": mime, "size": size, "last_modified": mtime},
    )
    assert r.status_code == 201, r.text
    return r.json()["upload_uid"]


def _upload(client, name, mime, data: bytes) -> str:
    uid = _open(client, name, mime, len(data))
    r = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    assert r.status_code == 200, r.text
    return uid


def _video(client, name="clip.mov", mime="video/quicktime") -> str:
    return _upload(client, name, mime, VIDEO_BYTES)


def _count(sql: str) -> int:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        n = c.execute(text(sql)).scalar_one()
    eng.dispose()
    return int(n)


def _scalar(sql: str):
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        value = c.execute(text(sql)).scalar_one()
    eng.dispose()
    return value


def _post(client, **body):
    return client.post("/api/memos", json={"visibility": "PUBLIC", **body})


def test_a_poster_override_lands_as_the_tier_and_never_as_an_attachment(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        title_card = _jpeg()
        video_uid = _video(client)
        poster_uid = _upload(client, "title.jpg", "image/jpeg", title_card)

        created = _post(
            client,
            content="the monthly video",
            upload_uids=[video_uid],
            video_posters={video_uid: poster_uid},
        )
        assert created.status_code == 201, created.text
        memo = created.json()

        # ONE tile: the video. The poster is a derived asset of it, not a second attachment.
        assert len(memo["attachments"]) == 1
        attachment = memo["attachments"][0]
        assert attachment["mime"] == "video/quicktime"
        assert attachment["status"] == "pending"  # the ordinary video route into the fast lane
        assert _count("SELECT count(*) FROM attachments") == 1

        # The bytes are the client's, verbatim, at the path the worker's hero frame would occupy.
        assert poster.poster_path(attachment["uid"]).read_bytes() == title_card
        assert not poster.poster_tmp_path(attachment["uid"]).exists()  # renamed into place

        # Both uploads are consumed — the poster one included, which is what makes it impossible to
        # replay or to finalize into an attachment afterwards.
        assert client.head(f"/api/uploads/{video_uid}").status_code == 404
        assert client.head(f"/api/uploads/{poster_uid}").status_code == 404
        # The poster is COPIED (its destination is on another mount), so its part file outlives the
        # copy and is reaped once the commit lands. Nothing else would ever collect it: the expiry
        # reaper unlinks part files by walking `uploads` rows, and this one's row is gone.
        assert not up_svc.part_path(poster_uid).exists()
        assert not up_svc.part_path(video_uid).exists()

        meta = client.get(f"/api/attachments/{attachment['uid']}")
        assert meta.status_code == 200
        assert meta.json()["hasPoster"] is True
    finally:
        _cleanup()


def test_a_video_posted_without_an_override_has_no_poster_yet(
    logged_in_client, monkeypatch, tmp_path
):
    """The control for the assertion above: `hasPoster` reports a FILE, and until the worker runs
    an ordinary video has none. Without this, a `hasPoster` hard-wired to True would pass."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        video_uid = _video(client)
        created = _post(client, content="no title card", upload_uids=[video_uid])
        assert created.status_code == 201, created.text
        uid = created.json()["attachments"][0]["uid"]

        assert not poster.poster_path(uid).exists()
        assert client.get(f"/api/attachments/{uid}").json()["hasPoster"] is False
    finally:
        _cleanup()


def test_a_failed_post_leaves_the_poster_retryable_and_no_orphan_tier_file(
    logged_in_client, monkeypatch, tmp_path
):
    """Consumption is atomic with the memo write. The poster is placed before the commit, so a
    write that does not land must (a) leave the bytes where the rolled-back upload row still
    promises them — otherwise the retry the client is entitled to has nothing to send — and
    (b) take the placed file with it: the attachment it was named after is rolled back too, so
    the poster would be an orphan under a uid nothing references."""
    from app.services import memos as memo_svc

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))

    async def boom(*args, **kwargs):
        raise RuntimeError("post failed after the uploads were consumed")

    try:
        title_card = _jpeg()
        video_uid = _video(client)
        poster_uid = _upload(client, "title.jpg", "image/jpeg", title_card)
        monkeypatch.setattr(memo_svc, "create_memo", boom)  # fails AFTER finalize_pending moved

        failed = _post(
            client,
            content="doomed",
            upload_uids=[video_uid],
            video_posters={video_uid: poster_uid},
        )
        assert failed.status_code == 500

        assert up_svc.part_path(poster_uid).read_bytes() == title_card  # byte for byte
        web = tmp_path / "assets" / "web"
        stranded = [p for p in web.rglob("*") if p.is_file()] if web.exists() else []
        # No poster AND no in-flight temp, under any uid — the copy is swept whatever it was named.
        assert stranded == []

        # ...and the session really is intact: the identical request succeeds on a later attempt.
        monkeypatch.undo()
        monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
        retried = _post(
            client,
            content="retried",
            upload_uids=[video_uid],
            video_posters={video_uid: poster_uid},
        )
        assert retried.status_code == 201, retried.text
        uid = retried.json()["attachments"][0]["uid"]
        assert poster.poster_path(uid).read_bytes() == title_card
    finally:
        _cleanup()


def test_a_poster_override_lands_across_the_deployed_mount_split(
    logged_in_client, monkeypatch, tmp_path, split_assets_mount
):
    """The production incident, pinned: the first real create through this feature 500'd because
    the poster was RENAMED from the upload parts onto the derived-asset mount. Publishing it has
    to be a copy into the destination directory followed by a within-device rename, so that no
    step of it ever crosses the boundary.
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    # The shared fixture (tests/conftest.py) guards the WHOLE assets directory in both directions
    # — one definition of the deployed boundary, reused by tests/test_asset_mount_boundary.py.
    split_assets_mount(tmp_path)
    try:
        title_card = _jpeg(color=(3, 33, 133))
        video_uid = _video(client)
        poster_uid = _upload(client, "title.jpg", "image/jpeg", title_card)

        created = _post(
            client,
            content="posted onto a split volume",
            upload_uids=[video_uid],
            video_posters={video_uid: poster_uid},
        )

        assert created.status_code == 201, created.text
        uid = created.json()["attachments"][0]["uid"]
        assert poster.poster_path(uid).read_bytes() == title_card
        assert not poster.poster_tmp_path(uid).exists()
        assert not up_svc.part_path(poster_uid).exists()  # reaped after the commit, as designed
        # The video's own original still travels by RENAME (uploads → originals → the memo folder,
        # one filesystem by design) — the guard above would have raised had that changed.
        assert client.get(f"/api/attachments/{uid}").json()["hasPoster"] is True
    finally:
        _cleanup()


async def _upload_async(bearer, name, mime, data: bytes) -> str:
    """The same two-step upload the sync helper does, over a bearer client (POST /api/memos and
    the upload verbs all take a PAT — app/auth.require_user)."""
    opened = await bearer.post(
        "/api/uploads",
        json={"filename": name, "mime": mime, "size": len(data), "last_modified": 1609459200000},
    )
    assert opened.status_code == 201, opened.text
    uid = opened.json()["upload_uid"]
    sent = await bearer.patch(
        f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"}
    )
    assert sent.status_code == 200, sent.text
    return uid


async def test_a_cancelled_create_never_strands_the_poster_it_was_copying(
    pat_client, monkeypatch, tmp_path
):
    """A client that hangs up mid-copy must not leave a poster behind.

    The copy runs in a worker thread, and a cancelled request does NOT wait for it — anyio raises
    the cancellation out of the await while the thread carries on placing the file. So the record
    the cleanup acts on is written BEFORE the copy starts, and publish/discard are serialised
    against each other, which is what makes the outcome the same in both orders. Nothing about the
    unit is stubbed: only the copy primitive is slowed down, so the cancellation has a window to
    land in that a real 200 KB copy would not reliably give.
    """
    bearer, _, _ = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    started = threading.Event()
    existed_after_copy: list[bool] = []
    real_copy = up_svc._copy_into_sync

    def slow_copy(src, dest, tmp):
        started.set()
        time.sleep(0.5)  # the window the cancellation lands in
        real_copy(src, dest, tmp)
        existed_after_copy.append(Path(dest).exists())  # the positive control, taken under the lock

    monkeypatch.setattr(up_svc, "_copy_into_sync", slow_copy)
    try:
        title_card = _jpeg(color=(11, 22, 33))
        video_uid = await _upload_async(bearer, "clip.mov", "video/quicktime", VIDEO_BYTES)
        poster_uid = await _upload_async(bearer, "title.jpg", "image/jpeg", title_card)

        posting = asyncio.create_task(
            bearer.post(
                "/api/memos",
                json={
                    "content": "hung up mid-copy",
                    "visibility": "PUBLIC",
                    "upload_uids": [video_uid],
                    "video_posters": {video_uid: poster_uid},
                },
            )
        )
        for _ in range(500):  # cancel only once the copy is genuinely under way
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set(), "the copy never started — cancelling here would prove nothing"
        posting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await posting

        # Positive control: the stub really did place its file, and saw it on disk. Without this,
        # "nothing survives" would also be satisfied by a copy that never happened at all.
        assert existed_after_copy == [True]

        assets = tmp_path / "assets"
        survivors = [str(p) for p in assets.rglob("*") if p.is_file()] if assets.exists() else []
        assert survivors == []  # no poster, no temp, under any uid
        assert _count("SELECT count(*) FROM memos") == 0
        assert _count("SELECT count(*) FROM attachments") == 0
        # The client's bytes are untouched, so the post it never completed can simply be retried.
        assert up_svc.part_path(poster_uid).read_bytes() == title_card
    finally:
        _cleanup()


async def test_a_cancel_during_the_post_commit_reap_leaves_the_stored_memo_whole(
    pat_client, monkeypatch, tmp_path
):
    """The mirror of the test above, and the more dangerous half: once the memo is COMMITTED, the
    failure path must not run at all. A cancellation delivered during the best-effort part reap
    lands in the same handler that discards posters and moves originals back — on a memo that is
    already stored, that is not cleanup, it is deleting a live memo's media. The committed flag is
    what makes the handler stand down; at worst a part file is left unreaped.
    """
    from app.routers import memos as memos_router

    bearer, _, _ = pat_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    started = threading.Event()
    reaping: list[list[str]] = []
    real_unlink_all = memos_router._unlink_all

    def slow_reap(paths):
        reaping.append(list(paths))  # in-stub control: the reap really began, with these files
        started.set()
        time.sleep(0.5)  # the cancellation lands here, AFTER the commit
        real_unlink_all(paths)

    monkeypatch.setattr(memos_router, "_unlink_all", slow_reap)
    try:
        title_card = _jpeg(color=(44, 55, 66))
        video_uid = await _upload_async(bearer, "clip.mov", "video/quicktime", VIDEO_BYTES)
        poster_uid = await _upload_async(bearer, "title.jpg", "image/jpeg", title_card)

        posting = asyncio.create_task(
            bearer.post(
                "/api/memos",
                json={
                    "content": "committed, then hung up",
                    "visibility": "PUBLIC",
                    "upload_uids": [video_uid],
                    "video_posters": {video_uid: poster_uid},
                },
            )
        )
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set(), "the reap never started — this would not test the committed gate"
        posting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await posting

        # The control: the reap really was in flight, and over this poster's own part file.
        assert reaping == [[str(up_svc.part_path(poster_uid))]]

        # Everything the commit stored is still there — this is the data-loss assertion.
        assert _count("SELECT count(*) FROM memos") == 1
        assert _count("SELECT count(*) FROM attachments") == 1
        uid = _scalar("SELECT uid FROM attachments")
        assert poster.poster_path(uid).read_bytes() == title_card
        # The accepted worst case, and the only one: the consumed part file was not reaped.
        assert up_svc.part_path(poster_uid).exists()
    finally:
        _cleanup()


def test_a_replayed_keyed_create_answers_with_the_stored_memo_and_consumes_nothing_twice(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        title_card = _jpeg()
        video_uid = _video(client)
        poster_uid = _upload(client, "title.jpg", "image/jpeg", title_card)
        body = {
            "content": "posted twice",
            "visibility": "PUBLIC",
            "upload_uids": [video_uid],
            "video_posters": {video_uid: poster_uid},
            "idempotency_key": "poster-replay-key-1",
        }

        first = client.post("/api/memos", json=body)
        assert first.status_code == 201, first.text
        uid = first.json()["attachments"][0]["uid"]

        # The replay is answered from the stored memo, BEFORE the uploads are looked at — which is
        # the only reason it can succeed at all: this request's uploads are gone, consumed by the
        # first call. A second consumption would 422 (`poster_missing`) instead of 201.
        replay = client.post("/api/memos", json=body)
        assert replay.status_code == 201, replay.text
        assert replay.json()["uid"] == first.json()["uid"]
        assert len(replay.json()["attachments"]) == 1
        assert replay.json()["attachments"][0]["uid"] == uid

        assert _count("SELECT count(*) FROM memos") == 1
        assert _count("SELECT count(*) FROM attachments") == 1
        assert poster.poster_path(uid).read_bytes() == title_card
    finally:
        _cleanup()


# --- refusals: one case per category in uploads.VideoPosterReason ---------------------------


def _case_video_not_uploaded(client):
    video_uid = _video(client)
    poster_uid = _upload(client, "t.jpg", "image/jpeg", _jpeg())
    # the mapping names a video this memo is not posting (upload_uids does not carry it)
    return {"upload_uids": [], "video_posters": {video_uid: poster_uid}}, video_uid


def _case_video_not_a_video(client):
    image_uid = _upload(client, "photo.jpg", "image/jpeg", _jpeg(color=(1, 2, 3)))
    poster_uid = _upload(client, "t.jpg", "image/jpeg", _jpeg())
    return {"upload_uids": [image_uid], "video_posters": {image_uid: poster_uid}}, image_uid


def _case_video_is_a_print_variant(client):
    video_uid = _video(client, name="clip - print.mov")
    poster_uid = _upload(client, "t.jpg", "image/jpeg", _jpeg())
    return {"upload_uids": [video_uid], "video_posters": {video_uid: poster_uid}}, video_uid


def _case_poster_missing(client):
    video_uid = _video(client)
    return {"upload_uids": [video_uid], "video_posters": {video_uid: "no-such-upload"}}, video_uid


def _case_poster_incomplete(client):
    video_uid = _video(client)
    poster_uid = _open(client, "t.jpg", "image/jpeg", len(_jpeg()))  # opened, never sent
    return {"upload_uids": [video_uid], "video_posters": {video_uid: poster_uid}}, video_uid


def _case_poster_not_a_jpeg(client):
    video_uid = _video(client)
    # DECLARED image/jpeg, and the bytes say otherwise — what they ARE is what decides.
    poster_uid = _upload(client, "t.jpg", "image/jpeg", _png())
    return {"upload_uids": [video_uid], "video_posters": {video_uid: poster_uid}}, video_uid


def _case_poster_unidentifiable_bytes(client):
    """The lenient-fallback hole: `_sniff_mime` answers with the CLIENT'S declared mime for bytes
    puremagic cannot place, so these very bytes pass as a video when declared one (see the tests
    above, which depend on exactly that). A poster has to be positively identified instead —
    otherwise this file lands as the tier, the worker skips extraction because a poster exists,
    and the blurhash decode fails on a video that can never get a poster again."""
    video_uid = _video(client)
    poster_uid = _upload(client, "t.jpg", "image/jpeg", VIDEO_BYTES)
    return {"upload_uids": [video_uid], "video_posters": {video_uid: poster_uid}}, video_uid


def _case_poster_listed_as_attachment(client):
    video_uid = _video(client)
    poster_uid = _upload(client, "t.jpg", "image/jpeg", _jpeg())
    return {
        "upload_uids": [video_uid, poster_uid],
        "video_posters": {video_uid: poster_uid},
    }, video_uid


def _case_poster_reused(client):
    first = _video(client, name="a.mov")
    second = _video(client, name="b.mov")
    poster_uid = _upload(client, "t.jpg", "image/jpeg", _jpeg())
    # Mappings are settled in sorted order, so the SECOND-sorting video is the one refused —
    # deterministic whatever order the JSON object happened to arrive in.
    loser = sorted([first, second])[1]
    return {
        "upload_uids": [first, second],
        "video_posters": {first: poster_uid, second: poster_uid},
    }, loser


REFUSALS = [
    ("video_not_uploaded", _case_video_not_uploaded),
    ("video_not_a_video", _case_video_not_a_video),
    ("video_is_a_print_variant", _case_video_is_a_print_variant),
    ("poster_missing", _case_poster_missing),
    ("poster_incomplete", _case_poster_incomplete),
    ("poster_not_a_jpeg", _case_poster_not_a_jpeg),
    ("poster_not_a_jpeg", _case_poster_unidentifiable_bytes),
    ("poster_listed_as_attachment", _case_poster_listed_as_attachment),
    ("poster_reused", _case_poster_reused),
]

# Named after the CASE, not the category: two cases share `poster_not_a_jpeg` (recognisably wrong
# bytes, and bytes nothing recognises at all), and they must stay tellable apart in a failure line.
REFUSAL_IDS = [build.__name__.removeprefix("_case_") for _, build in REFUSALS]


@pytest.mark.parametrize(("reason", "build"), REFUSALS, ids=REFUSAL_IDS)
def test_each_refusal_names_its_category_and_leaves_the_request_unfinalized(
    reason, build, logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        body, offender = build(client)
        refused = _post(client, content="refused", **body)

        assert refused.status_code == 422, refused.text
        detail = refused.json()["detail"]
        assert [d["type"] for d in detail] == [f"video_poster.{reason}"]
        assert detail[0]["loc"] == ["body", "video_posters", offender]
        assert detail[0]["msg"]

        # Refused whole, before the first side effect: no memo, no attachment, and nothing moved
        # out of the uploads directory — the same request works once the mapping is fixed.
        assert _count("SELECT count(*) FROM memos") == 0
        assert _count("SELECT count(*) FROM attachments") == 0
        originals = tmp_path / "originals"
        assert not (originals.exists() and any(p.is_file() for p in originals.rglob("*")))
        assert not (tmp_path / "assets").exists()
    finally:
        _cleanup()


@pytest.mark.parametrize(
    ("name", "payload", "expected"),
    [
        ("real.jpg", _jpeg(), True),  # the positive control: a genuine JPEG still passes
        ("progressive.jpg", _jpeg(size=(8, 8)), True),  # a tiny one is still a JPEG
        ("actually.png", _png(), False),
        ("unidentifiable.bin", VIDEO_BYTES, False),  # what the declared-mime fallback let through
        ("truncated.jpg", _jpeg()[:2], False),  # SOI alone is not the signature
        ("empty.jpg", b"", False),
    ],
)
def test_the_poster_gate_identifies_a_jpeg_positively(name, payload, expected, tmp_path):
    """One case per input category (project convention for a classifier). The `unidentifiable`
    row is the one that matters: `_sniff_mime` would answer those bytes with whatever the client
    declared, and this gate must not."""
    path = tmp_path / name
    path.write_bytes(payload)
    assert up_svc._is_jpeg_sync(path) is expected


def test_the_poster_gate_refuses_a_file_it_cannot_read(tmp_path):
    """A stat/read that fails is a refusal, never a pass — a gate that opened up when the disk
    misbehaved would be a gate in name only."""
    assert up_svc._is_jpeg_sync(tmp_path / "does-not-exist.jpg") is False
    assert up_svc._is_jpeg_sync(tmp_path) is False  # a directory: OSError, not a JPEG


def test_the_refusal_categories_and_their_messages_are_the_same_set():
    """The router renders a message per category; a category added without one would raise a
    KeyError inside the error path — i.e. a 500 in place of the 422 it was written for."""
    assert set(get_args(up_svc.VideoPosterReason)) == set(VIDEO_POSTER_MESSAGES)


# --- the worker half: a supplied poster is kept, an absent one is still derived ---------------


def _write_clip(path, size="320x240"):
    """A real 1s test clip — the poster path is only meaningful against something ffmpeg can
    actually decode (same helper idiom as tests/test_media.py)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=duration=1:size={size}:rate=5",
            "-pix_fmt", "yuv420p", str(path),
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_the_fast_lane_keeps_a_supplied_poster_and_blurhashes_it(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    clip = tmp_path / "originals" / "clip.mp4"
    _write_clip(clip)
    attachment = Attachment(
        uid="override1", filename="clip.mp4", mime="video/mp4", size=clip.stat().st_size,
        storage_path=str(clip),
    )
    title_card = _jpeg(color=(9, 99, 199), size=(128, 96))
    supplied = poster.poster_path(attachment.uid)
    supplied.parent.mkdir(parents=True, exist_ok=True)
    supplied.write_bytes(title_card)

    meta = media.process_video_poster(attachment)

    # The frame the seek WOULD have picked never touches the file — this is the whole feature.
    assert supplied.read_bytes() == title_card
    # The probe still describes the video itself, and the placeholder is derived from the poster
    # that is actually there.
    assert (meta["width"], meta["height"]) == (320, 240)
    assert isinstance(meta["blurhash"], str) and meta["blurhash"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_the_fast_lane_still_extracts_a_hero_frame_when_none_was_supplied(monkeypatch, tmp_path):
    """The positive control for the test above: the skip is conditional on a poster BEING there,
    not on the extraction having been removed."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    clip = tmp_path / "originals" / "clip.mp4"
    _write_clip(clip)
    attachment = Attachment(
        uid="derived1", filename="clip.mp4", mime="video/mp4", size=clip.stat().st_size,
        storage_path=str(clip),
    )
    derived = poster.poster_path(attachment.uid)
    assert not derived.exists()

    meta = media.process_video_poster(attachment)

    assert derived.exists()
    with Image.open(derived) as image:
        assert image.format == "JPEG" and image.size == (320, 240)
    assert not poster.poster_tmp_path(attachment.uid).exists()  # renamed into place, no debris
    assert isinstance(meta["blurhash"], str) and meta["blurhash"]


# --- the Aura-frame guard: a curated video must never be mailed to the photo frame -------------


def test_a_curated_video_with_a_poster_enqueues_no_frame_shares(
    logged_in_client, monkeypatch, tmp_path
):
    """#curated mails every IMAGE ATTACHMENT of a memo to the household photo frame. A poster
    override puts an image inside a curated memo for the first time — as a derived asset, which
    the queue must not see. Pinned with a positive control in the same test, because the enqueue
    runs inside a savepoint whose errors are swallowed: without one, a feature that never ran at
    all would count zero jobs and read as a pass.
    """
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "FRAME_API_URL", "http://frame.local")
    monkeypatch.setattr(settings, "FRAME_ALBUM_ID", 7)
    monkeypatch.setattr(settings, "FRAME_API_TOKEN", "album-token")
    try:
        video_uid = _video(client)
        poster_uid = _upload(client, "title.jpg", "image/jpeg", _jpeg())
        curated = _post(
            client,
            content="the monthly video #curated",
            upload_uids=[video_uid],
            video_posters={video_uid: poster_uid},
        )
        assert curated.status_code == 201, curated.text
        assert _count("SELECT count(*) FROM frame_share_jobs") == 0

        # Positive control: the very same request path, with a real photo, DOES enqueue — so the
        # zero above is the is_frame_image filter answering, not the feature being switched off.
        photo_uid = _upload(client, "photo.jpg", "image/jpeg", _jpeg(color=(7, 7, 7)))
        with_photo = _post(client, content="a photo #curated", upload_uids=[photo_uid])
        assert with_photo.status_code == 201, with_photo.text
        assert _count("SELECT count(*) FROM frame_share_jobs") == 1
    finally:
        _cleanup()
