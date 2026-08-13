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


def test_sanitize_name_caps_by_bytes_not_codepoints():
    # a multibyte name must be capped by ENCODED bytes (filesystem component limit is byte-based),
    # never splitting a char, and the extension is preserved
    out = storage.sanitize_name("é" * 300 + ".jpg")  # 'é' = 2 bytes → 600+ bytes raw
    assert len(out.encode()) <= storage.MAX_NAME_LEN
    assert out.endswith(".jpg")
    out.encode()  # round-trips → no broken trailing multibyte sequence


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


@pytest.mark.parametrize(
    "existing,name,expected",
    [
        ("IMG.png", "IMG.png", True),  # the sanitized name itself
        ("IMG (2).png", "IMG.png", True),  # …and the ` (n)` form _reserve bumped it to
        ("IMG (11).png", "IMG.png", True),
        ("IMG.png", "../IMG.png", True),  # compared after sanitization, like the placement
        ("IMG (1).png", "IMG.png", False),  # _reserve starts at 2 → ` (1)` is a literal name
        ("IMG (2).tif", "IMG.png", False),  # different extension → a different file
        ("OTHER.png", "IMG.png", False),
        ("IMG (2) (2).png", "IMG.png", False),  # only one bump level is _reserve's own output
    ],
)
def test_is_placement_of(existing, name, expected):
    dest = Path("/data/originals/2026/05/2026-05-21_1432_uid")
    assert storage.is_placement_of(dest / existing, dest, name) is expected


def test_is_placement_of_requires_the_same_directory():
    dest = Path("/data/originals/2026/05/2026-05-21_1432_uid")
    # the legacy print/ file of an attachment since linked to a memo is NOT the memo folder's file
    assert not storage.is_placement_of("/data/print/uid.png", dest, "uid.png")


async def test_replace_master_overwrites_in_place(tmp_path):
    dest = tmp_path / "memo" / "IMG (2).png"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old-print")
    src = tmp_path / "staged.part"
    src.write_bytes(b"new-print")
    out = await storage.replace_master(src, dest)
    assert out == dest
    assert dest.read_bytes() == b"new-print"
    assert not src.exists()  # moved, not copied
    assert sorted(p.name for p in dest.parent.iterdir()) == ["IMG (2).png"]  # no sibling created
