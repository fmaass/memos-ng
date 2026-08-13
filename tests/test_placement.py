"""Per-memo originals placement (new-writes-only). A newly-finalized master stages in
`originals/_unfiled/` and is atomically moved into the memo folder when the memo is saved; an
existing (committed) master is NEVER moved, so no memo↔file association can break."""

from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.models import Attachment, AttachmentRendition, MediaJob, User, Visibility
from app.routers.memos import _unlink_paths
from app.services import memos as memo_svc
from app.services import storage
from app.services import uploads as up_svc
from sqlalchemy import create_engine, select, text

from tests.conftest import SYNC_URL

PNG = b"\x89PNG\r\n\x1a\n"


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(
            text(
                "TRUNCATE attachment_renditions, media_jobs, attachments, memo_tags, "
                "reactions, memos, uploads RESTART IDENTITY CASCADE"
            )
        )
    eng.dispose()


def _start(client, name, mime, size):
    r = client.post("/api/uploads", json={"filename": name, "mime": mime, "size": size})
    assert r.status_code == 201
    uid = r.json()["upload_uid"]
    return uid


async def _owner(adb, subject):
    u = User(subject=subject, role="user")
    adb.add(u)
    await adb.flush()
    return u


async def _upload(adb, owner, name, data, mime="image/png"):
    up = await up_svc.create_upload(
        adb, owner_id=owner.id, filename=name, mime=mime, size=len(data)
    )
    await up_svc.append_chunk(adb, up, 0, data)
    return up


async def _att(adb, uid):
    return (await adb.execute(select(Attachment).where(Attachment.uid == uid))).scalar_one()


