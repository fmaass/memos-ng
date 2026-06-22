from pathlib import Path

from app.config import settings
from app.models import Attachment, AttachmentRendition, MediaJob, User
from app.routers.assets import _all_asset_paths
from app.services import uploads as svc
from sqlalchemy import func, select

PNG = b"\x89PNG\r\n\x1a\n"


async def _owner(adb, subject):
    u = User(subject=subject, role="user")
    adb.add(u)
    await adb.flush()
    return u


async def _upload(adb, name, data, mime="image/png"):
    up = await svc.create_upload(adb, filename=name, mime=mime, size=len(data))
    await svc.append_chunk(adb, up, 0, data)
    return up


async def _attachments(adb, owner_id):
    rows = await adb.execute(select(Attachment).where(Attachment.owner_id == owner_id))
    return rows.scalars().all()


async def _renditions(adb, attachment_id):
    return (
        (
            await adb.execute(
                select(AttachmentRendition).where(
                    AttachmentRendition.attachment_id == attachment_id
                )
            )
        )
        .scalars()
        .all()
    )


async def test_in_batch_screen_and_print_groups(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_group")
    screen = await _upload(adb, "_AR1.png", PNG + b"s" * 2000)
    glossy = await _upload(adb, "_AR1 - print (glossy).png", PNG + b"g" * 3000)
    out = await svc.finalize_pending(adb, [screen.upload_uid, glossy.upload_uid], o.id)

    # exactly ONE visible Attachment (the screen); the print is NOT an Attachment row
    assert len(out) == 1
    atts = await _attachments(adb, o.id)
    assert len(atts) == 1 and atts[0].filename == "_AR1.png"
    rends = await _renditions(adb, atts[0].id)
    assert len(rends) == 1
    r = rends[0]
    assert r.variant_key == "print:glossy" and r.label == "glossy"
    assert Path(r.storage_path).exists()
    # finalize_pending alone (no memo link) stages the print in _unfiled; link_attachments later
    # moves it into the memo folder (covered by tests/test_placement.py).
    assert Path(r.storage_path).parent == Path(settings.DATA_ROOT) / "originals" / "_unfiled"


async def test_identical_print_bytes_on_two_photos_are_not_file_shared(adb, monkeypatch, tmp_path):
    # sha256 is informational only — two photos with byte-identical prints must get DISTINCT files,
    # so deleting one photo can never unlink the other's rendition (the cross-row deletion hazard)
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_noshare")
    same_print = PNG + b"identical-print-bytes" * 200
    s1 = await _upload(adb, "A.png", PNG + b"a" * 2000)
    p1 = await _upload(adb, "A - print (glossy).png", same_print)
    s2 = await _upload(adb, "B.png", PNG + b"b" * 2000)
    p2 = await _upload(adb, "B - print (glossy).png", same_print)  # byte-identical to p1
    uids = [s1.upload_uid, p1.upload_uid, s2.upload_uid, p2.upload_uid]
    await svc.finalize_pending(adb, uids, o.id)

    atts = await _attachments(adb, o.id)
    a = next(x for x in atts if x.filename == "A.png")
    b = next(x for x in atts if x.filename == "B.png")
    ra = (await _renditions(adb, a.id))[0]
    rb = (await _renditions(adb, b.id))[0]
    assert ra.sha256 == rb.sha256  # same content...
    assert ra.storage_path != rb.storage_path  # ...but distinct files (no sharing)
    assert Path(ra.storage_path).exists() and Path(rb.storage_path).exists()


async def test_rendition_enqueues_zero_mediajob_even_for_heic(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_nojob")
    screen = await _upload(adb, "X.png", PNG + b"x" * 2000)
    heic = b"\x00\x00\x00\x18ftypheic" + b"h" * 3000  # sniffs to image/heic, not the declared mime
    pr = await _upload(adb, "X - print (matte).heic", heic, mime="application/octet-stream")
    await svc.finalize_pending(adb, [screen.upload_uid, pr.upload_uid], o.id)

    att = (await _attachments(adb, o.id))[0]
    r = (await _renditions(adb, att.id))[0]
    assert r.mime == "image/heic"  # sniffed via the shared path, not trusted from the declared mime
    # only the SCREEN got a MediaJob (1); the rendition added none (download-only archive master)
    total_jobs = (await adb.execute(select(func.count()).select_from(MediaJob))).scalar_one()
    assert total_jobs == 1


async def test_lone_print_is_promoted_to_visible(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_promote")
    lone = await _upload(adb, "Y - print (glossy).png", PNG + b"y" * 2000)
    out = await svc.finalize_pending(adb, [lone.upload_uid], o.id)

    assert len(out) == 1  # promoted to a normal visible photo
    atts = await _attachments(adb, o.id)
    assert len(atts) == 1 and atts[0].filename == "Y - print (glossy).png"
    assert await _renditions(adb, atts[0].id) == []  # no rendition (it became the photo)
    jobs = (await adb.execute(select(func.count()).select_from(MediaJob))).scalar_one()
    assert jobs == 1  # a promoted print is a normal photo → gets its derivation job


async def test_in_batch_variant_key_collision_promotes_loser(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_collide")
    screen = await _upload(adb, "X.png", PNG + b"x" * 2000)
    a = await _upload(adb, "X - print (glossy).png", PNG + b"a" * 2000)
    b = await _upload(adb, "X - print (glossy).png", PNG + b"b" * 2500)  # same key, different bytes
    out = await svc.finalize_pending(adb, [screen.upload_uid, a.upload_uid, b.upload_uid], o.id)

    # screen + the colliding loser (promoted) = 2 visible photos; exactly 1 rendition (the winner)
    assert len(out) == 2
    atts = await _attachments(adb, o.id)
    screen_att = next(x for x in atts if x.filename == "X.png")
    assert len(await _renditions(adb, screen_att.id)) == 1
    total_rends = (
        await adb.execute(select(func.count()).select_from(AttachmentRendition))
    ).scalar_one()
    assert total_rends == 1  # the loser was promoted, NOT silently dropped


async def test_two_screens_same_base_print_binds_first(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_two")
    s1 = await _upload(adb, "X.png", PNG + b"1" * 2000)
    s2 = await _upload(adb, "X - screen.png", PNG + b"2" * 2500)
    pr = await _upload(adb, "X - print (matte).png", PNG + b"p" * 3000)
    out = await svc.finalize_pending(adb, [s1.upload_uid, s2.upload_uid, pr.upload_uid], o.id)

    assert len(out) == 2  # both screens are visible photos
    atts = await _attachments(adb, o.id)
    first = next(x for x in atts if x.filename == "X.png")  # first-screen-wins
    second = next(x for x in atts if x.filename == "X - screen.png")
    assert len(await _renditions(adb, first.id)) == 1
    assert await _renditions(adb, second.id) == []


async def test_print_attaches_to_a_deduped_pre_existing_screen(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_dedup")
    body = PNG + b"z" * 4000
    first = await _upload(adb, "Z.png", body)
    await svc.finalize_pending(adb, [first.upload_uid], o.id)
    att_z = (await _attachments(adb, o.id))[0]

    # a later batch re-dumps the SAME screen bytes (dedups) + adds a print
    again = await _upload(adb, "Z.png", body)
    pr = await _upload(adb, "Z - print (glossy).png", PNG + b"p" * 3000)
    await svc.finalize_pending(adb, [again.upload_uid, pr.upload_uid], o.id)

    assert len(await _attachments(adb, o.id)) == 1  # no new screen (deduped)
    rends = await _renditions(adb, att_z.id)
    assert len(rends) == 1 and rends[0].variant_key == "print:glossy"


async def test_redump_same_key_is_idempotent(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_idem")
    body = PNG + b"s" * 4000
    s1 = await _upload(adb, "X.png", body)
    p1 = await _upload(adb, "X - print (glossy).png", PNG + b"g" * 3000)
    await svc.finalize_pending(adb, [s1.upload_uid, p1.upload_uid], o.id)
    att = (await _attachments(adb, o.id))[0]
    first_path = (await _renditions(adb, att.id))[0].storage_path

    # re-dump the SAME screen (dedups) + the SAME print key → write-once no-op
    s2 = await _upload(adb, "X.png", body)
    p2 = await _upload(adb, "X - print (glossy).png", PNG + b"DIFFERENT" * 200)
    await svc.finalize_pending(adb, [s2.upload_uid, p2.upload_uid], o.id)

    rends = await _renditions(adb, att.id)
    assert len(rends) == 1 and rends[0].storage_path == first_path  # unchanged


async def test_dedup_finalize_tracks_no_file_for_rollback(adb, monkeypatch, tmp_path):
    # Pre-existing bug: finalize_pending appended a dedup-returned (committed) path to moved_out, so
    # a rolled-back compose unlinked a PRIOR memo's file. The fix moves the append into finalize's
    # new-file branch only.
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_moved")
    data = b"dup-bytes" * 500
    up1 = await _upload(adb, "a.bin", data, mime="application/octet-stream")
    moved1: list[str] = []
    att1 = await svc.finalize(adb, up1, o.id, moved1)
    assert moved1 == [att1.storage_path] and Path(att1.storage_path).exists()

    up2 = await _upload(adb, "b.bin", data, mime="application/octet-stream")
    moved2: list[str] = []
    att2 = await svc.finalize(adb, up2, o.id, moved2)
    assert att2.uid == att1.uid  # deduped
    assert moved2 == []  # THE FIX: a dedup return tracks nothing for rollback

    for p in moved2:  # simulate the compose rolling back → unlink tracked paths
        Path(p).unlink(missing_ok=True)
    assert Path(att1.storage_path).exists()  # the original survives


async def test_delete_attachment_cascades_renditions_and_lists_files(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_del")
    screen = await _upload(adb, "D.png", PNG + b"d" * 2000)
    pr = await _upload(adb, "D - print (glossy).png", PNG + b"p" * 3000)
    await svc.finalize_pending(adb, [screen.upload_uid, pr.upload_uid], o.id)

    att = (await _attachments(adb, o.id))[0]
    await adb.refresh(att, ["renditions"])
    rpath = Path(att.renditions[0].storage_path)
    assert rpath.exists()
    assert rpath in _all_asset_paths(att)  # the router's _unlink_all will remove it

    rid = att.renditions[0].id
    await adb.delete(att)
    await adb.flush()
    gone = (
        await adb.execute(select(AttachmentRendition).where(AttachmentRendition.id == rid))
    ).scalar_one_or_none()
    assert gone is None  # DB ON DELETE CASCADE removed the rendition row
