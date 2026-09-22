"""An attachment records WHEN it left its memo, because age since detachment is a different age.

Nothing on `attachments` could answer that question before. `created_at` cannot stand in for it: it
is the public `createTime` (`app/routers/compat.py`) and the map-marker date fallback
(`app/services/map_markers.py`), and a file uploaded last month and detached ten seconds ago is
already a month old by it — so anything that grants a detached row a grace period would sweep it
immediately while passing every fixture that creates and detaches in one breath.

So the detach sites stamp `detached_at`, and a row nothing detached — a standalone upload, a 0.29.1
import, a detach that predates the column — keeps NULL and is therefore not a candidate for
anything that selects on that age.
"""

from datetime import UTC, datetime

from app.models import Attachment, AttachmentStatus, User, Visibility
from app.services import memos as svc
from sqlalchemy import select, text


async def _user(adb, subject):
    u = User(subject=subject, role="user")
    adb.add(u)
    await adb.flush()
    return u


async def _attachment(adb, owner, memo, uid):
    att = Attachment(
        uid=uid,
        owner_id=owner.id,
        memo_id=None if memo is None else memo.id,
        filename=f"{uid}.png",
        mime="image/png",
        size=7,
        status=AttachmentStatus.ready,
        storage_path=f"/data/originals/2026/09/{uid}/{uid}.png",
    )
    adb.add(att)
    await adb.flush()
    return att


async def _stamps(adb, uids: list[str]) -> dict[str, tuple[int | None, datetime | None]]:
    """What the DATABASE holds for these rows — a fresh SELECT, not the identity map."""
    rows = await adb.execute(
        select(Attachment.uid, Attachment.memo_id, Attachment.detached_at).where(
            Attachment.uid.in_(uids)
        )
    )
    return {row.uid: (row.memo_id, row.detached_at) for row in rows}


async def test_delete_memo_stamps_one_detach_instant_on_the_rows_it_orphans(adb):
    owner = await _user(adb, "alice")
    memo = await svc.create_memo(adb, owner, content="pics", visibility=Visibility.PUBLIC)
    await _attachment(adb, owner, memo, "det-one")
    await _attachment(adb, owner, memo, "det-two")
    await adb.refresh(memo, ["attachments"])

    before = datetime.now(UTC)
    orphaned = await svc.delete_memo(adb, memo)
    after = datetime.now(UTC)

    assert len(orphaned) == 2
    stamps = await _stamps(adb, ["det-one", "det-two"])
    instants = set()
    for uid in ("det-one", "det-two"):
        memo_id, detached_at = stamps[uid]
        assert memo_id is None
        assert detached_at is not None, f"{uid} was orphaned without a detach instant"
        assert detached_at.tzinfo is not None
        assert before <= detached_at <= after
        instants.add(detached_at)
    assert len(instants) == 1  # one transaction detached them, so one instant names all of it


async def test_a_delete_stamps_nothing_it_did_not_detach(adb):
    """The rows a delete never touched must stay unstamped — including a standalone upload, which
    is unlinked because nothing ever linked it, not because something took it away."""
    owner = await _user(adb, "alice")
    doomed = await svc.create_memo(adb, owner, content="doomed", visibility=Visibility.PUBLIC)
    kept = await svc.create_memo(adb, owner, content="kept", visibility=Visibility.PUBLIC)
    await _attachment(adb, owner, doomed, "det-doomed")
    await _attachment(adb, owner, kept, "det-kept")
    await _attachment(adb, owner, None, "det-loose")
    await adb.refresh(doomed, ["attachments"])

    await svc.delete_memo(adb, doomed)

    stamps = await _stamps(adb, ["det-doomed", "det-kept", "det-loose"])
    assert stamps["det-doomed"][1] is not None
    assert stamps["det-kept"] == (kept.id, None)
    assert stamps["det-loose"] == (None, None)


async def test_detaching_moves_the_memo_link_and_the_instant_together(adb):
    owner = await _user(adb, "alice")
    memo = await svc.create_memo(adb, owner, content="pics", visibility=Visibility.PUBLIC)
    first = await _attachment(adb, owner, memo, "det-a")
    second = await _attachment(adb, owner, memo, "det-b")

    at = svc.detach_attachments([first, second])
    await adb.flush()

    stamps = await _stamps(adb, ["det-a", "det-b"])
    assert stamps["det-a"] == (None, at)
    assert stamps["det-b"] == (None, at)


def test_attachments_detached_at_is_nullable_and_timezone_aware(db_engine):
    """Read back from the MIGRATED schema — `db_engine` builds it with `alembic upgrade head` from
    an empty one, so this is the migration's own output and not the ORM's opinion of it."""
    with db_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT is_nullable, data_type FROM information_schema.columns "
                "WHERE table_name = 'attachments' AND column_name = 'detached_at'"
            )
        ).one_or_none()
    assert row is not None, "attachments.detached_at is not in the migrated schema"
    assert row.is_nullable == "YES"
    assert row.data_type == "timestamp with time zone"