async def test_screen_and_print_land_in_memo_folder_under_real_names(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "pl_basic")
    screen = await _upload(adb, o, "Holiday Sunset.png", PNG + b"s" * 2000)
    glossy = await _upload(adb, o, "Holiday Sunset - print (glossy).png", PNG + b"g" * 3000)
    moved: list[str] = []
    finalized = await up_svc.finalize_pending(
        adb, [screen.upload_uid, glossy.upload_uid], o.id, moved_out=moved
    )
    memo = await memo_svc.create_memo(
        adb,
        o,
        content="x",
        visibility=Visibility.PUBLIC,
        attachment_uids=finalized,
        moved_out=moved,
    )

    folder = storage.memo_dir(Path(settings.DATA_ROOT), memo.created_at, memo.uid)
    att = await _att(adb, finalized[0])
    assert Path(att.storage_path).parent == folder
    assert Path(att.storage_path).name == "Holiday Sunset.png"  # real name, not a uuid
    assert Path(att.storage_path).exists()

    rends = (
        (
            await adb.execute(
                select(AttachmentRendition).where(AttachmentRendition.attachment_id == att.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rends) == 1
    assert Path(rends[0].storage_path).parent == folder  # print sits beside the screen
    assert Path(rends[0].storage_path).name == "Holiday Sunset - print (glossy).png"
    assert Path(rends[0].storage_path).exists()
    # both placed paths are tracked for rollback-unlink
    assert att.storage_path in moved and rends[0].storage_path in moved


async def test_finalized_but_unlinked_attachment_stays_in_unfiled(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "pl_unfiled")
    up = await _upload(adb, o, "lonely.png", PNG + b"s" * 2000)
    finalized = await up_svc.finalize_pending(adb, [up.upload_uid], o.id)
    att = await _att(adb, finalized[0])
    assert storage.is_unfiled(att.storage_path, Path(settings.DATA_ROOT))
    assert Path(att.storage_path).exists()


async def test_same_filename_twice_in_one_memo_is_numbered(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "pl_collide")
    a1 = await _upload(adb, o, "IMG.png", PNG + b"a" * 2000)
    a2 = await _upload(adb, o, "IMG.png", PNG + b"b" * 2000)  # same name, distinct bytes (no dedup)
    finalized = await up_svc.finalize_pending(adb, [a1.upload_uid, a2.upload_uid], o.id)
    await memo_svc.create_memo(
        adb, o, content="x", visibility=Visibility.PUBLIC, attachment_uids=finalized
    )
    atts = (
        (await adb.execute(select(Attachment).where(Attachment.owner_id == o.id))).scalars().all()
    )
    names = sorted(Path(x.storage_path).name for x in atts)
    assert names == ["IMG (2).png", "IMG.png"]
    assert all(Path(x.storage_path).exists() for x in atts)


async def test_rollback_unlinks_placed_file_but_never_a_prior_memos_master(
    adb, monkeypatch, tmp_path
):
    """The association-safety invariant: a rolled-back post unlinks only the file IT placed; a
    committed master belonging to another memo is never in `moved` and survives."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "pl_rb")
    # a prior, committed memo + master
    pre = await _upload(adb, o, "old.png", PNG + b"o" * 2000)
    pre_uids = await up_svc.finalize_pending(adb, [pre.upload_uid], o.id)
    await memo_svc.create_memo(
        adb, o, content="m1", visibility=Visibility.PUBLIC, attachment_uids=pre_uids
    )
    pre_att = await _att(adb, pre_uids[0])
    pre_path = pre_att.storage_path
    assert Path(pre_path).exists()

    # a new post that will "roll back"
    new = await _upload(adb, o, "new.png", PNG + b"n" * 2000)
    moved: list[str] = []
    new_uids = await up_svc.finalize_pending(adb, [new.upload_uid], o.id, moved_out=moved)
    await memo_svc.create_memo(
        adb,
        o,
        content="m2",
        visibility=Visibility.PUBLIC,
        attachment_uids=new_uids,
        moved_out=moved,
    )
    new_att = await _att(adb, new_uids[0])
    assert Path(new_att.storage_path).exists()

    _unlink_paths(moved)  # the router's finally-on-rollback
    assert not Path(new_att.storage_path).exists()  # the placed file is gone
    assert pre_path not in moved  # the prior master was never tracked
    assert Path(pre_path).exists()  # …and is untouched


async def test_existing_master_is_not_moved_when_relinked(adb, monkeypatch, tmp_path):
    """Proves new-writes-only: an already-placed master re-linked to another memo stays put."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "pl_guard")
    up = await _upload(adb, o, "p.png", PNG + b"p" * 2000)
    uids = await up_svc.finalize_pending(adb, [up.upload_uid], o.id)
    await memo_svc.create_memo(
        adb, o, content="m1", visibility=Visibility.PUBLIC, attachment_uids=uids
    )
    att = await _att(adb, uids[0])
    placed = att.storage_path
    assert not storage.is_unfiled(placed, Path(settings.DATA_ROOT))

    moved: list[str] = []
    await memo_svc.create_memo(
        adb,
        o,
        content="m2",
        visibility=Visibility.PUBLIC,
        attachment_uids=[att.uid],
        moved_out=moved,
    )
    await adb.refresh(att)
    assert att.storage_path == placed  # NOT moved
    assert moved == []
    assert Path(placed).exists()


async def test_worker_sees_the_final_memo_folder_path(adb, monkeypatch, tmp_path):
    """The MediaJob and the placed storage_path commit together, so the worker never reads a
    pre-placement `_unfiled` path."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "pl_job")
    up = await _upload(adb, o, "w.png", PNG + b"w" * 2000)
    uids = await up_svc.finalize_pending(adb, [up.upload_uid], o.id)
    memo = await memo_svc.create_memo(
        adb, o, content="x", visibility=Visibility.PUBLIC, attachment_uids=uids
    )
    att = await _att(adb, uids[0])
    job = (await adb.execute(select(MediaJob).where(MediaJob.attachment_id == att.id))).scalar_one()
    assert job.attachment_id == att.id
    assert not storage.is_unfiled(att.storage_path, Path(settings.DATA_ROOT))
    assert Path(att.storage_path).parent == storage.memo_dir(
        Path(settings.DATA_ROOT), memo.created_at, memo.uid
    )


async def test_same_photo_in_two_memos_does_not_strand_the_first(adb, monkeypatch, tmp_path):
    """Association-safety regression (Codex HIGH): re-uploading identical bytes into a second memo
    must NOT dedup-and-steal the first memo's attachment. Dedup only reuses UNLINKED attachments, so
    the second memo gets its own row + file and BOTH memos keep their photo."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "pl_steal")
    same = PNG + b"identical-photo-bytes" * 400

    upA = await _upload(adb, o, "P.png", same)
    finA = await up_svc.finalize_pending(adb, [upA.upload_uid], o.id)
    memoA = await memo_svc.create_memo(
        adb, o, content="A", visibility=Visibility.PUBLIC, attachment_uids=finA
    )
    attA = await _att(adb, finA[0])

    upB = await _upload(adb, o, "P.png", same)  # byte-identical, second memo
    finB = await up_svc.finalize_pending(adb, [upB.upload_uid], o.id)
    assert finB[0] != finA[0]  # NOT deduped to A's (linked) attachment → a fresh row
    memoB = await memo_svc.create_memo(
        adb, o, content="B", visibility=Visibility.PUBLIC, attachment_uids=finB
    )

    await adb.refresh(attA)
    assert attA.memo_id == memoA.id  # A's attachment was NOT stolen
    a_atts = (
        (await adb.execute(select(Attachment).where(Attachment.memo_id == memoA.id)))
        .scalars()
        .all()
    )
    b_atts = (
        (await adb.execute(select(Attachment).where(Attachment.memo_id == memoB.id)))
        .scalars()
        .all()
    )
    assert len(a_atts) == 1 and len(b_atts) == 1  # both memos keep a photo
    assert a_atts[0].storage_path != b_atts[0].storage_path  # distinct files (no sharing)
    assert Path(a_atts[0].storage_path).exists() and Path(b_atts[0].storage_path).exists()


async def test_in_post_duplicate_still_dedups(adb, monkeypatch, tmp_path):
    """The dedup guard must NOT break in-post dedup: two identical files in ONE memo collapse to one
    attachment (both are still UNLINKED when the second finalizes)."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "pl_indedup")
    same = PNG + b"dup" * 700
    u1 = await _upload(adb, o, "a.png", same)
    u2 = await _upload(adb, o, "b.png", same)
    fin = await up_svc.finalize_pending(adb, [u1.upload_uid, u2.upload_uid], o.id)
    assert len(set(fin)) == 1  # collapsed to a single attachment within the post


async def test_relink_via_attachment_uids_does_not_steal(adb, monkeypatch, tmp_path):
    """Defence for the back-compat attachment_uids path: passing an already-linked attachment's uid
    to a new memo must NOT move it (link_attachments skips a foreign-memo attachment)."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "pl_relink")
    up = await _upload(adb, o, "x.png", PNG + b"x" * 2000)
    fin = await up_svc.finalize_pending(adb, [up.upload_uid], o.id)
    memoA = await memo_svc.create_memo(
        adb, o, content="A", visibility=Visibility.PUBLIC, attachment_uids=fin
    )
    att = await _att(adb, fin[0])
    placed = att.storage_path
    await memo_svc.create_memo(
        adb, o, content="B", visibility=Visibility.PUBLIC, attachment_uids=[att.uid]
    )
    await adb.refresh(att)
    assert att.memo_id == memoA.id  # not stolen
    assert att.storage_path == placed  # not moved


async def test_custom_created_at_drives_the_folder(adb, monkeypatch, tmp_path):
    """A caller-supplied display date names the folder (the refresh must not clobber it)."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "pl_date")
    custom = datetime(2021, 3, 7, 9, 5, tzinfo=UTC)
    up = await _upload(adb, o, "d.png", PNG + b"d" * 2000)
    fin = await up_svc.finalize_pending(adb, [up.upload_uid], o.id)
    memo = await memo_svc.create_memo(
        adb, o, content="x", visibility=Visibility.PUBLIC, attachment_uids=fin, created_at=custom
    )
    assert memo.created_at == custom  # refresh reloaded the persisted custom value, not now()
    att = await _att(adb, fin[0])
    assert Path(att.storage_path).parent == storage.memo_dir(
        Path(settings.DATA_ROOT), custom, memo.uid
    )
    assert "2021/03/2021-03-07_0905_" in att.storage_path


async def test_patch_memo_adds_photo_and_places_it(adb, monkeypatch, tmp_path):
    """Adding a photo to an existing memo via the edit path places it in that memo's folder."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "pl_patch")
    memo = await memo_svc.create_memo(adb, o, content="m", visibility=Visibility.PUBLIC)
    up = await _upload(adb, o, "added.png", PNG + b"p" * 2000)
    moved: list[str] = []
    fin = await up_svc.finalize_pending(adb, [up.upload_uid], o.id, moved_out=moved)
    await memo_svc.link_attachments(adb, o.id, memo, fin, moved_out=moved)
    att = await _att(adb, fin[0])
    folder = storage.memo_dir(Path(settings.DATA_ROOT), memo.created_at, memo.uid)
    assert Path(att.storage_path).parent == folder
    assert Path(att.storage_path).name == "added.png" and Path(att.storage_path).exists()
    assert att.storage_path in moved


def test_api_upload_post_places_masters_and_serves_them(logged_in_client, monkeypatch, tmp_path):
    """End-to-end through the real HTTP API: upload a screen + its print, POST a memo, then prove
    both masters were placed in the per-memo folder under real names AND still serve from there —
    the memo↔file association is intact after placement."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        screen = PNG + b"S" * 4000
        printb = PNG + b"P" * 5000
        su = _start(client, "Holiday Sunset.png", "image/png", len(screen))
        assert (
            client.patch(
                f"/api/uploads/{su}", content=screen, headers={"Upload-Offset": "0"}
            ).status_code
            == 200
        )
        pu = _start(client, "Holiday Sunset - print (glossy).png", "image/png", len(printb))
        client.patch(f"/api/uploads/{pu}", content=printb, headers={"Upload-Offset": "0"})

        r = client.post(
            "/api/memos",
            json={"content": "trip", "visibility": "PUBLIC", "upload_uids": [su, pu]},
        )
        assert r.status_code == 201, r.text
        memo = r.json()
        # only the screen is a visible attachment; the print is a hidden rendition on it
        assert len(memo["attachments"]) == 1
        att = memo["attachments"][0]
        assert att["hasPrint"] is True and len(att["renditions"]) == 1

        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            sp = c.execute(
                text("SELECT storage_path FROM attachments WHERE uid=:u"), {"u": att["uid"]}
            ).scalar_one()
            rp = c.execute(text("SELECT storage_path FROM attachment_renditions")).scalar_one()
            ca, mu = c.execute(text("SELECT created_at, uid FROM memos")).first()
        eng.dispose()

        folder = storage.memo_dir(Path(str(tmp_path)), ca, mu)
        assert Path(sp).parent == folder
        assert Path(sp).name == "Holiday Sunset.png" and Path(sp).exists()
        assert Path(rp).parent == folder
        assert Path(rp).name == "Holiday Sunset - print (glossy).png" and Path(rp).exists()

        # the photo serves from its placed location (original tier, auth path)
        got = client.get(f"/api/attachments/{att['uid']}/original")
        assert got.status_code == 200 and got.content == screen
        # the hidden print serves from the same folder via its download URL
        pr = client.get(att["renditions"][0]["downloadUrl"])
        assert pr.status_code == 200 and pr.content == printb
    finally:
        _cleanup()


def test_upload_print_to_linked_attachment_lands_in_memo_folder(
    logged_in_client, monkeypatch, tmp_path
):
    """P2: a /print upload to an attachment already in a memo lands in that memo's folder under its
    real filename (beside the screen) and serves via the print tier."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        screen = PNG + b"S" * 3000
        su = _start(client, "beach.png", "image/png", len(screen))
        client.patch(f"/api/uploads/{su}", content=screen, headers={"Upload-Offset": "0"})
        memo = client.post(
            "/api/memos", json={"content": "x", "visibility": "PUBLIC", "upload_uids": [su]}
        ).json()
        att_uid = memo["attachments"][0]["uid"]

        printb = PNG + b"P" * 4000
        r = client.post(
            f"/api/attachments/{att_uid}/print",
            files={"file": ("beach - print (matte).tif", printb, "image/tiff")},
        )
        assert r.status_code == 200, r.text

        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            pp = c.execute(
                text("SELECT print_path FROM attachments WHERE uid=:u"), {"u": att_uid}
            ).scalar_one()
            ca, mu = c.execute(text("SELECT created_at, uid FROM memos")).first()
        eng.dispose()

        folder = storage.memo_dir(Path(str(tmp_path)), ca, mu)
        assert Path(pp).parent == folder
        assert Path(pp).name == "beach - print (matte).tif"
        assert Path(pp).exists() and Path(pp).read_bytes() == printb
        got = client.get(f"/api/attachments/{att_uid}/print")  # serves from the memo folder
        assert got.status_code == 200 and got.content == printb
    finally:
        _cleanup()


def test_upload_print_to_unlinked_attachment_keeps_print_dir(
    logged_in_client, monkeypatch, tmp_path
):
    """P2: a /print upload to an attachment NOT linked to a memo keeps the legacy print/ path."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        screen = PNG + b"u" * 3000
        su = _start(client, "loose.png", "image/png", len(screen))
        client.patch(f"/api/uploads/{su}", content=screen, headers={"Upload-Offset": "0"})
        att = client.post(f"/api/uploads/{su}/finalize").json()  # attachment, NOT in a memo

        printb = PNG + b"q" * 2000
        r = client.post(
            f"/api/attachments/{att['uid']}/print",
            files={"file": ("loose - print.tif", printb, "image/tiff")},
        )
        assert r.status_code == 200

        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            pp = c.execute(
                text("SELECT print_path FROM attachments WHERE uid=:u"), {"u": att["uid"]}
            ).scalar_one()
        eng.dispose()
        assert Path(pp).parent == Path(str(tmp_path)) / "print"  # legacy location, unchanged
        assert Path(pp).exists()
    finally:
        _cleanup()


def test_reupload_print_to_unlinked_attachment_keeps_legacy_file(
    logged_in_client, monkeypatch, tmp_path
):
    """The legacy print/{uid} path is deterministic and may be shared/committed — a re-upload must
    leave a valid file at print_path (commit-failure cleanup must never touch the legacy path)."""
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        screen = PNG + b"u" * 3000
        su = _start(client, "loose.png", "image/png", len(screen))
        client.patch(f"/api/uploads/{su}", content=screen, headers={"Upload-Offset": "0"})
        att = client.post(f"/api/uploads/{su}/finalize").json()

        first = PNG + b"first-print" * 100
        second = PNG + b"second-print" * 100
        client.post(
            f"/api/attachments/{att['uid']}/print",
            files={"file": ("a.tif", first, "image/tiff")},
        )
        r2 = client.post(
            f"/api/attachments/{att['uid']}/print",
            files={"file": ("a.tif", second, "image/tiff")},
        )
        assert r2.status_code == 200
        got = client.get(f"/api/attachments/{att['uid']}/print")
        assert got.status_code == 200 and got.content == second  # latest content, file intact
    finally:
        _cleanup()
