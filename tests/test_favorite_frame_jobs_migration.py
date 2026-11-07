"""The favorite_frame_jobs migration (plan PLAN_2026-11-06_favorites-to-aura-frame.md, B1).

The table is additive and reuses the `frame_share_state` enum the curated queue created. Prod
already has that type, so the migration must never emit `CREATE TYPE frame_share_state` (check
R2-1): the online upgrade runs against a database where the type exists, and the offline SQL is
read for the statement itself. `frame_share_jobs` is not touched in either direction.
"""

import io
from pathlib import Path

from alembic import command
from alembic.config import Config
from app.models import FavoriteFrameJob
from sqlalchemy import create_engine, text
from sqlalchemy.engine import create_mock_engine

from tests.conftest import SYNC_URL

BEFORE_FAVORITES_REVISION = "a7d3e9f2c41b"
FAVORITES_REVISION = "31af52f05e0f"


def _sql(stmt: str, params: dict | None = None):
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        result = c.execute(text(stmt), params or {})
        rows = result.fetchall() if result.returns_rows else []
    eng.dispose()
    return rows


def _alembic(buffer: io.StringIO | None = None) -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"), output_buffer=buffer)


def _table_exists(name: str) -> bool:
    return bool(
        _sql(
            "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=:n",
            {"n": name},
        )
    )


def _indexes(table: str) -> list[tuple[str, str]]:
    return [
        (r[0], r[1])
        for r in _sql(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename=:t ORDER BY indexname",
            {"t": table},
        )
    ]


def _columns(table: str) -> list[tuple]:
    return [
        tuple(r)
        for r in _sql(
            "SELECT column_name, data_type, udt_name, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_name=:t ORDER BY ordinal_position",
            {"t": table},
        )
    ]


def test_head_carries_the_table_with_a_unique_attachment_index_and_the_shared_enum(db_engine):
    assert _table_exists("favorite_frame_jobs")
    unique = [
        name
        for name, ddl in _indexes("favorite_frame_jobs")
        if "UNIQUE" in ddl and "(attachment_id)" in ddl
    ]
    assert unique, _indexes("favorite_frame_jobs")
    state = [c for c in _columns("favorite_frame_jobs") if c[0] == "state"]
    assert state and state[0][2] == "frame_share_state"  # the SHARED type, not a copy
    photo_id = [c for c in _columns("favorite_frame_jobs") if c[0] == "aura_album_photo_id"]
    assert photo_id and photo_id[0][1] == "bigint" and photo_id[0][3] == "YES"
    # One type of that name, and the attachment FK cascades.
    assert _sql("SELECT count(*) FROM pg_type WHERE typname='frame_share_state'")[0][0] == 1
    fk = _sql(
        "SELECT confdeltype FROM pg_constraint WHERE conrelid='favorite_frame_jobs'::regclass "
        "AND contype='f' AND confrelid='attachments'::regclass"
    )
    assert [r[0] for r in fk] == ["c"]


def test_round_trip_leaves_frame_share_jobs_and_the_enum_untouched(db_engine):
    cfg = _alembic()
    before_idx = _indexes("frame_share_jobs")
    before_cols = _columns("frame_share_jobs")
    before_rows = _sql("SELECT count(*) FROM frame_share_jobs")[0][0]
    assert before_idx and before_cols  # control: the curated table is there to compare
    try:
        command.downgrade(cfg, BEFORE_FAVORITES_REVISION)
        assert not _table_exists("favorite_frame_jobs")
        # The downgrade drops only its own table; the type belongs to frame_share_jobs.
        assert _sql("SELECT count(*) FROM pg_type WHERE typname='frame_share_state'")[0][0] == 1
        assert _indexes("frame_share_jobs") == before_idx
    finally:
        # The type exists here (prod's situation): a migration that creates it fails right now.
        command.upgrade(cfg, "head")
    assert _table_exists("favorite_frame_jobs")
    assert _indexes("frame_share_jobs") == before_idx
    assert _columns("frame_share_jobs") == before_cols
    assert _sql("SELECT count(*) FROM frame_share_jobs")[0][0] == before_rows


def test_offline_sql_creates_the_table_and_never_the_type():
    buf = io.StringIO()
    command.upgrade(_alembic(buf), f"{BEFORE_FAVORITES_REVISION}:{FAVORITES_REVISION}", sql=True)
    ddl = buf.getvalue()
    assert "CREATE TABLE favorite_frame_jobs" in ddl  # positive control: the SQL was captured
    assert "CREATE TYPE" not in ddl
    assert "frame_share_jobs" not in ddl.replace("favorite_frame_jobs", "")


def test_the_model_emits_no_create_type_either():
    # The ORM side of R2-1: a `metadata.create_all` (or Table.create) must not try to create the
    # shared type. The generic sa.Enum(create_type=False) still emits it on SQLAlchemy 2.0.
    emitted: list[str] = []

    def dump(sql, *_args, **_kw):
        emitted.append(str(sql.compile(dialect=mock.dialect)))

    mock = create_mock_engine("postgresql+psycopg2://", dump)
    FavoriteFrameJob.__table__.create(mock, checkfirst=False)  # type: ignore[attr-defined]
    joined = "\n".join(emitted)
    assert "CREATE TABLE favorite_frame_jobs" in joined
    assert "CREATE TYPE" not in joined
