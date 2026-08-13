from pathlib import Path

from app.config import settings
from app.models import Attachment, AttachmentRendition, MediaJob, User, Visibility
from app.routers.assets import _all_asset_paths
from app.services import memos as memo_svc
from app.services import storage
from app.services import uploads as svc
from sqlalchemy import func, select

PNG = b"\x89PNG\r\n\x1a\n"


async def _owner(adb, subject):
    u = User(subject=subject, role="user")
    adb.add(u)
    await adb.flush()
    return u


async def _upload(adb, owner, name, data, mime="image/png"):
    up = await svc.create_upload(adb, owner_id=owner.id, filename=name, mime=mime, size=len(data))
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


async def _memo_with(adb, owner, uids):
    """A saved memo carrying `uids` — the earlier batch a later print has to find its screen in."""
    return await memo_svc.create_memo(
        adb, owner, content="x", visibility=Visibility.PUBLIC, attachment_uids=uids
    )


def _memo_files(memo):
    folder = storage.memo_dir(Path(settings.DATA_ROOT), memo.created_at, memo.uid)
    return sorted(p.name for p in folder.iterdir())


async def test_in_batch_screen_and_print_groups(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_group")
    screen = await _upload(adb, o, "_AR1.png", PNG + b"s" * 2000)
    glossy = await _upload(adb, o, "_AR1 - print (glossy).png", PNG + b"g" * 3000)
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
    s1 = await _upload(adb, o, "A.png", PNG + b"a" * 2000)
    p1 = await _upload(adb, o, "A - print (glossy).png", same_print)
    s2 = await _upload(adb, o, "B.png", PNG + b"b" * 2000)
    p2 = await _upload(adb, o, "B - print (glossy).png", same_print)  # byte-identical to p1
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
    screen = await _upload(adb, o, "X.png", PNG + b"x" * 2000)
    heic = b"\x00\x00\x00\x18ftypheic" + b"h" * 3000  # sniffs to image/heic, not the declared mime
    pr = await _upload(adb, o, "X - print (matte).heic", heic, mime="application/octet-stream")
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
    lone = await _upload(adb, o, "Y - print (glossy).png", PNG + b"y" * 2000)
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
    screen = await _upload(adb, o, "X.png", PNG + b"x" * 2000)
    a = await _upload(adb, o, "X - print (glossy).png", PNG + b"a" * 2000)
    b = await _upload(adb, o, "X - print (glossy).png", PNG + b"b" * 2500)  # same key, other bytes
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
    s1 = await _upload(adb, o, "X.png", PNG + b"1" * 2000)
    s2 = await _upload(adb, o, "X - screen.png", PNG + b"2" * 2500)
    pr = await _upload(adb, o, "X - print (matte).png", PNG + b"p" * 3000)
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
    first = await _upload(adb, o, "Z.png", body)
    await svc.finalize_pending(adb, [first.upload_uid], o.id)
    att_z = (await _attachments(adb, o.id))[0]

    # a later batch re-dumps the SAME screen bytes (dedups) + adds a print
    again = await _upload(adb, o, "Z.png", body)
    pr = await _upload(adb, o, "Z - print (glossy).png", PNG + b"p" * 3000)
    await svc.finalize_pending(adb, [again.upload_uid, pr.upload_uid], o.id)

    assert len(await _attachments(adb, o.id)) == 1  # no new screen (deduped)
    rends = await _renditions(adb, att_z.id)
    assert len(rends) == 1 and rends[0].variant_key == "print:glossy"


async def test_redump_same_key_is_idempotent(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_idem")
    body = PNG + b"s" * 4000
    s1 = await _upload(adb, o, "X.png", body)
    p1 = await _upload(adb, o, "X - print (glossy).png", PNG + b"g" * 3000)
    await svc.finalize_pending(adb, [s1.upload_uid, p1.upload_uid], o.id)
    att = (await _attachments(adb, o.id))[0]
    first_path = (await _renditions(adb, att.id))[0].storage_path

    # re-dump the SAME screen (dedups) + the SAME print key → write-once no-op
    s2 = await _upload(adb, o, "X.png", body)
    p2 = await _upload(adb, o, "X - print (glossy).png", PNG + b"DIFFERENT" * 200)
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
    up1 = await _upload(adb, o, "a.bin", data, mime="application/octet-stream")
    moved1: list[str] = []
    att1 = await svc.finalize(adb, up1, o.id, moved1)
    assert moved1 == [att1.storage_path] and Path(att1.storage_path).exists()

    up2 = await _upload(adb, o, "b.bin", data, mime="application/octet-stream")
    moved2: list[str] = []
    att2 = await svc.finalize(adb, up2, o.id, moved2)
    assert att2.uid == att1.uid  # deduped
    assert moved2 == []  # THE FIX: a dedup return tracks nothing for rollback

    for p in moved2:  # simulate the compose rolling back → unlink tracked paths
        Path(p).unlink(missing_ok=True)
    assert Path(att1.storage_path).exists()  # the original survives


async def test_cross_batch_print_binds_to_the_memos_screen_sibling(adb, monkeypatch, tmp_path):
    """MEMNG-79 (the production incident): the print export finished ~14 min after its screen and
    was added to the memo in a LATER edit. It must fill that screen's print slot — promoting it
    made the memo (and every image/* consumer, e.g. the daily mail) carry the photo twice."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_xbatch")
    screen = await _upload(adb, o, "_AR59988.png", PNG + b"s" * 2000)
    memo = await _memo_with(adb, o, await svc.finalize_pending(adb, [screen.upload_uid], o.id))

    body = PNG + b"p" * 3000
    pr = await _upload(adb, o, "_AR59988 - print.png", body)  # a batch of its own, 14 min later
    moved: list[str] = []
    out = await svc.finalize_pending(adb, [pr.upload_uid], o.id, moved_out=moved, memo=memo)

    assert out == []  # nothing promoted → nothing new for link_attachments to link
    atts = await _attachments(adb, o.id)
    assert len(atts) == 1 and atts[0].filename == "_AR59988.png"  # still ONE visible photo
    att = atts[0]
    assert att.print_path is not None
    assert Path(att.print_path).read_bytes() == body  # the print master itself, bound to the screen
    assert att.print_mime == "image/png" and att.print_size == len(body)
    folder = storage.memo_dir(Path(settings.DATA_ROOT), memo.created_at, memo.uid)
    assert Path(att.print_path).parent == folder  # placed beside the screen, under its real name
    assert Path(att.print_path).name == "_AR59988 - print.png"
    assert att.print_path in moved  # freshly placed → unlinked if the edit rolls back
    assert await _renditions(adb, att.id) == []
    jobs = (await adb.execute(select(func.count()).select_from(MediaJob))).scalar_one()
    assert jobs == 1  # only the screen's; a bound print master gets no derivation job


async def test_cross_batch_print_matches_an_explicit_screen_suffix(adb, monkeypatch, tmp_path):
    """The sibling match runs the same grammar over the stored CLIENT filename, so a screen dumped
    as `X - screen.png` groups a later `X - print (glossy).png` exactly as a bare `X.png` would."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_xbatch_screen")
    screen = await _upload(adb, o, "_AR60001 - screen.png", PNG + b"s" * 2000)
    memo = await _memo_with(adb, o, await svc.finalize_pending(adb, [screen.upload_uid], o.id))

    pr = await _upload(adb, o, "_AR60001 - print (glossy).png", PNG + b"g" * 2500)
    out = await svc.finalize_pending(adb, [pr.upload_uid], o.id, memo=memo)

    assert out == []
    atts = await _attachments(adb, o.id)
    assert len(atts) == 1 and atts[0].print_path is not None
    assert Path(atts[0].print_path).name == "_AR60001 - print (glossy).png"
    assert _memo_files(memo) == ["_AR60001 - print (glossy).png", "_AR60001 - screen.png"]


async def test_resent_cross_batch_print_replaces_and_does_not_duplicate(adb, monkeypatch, tmp_path):
    """Re-sending the pair (corrected export, same filename) replaces the bound master in place —
    it must not reserve a `X - print (2).png` sibling and orphan the previous file (MEMNG-78)."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_xbatch_resend")
    screen = await _upload(adb, o, "R.png", PNG + b"s" * 2000)
    memo = await _memo_with(adb, o, await svc.finalize_pending(adb, [screen.upload_uid], o.id))
    p1 = await _upload(adb, o, "R - print.png", PNG + b"1" * 3000)
    await svc.finalize_pending(adb, [p1.upload_uid], o.id, memo=memo)
    att = (await _attachments(adb, o.id))[0]
    first_path = att.print_path

    corrected = PNG + b"2" * 3500
    p2 = await _upload(adb, o, "R - print.png", corrected)
    out = await svc.finalize_pending(adb, [p2.upload_uid], o.id, memo=memo)

    assert out == []
    assert len(await _attachments(adb, o.id)) == 1
    assert att.print_path == first_path  # same file, replaced in place
    assert Path(first_path).read_bytes() == corrected and att.print_size == len(corrected)
    assert _memo_files(memo) == ["R - print.png", "R.png"]  # no orphaned ` (2)` master


async def test_cross_batch_print_without_a_sibling_is_still_promoted(adb, monkeypatch, tmp_path):
    """The no-match case is untouched: a print whose base is on no attachment of the memo (and in
    no batch screen) still becomes a normal visible photo — never silently hidden."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_xbatch_none")
    screen = await _upload(adb, o, "A.png", PNG + b"a" * 2000)
    memo = await _memo_with(adb, o, await svc.finalize_pending(adb, [screen.upload_uid], o.id))

    pr = await _upload(adb, o, "B - print.png", PNG + b"b" * 2500)
    out = await svc.finalize_pending(adb, [pr.upload_uid], o.id, memo=memo)

    assert len(out) == 1  # promoted
    atts = await _attachments(adb, o.id)
    assert len(atts) == 2
    promoted = next(x for x in atts if x.filename == "B - print.png")
    screen_att = next(x for x in atts if x.filename == "A.png")
    assert promoted.print_path is None and screen_att.print_path is None
    jobs = (await adb.execute(select(func.count()).select_from(MediaJob))).scalar_one()
    assert jobs == 2  # a promoted print is a normal photo → it gets its derivation job


