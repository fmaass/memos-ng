from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.services import storage


def test_memo_folder_name():
    dt = datetime(2026, 5, 21, 14, 32, tzinfo=UTC)
    assert storage.memo_folder_name(dt, "aB3xK9") == "2026-05-21_1432_aB3xK9"


def test_memo_dir_has_year_month_parents():
    dt = datetime(2026, 5, 21, 14, 32, tzinfo=UTC)
    root = Path("/data")
    assert (
        storage.memo_dir(root, dt, "uid1")
        == root / "originals" / "2026" / "05" / "2026-05-21_1432_uid1"
    )


def test_is_unfiled_is_true_only_for_the_staging_bucket():
    root = Path("/data")
    assert storage.is_unfiled("/data/originals/_unfiled/abc_x.png", root)
    # a placed master in a memo folder is NOT unfiled → the placement guard skips it
    assert not storage.is_unfiled("/data/originals/2026/05/2026-05-21_1432_uid/x.png", root)
    # the legacy flat layout is NOT unfiled either
    assert not storage.is_unfiled("/data/originals/2026/05/uuid_x.png", root)
    assert not storage.is_unfiled("/data/print/uuid.png", root)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("photo.jpg", "photo.jpg"),
        ("a/b/c.png", "c.png"),  # directory components dropped
        ("a\\b.png", "ab.png"),  # backslash (not a posix separator) stripped as a char
        (".hidden", "hidden"),  # leading dot trimmed
        ("trail.   ", "trail"),  # trailing space + dot trimmed
        ("with\ttab.png", "withtab.png"),  # control char removed
        ("", "file"),  # empty → fallback
        ("   ", "file"),  # whitespace-only → fallback
        ("...", "file"),  # dots-only → fallback
    ],
)
def test_sanitize_name(raw, expected):
    assert storage.sanitize_name(raw) == expected


def test_sanitize_name_caps_length():
    out = storage.sanitize_name("x" * 500 + ".png")
    assert len(out) <= storage.MAX_NAME_LEN


def test_place_master_moves_under_real_name(tmp_path):
    src = tmp_path / "uuid_src.part"
    src.write_bytes(b"payload")
    dest_dir = tmp_path / "originals" / "2026" / "05" / "2026-05-21_1432_uid"
    out = storage._place_master_sync(src, dest_dir, "Holiday Sunset.png")
    assert out == dest_dir / "Holiday Sunset.png"
    assert out.read_bytes() == b"payload"
    assert not src.exists()  # atomically moved, not copied


def test_place_master_numbers_collisions_and_never_overwrites(tmp_path):
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir(parents=True)
    (dest_dir / "IMG.png").write_bytes(b"first")
    src = tmp_path / "s.part"
    src.write_bytes(b"second")
    out = storage._place_master_sync(src, dest_dir, "IMG.png")
    assert out == dest_dir / "IMG (2).png"
    assert (dest_dir / "IMG.png").read_bytes() == b"first"  # the existing file is untouched
    assert out.read_bytes() == b"second"


async def test_place_master_async_wrapper_delegates(tmp_path):
    src = tmp_path / "a.part"
    src.write_bytes(b"x")
    out = await storage.place_master(src, tmp_path / "d", "n.bin")
    assert out == tmp_path / "d" / "n.bin"
    assert out.read_bytes() == b"x"
