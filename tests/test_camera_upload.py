"""Camera EXIF through the REAL upload API, and the detail that serves it (MEMNG-865).

The upload half pins that finalize stores the eight camera columns AND the `camera_source` ledger
on the new row — so a fresh upload is never a backfill candidate — and that no camera read, however
bad the file, refuses an upload. The detail half pins that `camera` is served only for a row whose
ledger says `'exif'`, whatever the columns hold, and that the files fold F1 is about serialize.
"""

import math
from pathlib import Path

import pytest
from app.config import settings
from app.services import camera
from sqlalchemy import create_engine, text

from tests import camera_fixtures as fx
from tests.conftest import SYNC_URL
from tests.test_assets import _cleanup as _cleanup_ids
from tests.test_assets import _seed_attachment
from tests.test_location_upload import _cleanup, _upload

CAMERA_COLUMNS = (
    "camera_make, camera_model, lens_model, f_number, exposure_time_s, focal_length_mm,"
    " exposure_bias_ev, iso, camera_source"
)


def _stored(attachment_uid: str) -> dict[str, object]:
    eng = create_engine(SYNC_URL)
    with eng.connect() as c:
        row = c.execute(
            text(f"SELECT {CAMERA_COLUMNS} FROM attachments WHERE uid = :u"),
            {"u": attachment_uid},
        ).one()
    eng.dispose()
    return dict(row._mapping)


def _post(client, name: str, mime: str, data: bytes) -> str:
    upload_uid = _upload(client, name, mime, data)
    created = client.post(
        "/api/memos",
        json={"visibility": "PUBLIC", "content": name, "upload_uids": [upload_uid]},
    )
    assert created.status_code == 201, created.text
    return created.json()["attachments"][0]["uid"]


def test_the_upload_path_stores_the_camera_fields_and_the_ledger(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = fx.full_jpeg(tmp_path / "src.jpg").read_bytes()
        uid = _post(client, "full.jpg", "image/jpeg", data)
        assert _stored(uid) == {
            "camera_make": "ACME",
            "camera_model": "Fieldcam 9",
            "lens_model": "ACME 35mm F1.8",
            "f_number": pytest.approx(2.8),
            "exposure_time_s": pytest.approx(0.004),
            "focal_length_mm": pytest.approx(35.0),
            "exposure_bias_ev": pytest.approx(-0.3),
            "iso": 400,
            "camera_source": "exif",
        }
        meta = client.get(f"/api/attachments/{uid}").json()
        assert meta["camera"] == {
            "make": "ACME",
            "model": "Fieldcam 9",
            "lensModel": "ACME 35mm F1.8",
            "fNumber": pytest.approx(2.8),
            "exposureTimeS": pytest.approx(0.004),
            "focalLengthMm": pytest.approx(35.0),
            "exposureBiasEv": pytest.approx(-0.3),
            "iso": 400,
        }
    finally:
        _cleanup()


@pytest.mark.parametrize(
    ("builder", "source"),
    [
        (fx.bare_jpeg, "none"),
        (fx.corrupt, "unreadable"),
        (fx.truncated_ifd_jpeg, "unreadable"),
        (fx.invalid_only_jpeg, "none"),
        (fx.huge_iso_jpeg, "exif"),
        (fx.zero_denominator_jpeg, "exif"),
        (fx.nul_make_jpeg, "exif"),
    ],
)
def test_every_bad_or_silent_file_still_finalizes_and_its_detail_serializes(
    builder, source, logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = builder(tmp_path / "src.jpg").read_bytes()
        uid = _post(client, "f.jpg", "image/jpeg", data)
        assert _stored(uid)["camera_source"] == source
        r = client.get(f"/api/attachments/{uid}")
        assert r.status_code == 200, r.text
        cam = r.json()["camera"]
        if source != "exif":
            assert cam is None
        else:
            for value in cam.values():
                assert not (isinstance(value, float) and not math.isfinite(value))
    finally:
        _cleanup()


P = fx.EXIF_PREFIX


@pytest.mark.parametrize(
    ("build", "name", "mime", "source"),
    [
        (lambda p: fx.jpeg_app1(p, P), "f.jpg", "image/jpeg", "unreadable"),
        (lambda p: fx.png_exif_chunk(p, b"", after_idat=True), "f.png", "image/png", "unreadable"),
        (lambda p: fx.webp_exif_chunk(p, b""), "f.webp", "image/webp", "unreadable"),
        (lambda p: fx.heif_exif_extent(p, b""), "f.heic", "image/heic", "unreadable"),
        (lambda p: fx.jpeg_app1(p, P + fx.EMPTY_CLASSIC), "f.jpg", "image/jpeg", "none"),
        (
            lambda p: fx.png_exif_chunk(p, fx.oversized_exif(), after_idat=True),
            "f.png",
            "image/png",
            "unreadable",
        ),
        (
            lambda p: fx.webp_vp8x_without_exif_chunk(p, flag=True),
            "f.webp",
            "image/webp",
            "unreadable",
        ),
        (lambda p: fx.webp_with_exif_bytes_appended(p), "f.webp", "image/webp", "none"),
    ],
    ids=[
        "jpeg-prefix-only",
        "png-empty-chunk",
        "webp-empty-chunk",
        "heif-empty-item",
        "jpeg-empty-ifd0",
        "png-oversized-after-idat",
        "webp-vp8x-flag-only",
        "webp-bytes-past-riff",
    ],
)
def test_a_declared_but_empty_exif_upload_is_stored_unreadable_not_none(
    build, name, mime, source, logged_in_client, monkeypatch, tmp_path
):
    # 'none' is final and 'unreadable' is what --retry-unreadable revisits: a file that declared
    # EXIF and gave none back must land in the class that is read again.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        data = build(tmp_path / f"src{Path(name).suffix}").read_bytes()
        uid = _post(client, name, mime, data)
        assert _stored(uid)["camera_source"] == source
        r = client.get(f"/api/attachments/{uid}")
        assert r.status_code == 200, r.text
        assert r.json()["camera"] is None
    finally:
        _cleanup()


def test_a_reader_that_blows_up_never_fails_the_upload(logged_in_client, monkeypatch, tmp_path):
    from app.services import uploads

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))

    def _boom(path, mime):
        raise RuntimeError("a reader bug")

    monkeypatch.setattr(uploads.camera, "read_camera", _boom)
    try:
        data = fx.full_jpeg(tmp_path / "src.jpg").read_bytes()
        uid = _post(client, "full.jpg", "image/jpeg", data)
        stored = _stored(uid)
        assert stored["camera_source"] == "unreadable"
        assert stored["camera_make"] is None
    finally:
        _cleanup()