async def test_only_one_cross_batch_print_binds_per_sibling(adb, monkeypatch, tmp_path):
    """The print slot holds ONE master: the second same-name print of a batch must be promoted, not
    written over the first (which would destroy the bytes the first upload just committed)."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_xbatch_two")
    screen = await _upload(adb, o, "X.png", PNG + b"s" * 2000)
    memo = await _memo_with(adb, o, await svc.finalize_pending(adb, [screen.upload_uid], o.id))

    first_body = PNG + b"1" * 3000
    a = await _upload(adb, o, "X - print.png", first_body)
    b = await _upload(adb, o, "X - print.png", PNG + b"2" * 3200)  # same name, other bytes
    out = await svc.finalize_pending(adb, [a.upload_uid, b.upload_uid], o.id, memo=memo)

    assert len(out) == 1  # the loser was promoted, not silently dropped
    atts = await _attachments(adb, o.id)
    assert len(atts) == 2
    screen_att = next(x for x in atts if x.filename == "X.png")
    assert Path(screen_att.print_path or "").read_bytes() == first_body  # first-wins, not clobbered


async def test_two_labelled_cross_batch_prints_keep_both_masters(adb, monkeypatch, tmp_path):
    """Two labels for one photo cannot share the single print slot: the first binds, the second is
    promoted (visible, cosmetically odd) rather than replacing a master the owner meant to keep."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_xbatch_labels")
    screen = await _upload(adb, o, "L.png", PNG + b"s" * 2000)
    memo = await _memo_with(adb, o, await svc.finalize_pending(adb, [screen.upload_uid], o.id))

    glossy = await _upload(adb, o, "L - print (glossy).png", PNG + b"g" * 3000)
    matte = await _upload(adb, o, "L - print (matte).png", PNG + b"m" * 3100)
    out = await svc.finalize_pending(adb, [glossy.upload_uid, matte.upload_uid], o.id, memo=memo)

    assert len(out) == 1
    atts = await _attachments(adb, o.id)
    screen_att = next(x for x in atts if x.filename == "L.png")
    assert Path(screen_att.print_path or "").name == "L - print (glossy).png"
    assert {x.filename for x in atts} == {"L.png", "L - print (matte).png"}


