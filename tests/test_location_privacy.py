"""The privacy invariant of Places, enforced where each leak would happen.

A coordinate reaches Postgres and `GET /api/map/markers` and NOTHING else. The causes are
enumerated here as cases rather than asserted as a principle, because each one is a different
mechanism and each has its own guard:

* a LOG LINE — structlog events carry counts and sources, never values (MEMNG-262 kept the PIL
  loggers at INFO for the same reason);
* a DERIVED IMAGE TIER or PRINT — `media._date_only_exif` rebuilds a whitelist EXIF rather than
  filtering the source's, so a GPS IFD cannot survive it;
* a TRANSCODED WEB VIDEO or POSTER — `-map_metadata -1 -map_chapters -1` on every ffmpeg command
  this repo runs (a pre-existing gap: the transcode used to carry its source's location tags);
* an UPLOADED POSTER OVERRIDE — a client JPEG that used to be copied byte-for-byte onto a derived
  path, and is now re-encoded through the same date-only save as every other derived asset;
* ANOTHER API BODY — `/api/memos` and `/api/attachments/{uid}` never carry a coordinate;
* the `uploads` TABLE — an upload session must never hold a location at all.

Every coordinate used here comes from `fixtures/locations.json`, and every coordinate there is
fictional (open ocean) — see `scripts/gen_location_fixtures.py`.
"""

import io
import json
import subprocess
from pathlib import Path

import pytest
from app.config import settings
from app.services import capture, location, media
from PIL import Image
from sqlalchemy import create_engine, inspect, text
from structlog.testing import capture_logs

from tests.conftest import SYNC_URL

REPO = Path(__file__).resolve().parent.parent
BINARIES = REPO / "fixtures" / "locations"
ARTEFACT = json.loads((REPO / "fixtures/locations.json").read_text())

GPS_IFD = 0x8825

# Every coordinate the fixtures carry, as the substrings a leak would spell them with. "12.5" and
# "12°30'" are the same location; a log line or a body carrying either is the failure.
LEAKED_SPELLINGS = sorted(
    {
        piece
        for spec in ARTEFACT["binaries"].values()
        for value in (spec["expect"]["latitude"], spec["expect"]["longitude"])
        if value is not None
        for piece in (f"{value}", f"{abs(value)}", f"{int(abs(value))}")
        if len(piece) >= 4  # "12" alone is in every timestamp; only a decimal spelling is evidence
    }
)


def _gps_ifd(path: Path) -> dict:
    with Image.open(path) as im:
        return dict(im.getexif().get_ifd(GPS_IFD))


def _ffprobe_tags(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-print_format",
            "json",
            str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    probed = json.loads(out)
    tags = dict(probed.get("format", {}).get("tags", {}))
    for stream in probed.get("streams", []):
        tags.update(stream.get("tags", {}))
    return tags


def _assert_no_location_anywhere(path: Path) -> None:
    """The moov reader AND ffprobe — two readers, because either one could be the blind one."""
    moov = capture.read_moov(path)
    assert moov is not None, f"{path} has no moov box to inspect"
    assert location._iso6709_from_moov(moov) is None
    tags = _ffprobe_tags(path)
    offenders = [k for k in tags if "location" in k.lower() or "gps" in k.lower()]
    assert offenders == [], f"{path} carries {offenders}: {[tags[k] for k in offenders]}"


# --------------------------------------------------------------------------------------------
# Derived image tiers and prints
# --------------------------------------------------------------------------------------------


def test_the_date_only_exif_of_a_geotagged_photo_carries_no_gps():
    # `_date_only_exif` is CONSTRUCTED, not filtered — this pins that a GPS-bearing source yields
    # an EXIF block with no GPS IFD rather than one that merely happens to be empty today.
    with Image.open(BINARIES / "jpeg-north-east.jpg") as raw:
        assert dict(raw.getexif().get_ifd(GPS_IFD)) != {}  # the source really is geotagged
        rebuilt = media._date_only_exif(raw)
    if rebuilt is None:
        return  # the fixture carries no date tags, so there is no EXIF block at all: also GPS-free
    parsed = Image.Exif()
    parsed.load(rebuilt)
    assert dict(parsed.get_ifd(GPS_IFD)) == {}


def test_a_thumb_derived_from_a_geotagged_photo_has_an_empty_gps_ifd(tmp_path, monkeypatch, adb):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    from app.models import Attachment

    source = tmp_path / "geo.jpg"
    source.write_bytes((BINARIES / "jpeg-north-east.jpg").read_bytes())
    att = Attachment(uid="thumbgps", mime="image/jpeg", storage_path=str(source))
    media.process_image(att)
    thumb = tmp_path / "assets" / "thumbs" / "thumbgps.jpg"
    assert thumb.exists()
    assert _gps_ifd(thumb) == {}


# --------------------------------------------------------------------------------------------
# ffmpeg: the web tier and the derived poster
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["mov-iso6709.mov", "mp4-xyz.mp4"])
def test_the_fixture_clip_really_carries_a_location(name):
    # The positive control for the two cases below: a transcode that dropped a tag the source
    # never had would prove nothing at all.
    moov = capture.read_moov(BINARIES / name)
    assert moov is not None
    assert location._iso6709_from_moov(moov) is not None