def test_a_stalled_camera_read_settles_as_unreadable(logged_in_client, monkeypatch, tmp_path):
    from app.services import uploads

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(uploads, "CAMERA_READ_TIMEOUT_S", 0.05)

    def _stall(path, mime):
        import time

        time.sleep(0.5)
        return camera.CameraInfo(make="late")

    monkeypatch.setattr(uploads.camera, "read_camera", _stall)
    try:
        data = fx.full_jpeg(tmp_path / "src.jpg").read_bytes()
        uid = _post(client, "full.jpg", "image/jpeg", data)
        assert _stored(uid)["camera_source"] == "unreadable"
        assert _stored(uid)["camera_make"] is None
    finally:
        _cleanup()


def test_a_video_upload_is_classified_not_image_without_a_read(
    logged_in_client, monkeypatch, tmp_path
):
    from app.services import uploads

    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(uploads.camera, "read_camera", lambda p, m: calls.append(m))
    try:
        mov = Path(__file__).resolve().parent.parent / "fixtures/locations/mov-iso6709.mov"
        uid = _post(client, "clip.mov", "video/quicktime", mov.read_bytes())
        assert _stored(uid)["camera_source"] == "not-image"
        assert calls == []
    finally:
        _cleanup()


# --- the detail serves `camera` only when the ledger says 'exif' -----------------------------


def _set_camera(aid: int, source: str | None) -> None:
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(
            text(
                "UPDATE attachments SET camera_make='ACME', camera_model='M', iso=200,"
                " f_number=1.8, camera_source=:s WHERE id=:a"
            ),
            {"s": source, "a": aid},
        )
    eng.dispose()


@pytest.mark.parametrize("source", [None, "none", "unreadable", "not-image"])
def test_the_detail_withholds_camera_unless_the_ledger_says_exif(source, logged_in_client):
    client, subject = logged_in_client
    aid = _seed_attachment(f"att-cam-{source}", subject)
    try:
        _set_camera(aid, source)
        body = client.get(f"/api/attachments/att-cam-{source}").json()
        assert "camera" in body
        assert body["camera"] is None
    finally:
        _cleanup_ids(aid)


def test_the_detail_serves_camera_when_the_ledger_says_exif(logged_in_client):
    client, subject = logged_in_client
    aid = _seed_attachment("att-cam-exif", subject)
    try:
        _set_camera(aid, camera.SOURCE_EXIF)
        body = client.get("/api/attachments/att-cam-exif").json()
        assert body["camera"] == {
            "make": "ACME",
            "model": "M",
            "lensModel": None,
            "fNumber": 1.8,
            "exposureTimeS": None,
            "focalLengthMm": None,
            "exposureBiasEv": None,
            "iso": 200,
        }
    finally:
        _cleanup_ids(aid)