async def test_cross_batch_print_leaves_an_occupied_slot_alone(adb, monkeypatch, tmp_path):
    """A slot already holding a DIFFERENT master (here: bound by an earlier edit) is never
    overwritten — this call runs inside the memo transaction and could not sweep the superseded
    file if the edit rolled back, so the newcomer is promoted and both masters survive."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_xbatch_occupied")
    screen = await _upload(adb, o, "O.png", PNG + b"s" * 2000)
    memo = await _memo_with(adb, o, await svc.finalize_pending(adb, [screen.upload_uid], o.id))
    glossy_body = PNG + b"g" * 3000
    glossy = await _upload(adb, o, "O - print (glossy).png", glossy_body)
    await svc.finalize_pending(adb, [glossy.upload_uid], o.id, memo=memo)
    att = (await _attachments(adb, o.id))[0]
    bound_path = att.print_path

    matte = await _upload(adb, o, "O - print (matte).png", PNG + b"m" * 3300)
    out = await svc.finalize_pending(adb, [matte.upload_uid], o.id, memo=memo)

    assert len(out) == 1  # promoted
    assert att.print_path == bound_path
    assert Path(bound_path).read_bytes() == glossy_body  # the earlier master is intact
    assert {x.filename for x in await _attachments(adb, o.id)} == {
        "O.png",
        "O - print (matte).png",
    }


async def test_numeric_label_master_is_not_mistaken_for_a_collision_bump(
    adb, monkeypatch, tmp_path
):
    """The label grammar is `[^()]+`, so `N - print (2).png` is a legitimate `print:2` master whose
    STORED name is also the ` (2)` form a collision bump produces. A later bare `N - print.png` must
    NOT be read as a re-send of it: name inference alone cannot tell the two apart, so the bare
    print promotes and the label-2 master survives byte-for-byte."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_alias")
    screen = await _upload(adb, o, "N.png", PNG + b"s" * 2000)
    memo = await _memo_with(adb, o, await svc.finalize_pending(adb, [screen.upload_uid], o.id))
    label2 = PNG + b"2" * 3000
    two = await _upload(adb, o, "N - print (2).png", label2)  # label "2" → variant_key print:2
    await svc.finalize_pending(adb, [two.upload_uid], o.id, memo=memo)
    att = (await _attachments(adb, o.id))[0]
    bound_path = att.print_path
    assert Path(bound_path or "").name == "N - print (2).png"

    bare = await _upload(adb, o, "N - print.png", PNG + b"b" * 3300)
    out = await svc.finalize_pending(adb, [bare.upload_uid], o.id, memo=memo)

    assert len(out) == 1  # promoted — the slot holds a different master
    assert att.print_path == bound_path
    assert Path(bound_path).read_bytes() == label2  # not overwritten by the bare print
    assert {x.filename for x in await _attachments(adb, o.id)} == {"N.png", "N - print.png"}