@pytest.mark.parametrize("name", ["mov-iso6709.mov", "mp4-xyz.mp4"])
def test_the_transcoded_web_tier_of_a_located_clip_carries_no_location(tmp_path, name):
    out = tmp_path / "web.mp4"
    subprocess.run(
        media.slow_ffmpeg_cmd(str(BINARIES / name), str(out), 1),
        capture_output=True,
        check=True,
        timeout=120,
    )
    _assert_no_location_anywhere(out)


@pytest.mark.parametrize("name", ["mov-iso6709.mov", "mp4-xyz.mp4"])
def test_the_derived_poster_of_a_located_clip_carries_no_location(tmp_path, name):
    out = tmp_path / "poster.jpg"
    subprocess.run(
        media.poster_ffmpeg_cmd(str(BINARIES / name), str(out), 0.5),
        capture_output=True,
        check=True,
        timeout=120,
    )
    assert _gps_ifd(out) == {}


def test_every_ffmpeg_command_this_repo_runs_strips_metadata():
    # Enumerated rather than spot-checked: a new lane added without the flags is the regression.
    for cmd in (
        media.slow_ffmpeg_cmd("/in.mov", "/out.mp4", 1),
        media.poster_ffmpeg_cmd("/in.mov", "/out.jpg", 100.0),
        media.poster_ffmpeg_cmd("/in.mov", "/out.jpg", 100.0, colour={"color_transfer": "bt709"}),
    ):
        assert cmd[cmd.index("-map_metadata") + 1] == "-1"
        assert cmd[cmd.index("-map_chapters") + 1] == "-1"


# --------------------------------------------------------------------------------------------
# The uploaded poster override — a derived asset that used to be a byte copy
# --------------------------------------------------------------------------------------------


def test_a_poster_override_is_sanitized_rather_than_copied(tmp_path):
    src = BINARIES / "jpeg-north-east.jpg"
    dest = tmp_path / "out" / "x.poster.jpg"
    tmp = tmp_path / "out" / "x.poster.tmp.jpg"
    media.sanitize_poster_override(src, dest, tmp)
    assert dest.exists()
    assert not tmp.exists()
    assert dest.read_bytes() != src.read_bytes(), "a byte copy would carry the source's GPS IFD"
    assert _gps_ifd(dest) == {}
    with Image.open(dest) as published, Image.open(src) as original:
        assert published.size == original.size  # the client's picture, not a different one


# --------------------------------------------------------------------------------------------
# Other API bodies, the uploads table, and the logs
# --------------------------------------------------------------------------------------------


def _cleanup():
    eng = create_engine(SYNC_URL)
    with eng.begin() as c:
        c.execute(text("DELETE FROM frame_share_jobs"))
        c.execute(text("DELETE FROM media_jobs"))
        c.execute(text("DELETE FROM attachments"))
        c.execute(text("DELETE FROM uploads"))
    eng.dispose()