async def test_resent_labelled_cross_batch_print_replaces_in_place(adb, monkeypatch, tmp_path):
    """The other side of that discrimination: an EXACT stored-name match is a true re-send and is
    still replaced in place — the slot keeps naming one file, not a pile of numbered copies."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_alias_resend")
    screen = await _upload(adb, o, "M.png", PNG + b"s" * 2000)
    memo = await _memo_with(adb, o, await svc.finalize_pending(adb, [screen.upload_uid], o.id))
    first = await _upload(adb, o, "M - print (glossy).png", PNG + b"1" * 3000)
    await svc.finalize_pending(adb, [first.upload_uid], o.id, memo=memo)
    att = (await _attachments(adb, o.id))[0]
    bound_path = att.print_path

    corrected = PNG + b"2" * 3400
    again = await _upload(adb, o, "M - print (glossy).png", corrected)
    out = await svc.finalize_pending(adb, [again.upload_uid], o.id, memo=memo)

    assert out == []  # bound, not promoted
    assert att.print_path == bound_path and Path(bound_path).read_bytes() == corrected
    assert _memo_files(memo) == ["M - print (glossy).png", "M.png"]  # no numbered leftover


async def test_cross_batch_print_never_binds_onto_a_promoted_print(adb, monkeypatch, tmp_path):
    """Only a SCREEN owns print masters. A print an earlier batch already promoted parses as a
    print, so it is no bind target — a later print of the same base promotes instead of hanging a
    print off a print row (repairing already-duplicated rows is a separate job)."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_xbatch_promoted")
    lone = await _upload(adb, o, "P - print.png", PNG + b"1" * 2000)
    memo = await _memo_with(adb, o, await svc.finalize_pending(adb, [lone.upload_uid], o.id))
    assert len(await _attachments(adb, o.id)) == 1

    again = await _upload(adb, o, "P - print.png", PNG + b"2" * 2400)
    out = await svc.finalize_pending(adb, [again.upload_uid], o.id, memo=memo)

    assert len(out) == 1
    atts = await _attachments(adb, o.id)
    assert len(atts) == 2 and all(x.print_path is None for x in atts)


async def test_delete_attachment_cascades_renditions_and_lists_files(adb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    o = await _owner(adb, "vp_del")
    screen = await _upload(adb, o, "D.png", PNG + b"d" * 2000)
    pr = await _upload(adb, o, "D - print (glossy).png", PNG + b"p" * 3000)
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