def _upload(client, name, mime, data: bytes) -> str:
    r = client.post(
        "/api/uploads",
        json={"filename": name, "mime": mime, "size": len(data), "last_modified": 1609459200000},
    )
    assert r.status_code == 201, r.text
    uid = r.json()["upload_uid"]
    r = client.patch(f"/api/uploads/{uid}", content=data, headers={"Upload-Offset": "0"})
    assert r.status_code == 200, r.text
    return uid


def test_a_poster_override_uploaded_through_the_api_is_served_without_its_gps(
    logged_in_client, monkeypatch, tmp_path
):
    # The end-to-end case for the override lane: a client's own JPEG, which carries whatever its
    # camera wrote, becomes a DERIVED tier. Driven through the real upload + create routes and
    # read back from the SERVED poster URL, because the leak would be in what is served, not in
    # what a helper returns.
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        located_jpeg = (BINARIES / "jpeg-north-east.jpg").read_bytes()
        assert _gps_ifd(BINARIES / "jpeg-north-east.jpg"), "the source must carry GPS to prove this"

        video_uid = _upload(client, "clip.mov", "video/quicktime", bytes(range(256)) * 400)
        poster_uid = _upload(client, "title.jpg", "image/jpeg", located_jpeg)
        created = client.post(
            "/api/memos",
            json={
                "visibility": "PUBLIC",
                "content": "the monthly video",
                "upload_uids": [video_uid],
                "video_posters": {video_uid: poster_uid},
            },
        )
        assert created.status_code == 201, created.text
        [attachment] = created.json()["attachments"]

        served = client.get(attachment["posterUrl"])
        assert served.status_code == 200, served.text
        assert served.headers["content-type"].startswith("image/")
        with Image.open(io.BytesIO(served.content)) as poster:
            assert dict(poster.getexif().get_ifd(GPS_IFD)) == {}
        for spelling in LEAKED_SPELLINGS:
            assert spelling.encode() not in served.content
    finally:
        _cleanup()


def test_no_other_api_body_and_no_log_line_carries_a_coordinate(
    logged_in_client, monkeypatch, tmp_path
):
    client, _ = logged_in_client
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    try:
        payload = (BINARIES / "jpeg-north-east.jpg").read_bytes()
        with capture_logs() as logs:
            upload_uid = _upload(client, "geo.jpg", "image/jpeg", payload)
            created = client.post(
                "/api/memos",
                json={"visibility": "PUBLIC", "content": "by the sea", "upload_uids": [upload_uid]},
            )
        assert created.status_code == 201, created.text
        memo = created.json()
        attachment_uid = memo["attachments"][0]["uid"]

        # The row DOES hold the coordinate — otherwise the rest of this case proves nothing.
        eng = create_engine(SYNC_URL)
        with eng.connect() as c:
            stored = c.execute(
                text("SELECT latitude, longitude, location_source FROM attachments WHERE uid = :u"),
                {"u": attachment_uid},
            ).one()
        eng.dispose()
        assert (stored[0], stored[1], stored[2]) == (12.5, 45.25, "exif")

        rendered = json.dumps(logs)
        for spelling in LEAKED_SPELLINGS:
            assert spelling not in rendered, f"a log event spells out {spelling}: {rendered[:400]}"
        # Positive control for the grep: the same search DOES find a coordinate when one is there.
        assert any(
            s in json.dumps([{"event": "probe", "latitude": 12.5}]) for s in LEAKED_SPELLINGS
        )

        for body in (
            client.get("/api/memos").text,
            client.get(f"/api/attachments/{attachment_uid}").text,
        ):
            assert "latitude" not in body
            assert "longitude" not in body
            assert "location_source" not in body
            for spelling in LEAKED_SPELLINGS:
                assert spelling not in body
    finally:
        _cleanup()


def test_the_uploads_table_gained_no_location_column(db_engine):
    columns = {c["name"] for c in inspect(db_engine).get_columns("uploads")}
    assert {"latitude", "longitude", "location_source"} & columns == set()
    attachment_columns = {c["name"] for c in inspect(db_engine).get_columns("attachments")}
    assert {"latitude", "longitude", "location_source"} <= attachment_columns
