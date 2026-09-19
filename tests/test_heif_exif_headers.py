"""The HEIC/HEIF header read (MEMNG-341): an Exif item costs its own extent, not the file.

`capture.heif_exif_from` walks the ISO-BMFF boxes and reads ONE extent — the Exif item's — so a
multi-megabyte original on an NFS mount costs a header read like every other container this repo
reads. Opening the same file with libheif parses the whole thing, which is what used to put the
HEIC leg of the location backfill (and its 5 s `CAPTURE_READ_TIMEOUT_S`) at the mercy of the mount.

The fixtures are built here at runtime by `scripts/gen_location_fixtures.py`, not committed: the
large one is megabytes of filler by design, and the rest differ from the committed
`fixtures/locations/heic-gps.heic` only in HOW the same payload is addressed. Every expectation
below comes from `fixtures/locations.json`, from `fixtures/auto-memo-date.json`, or from what
Pillow independently finds in the same file — never from a value this module built for itself.
"""

import json
from functools import partial
from pathlib import Path

import pytest
from app.services import capture, location
from PIL import Image
from structlog.testing import capture_logs

from scripts import gen_location_fixtures as gen

REPO = Path(__file__).resolve().parent.parent
BINARIES = REPO / "fixtures" / "locations"
LOCATIONS = json.loads((REPO / "fixtures/locations.json").read_text())
DATES = json.loads((REPO / "fixtures/auto-memo-date.json").read_text())

HEIC_MIME = "image/heic"
# What the committed HEIC promises; every variant below is that same file re-laid-out, so it is
# also what every one of them must still answer.
EXPECTED = LOCATIONS["binaries"]["heic-gps.heic"]["expect"]
# The coordinate as a log line would spell it — the privacy rule of MEMNG-262 applies to the new
# fallback event too.
SPELLINGS = ("20.0", "150.0", "-150.0")


@pytest.fixture(scope="module")
def heifs(tmp_path_factory) -> dict[str, Path]:
    """One build of each variant for the whole module — the large one is 4 MB of filler."""
    out = tmp_path_factory.mktemp("heif")
    built = {
        "large": gen.heic_gps_large,
        "idat": gen.heic_gps_idat,
        "item-offset": gen.heic_gps_item_offset,
        "external": gen.heic_gps_external,
        "overlong": gen.heic_gps_overlong_exif,
    }
    paths = {}
    for name, build in built.items():
        paths[name] = out / f"{name}.heic"
        build(paths[name])
    for version in (0, 1, 2):
        paths[f"iloc{version}"] = out / f"iloc{version}.heic"
        gen.heic_gps_iloc(paths[f"iloc{version}"], version)
    return paths


def _read(path: Path) -> dict[str, object]:
    """What the location reader answers, in the artefact's own shape."""
    result = location.read_location(path, HEIC_MIME)
    if result is None:
        return {"latitude": None, "longitude": None, "location_source": location.SOURCE_NONE}
    return {
        "latitude": result.latitude,
        "longitude": result.longitude,
        "location_source": result.source,
    }


def _read_avif(path: Path) -> dict[str, object]:
    """`_read`, for the mime the AVIF cases below are read under."""
    result = location.read_location(path, AVIF_MIME)
    if result is None:
        return {"latitude": None, "longitude": None, "location_source": location.SOURCE_NONE}
    return {
        "latitude": result.latitude,
        "longitude": result.longitude,
        "location_source": result.source,
    }


def _walk(path: Path) -> bytes | None:
    with open(path, "rb") as f:
        return capture.heif_exif_from(f)


# --------------------------------------------------------------------------------------------
# The reason the walker exists
# --------------------------------------------------------------------------------------------


def test_a_multi_megabyte_heic_costs_a_header_read_not_the_file(heifs):
    # THE acceptance probe. Before this change the same read metered the whole file, because
    # pillow-heif parses every byte an open touches — over NFS that is what tripped the 5 s
    # CAPTURE_READ_TIMEOUT_S the upload and the backfill both read under.
    path = heifs["large"]
    size = path.stat().st_size
    assert size > 4 * 1024 * 1024, "the fixture has to be big enough for the claim to mean anything"

    with location.measuring() as meter:
        answer = _read(path)

    assert answer == EXPECTED
    assert meter.bytes_read < 64 * 1024, f"read {meter.bytes_read} bytes of a {size}-byte HEIC"


def test_the_filler_megabytes_are_seeked_over_and_never_read(heifs):
    # The bound above is generous on purpose; this one is the mechanism. What a header read may
    # cost is the `meta` box plus the one Exif extent, and nothing may scale with the `mdat`.
    small, large = heifs["iloc0"], heifs["large"]
    with location.measuring() as small_meter:
        _read(small)
    with location.measuring() as large_meter:
        _read(large)
    grown = large.stat().st_size - small.stat().st_size
    assert grown > 4 * 1024 * 1024
    assert large_meter.bytes_read - small_meter.bytes_read < 1024, (
        f"{grown} bytes of filler cost {large_meter.bytes_read - small_meter.bytes_read} extra"
    )


# --------------------------------------------------------------------------------------------
# The payload the walker hands over is the one Pillow would have found
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["iloc0", "iloc1", "iloc2", "large"])
def test_the_walker_hands_over_exactly_the_payload_pillow_finds(heifs, name):
    # The oracle is libheif's own answer for the same file, not a value written down here: the
    # contract is "the same bytes, cheaper", and only an independent reader can say so.
    with Image.open(heifs[name]) as im:
        expected = im.info.get("exif")
    assert expected, "the fixture must carry an Exif item for this to prove anything"
    assert _walk(heifs[name]) == expected


@pytest.mark.parametrize("version", [0, 1, 2])
def test_every_iloc_version_reads_the_same_location(heifs, version):
    # v0 has no construction_method, v1 adds it, v2 widens the item ID and the item count. All
    # three are legal and all three occur; a walker that reads the later ones with the v0 field
    # widths lands mid-field and answers nonsense.
    path = heifs[f"iloc{version}"]
    assert _walk(path) is not None
    assert _read(path) == EXPECTED


def test_the_capture_date_of_the_primary_item_is_unchanged(heifs):
    # `heic-offset.heic` carries FOUR items: two images and two Exif items, with the primary one
    # second and a 2019 decoy first. A walker that takes the first Exif item it sees reads the
    # decoy, so this is the case that pins item selection to `pitm` + `iref`.
    name = "heic-offset.heic"
    spec = DATES["binaries"][name]
    path = REPO / "fixtures" / "auto-memo-date" / name
    with Image.open(path) as im:
        assert _walk(path) == im.info.get("exif")
    result = capture.read_capture(path, spec["mime"])
    assert {
        "capture_instant": None
        if result.capture_instant is None
        else result.capture_instant.isoformat().replace("+00:00", "Z"),
        "capture_civil": result.capture_civil,
        "capture_source": result.capture_source,
    } == spec["expect"]


# --------------------------------------------------------------------------------------------
# Structural doubt is declined, and the full read is what answers instead
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["idat", "item-offset", "external"])
def test_an_extent_the_walker_cannot_address_is_declined(heifs, name):
    # construction_method 1 addresses the payload inside `idat`, method 2 inside another item, and
    # a non-zero data_reference_index puts it in a different file. Following any of the three from
    # a file offset reads unrelated bytes; None hands the file to the reader that can.
    assert _walk(heifs[name]) is None


@pytest.mark.parametrize("name", ["idat", "external"])
def test_a_declined_extent_still_reads_its_location_through_the_fallback(heifs, name):
    with capture_logs() as logs:
        assert _read(heifs[name]) == EXPECTED
    events = [entry for entry in logs if entry.get("event") == "location.heif_fallback"]
    assert len(events) == 1, logs
    assert events[0]["mime"] == HEIC_MIME
    rendered = json.dumps(logs)
    for spelling in SPELLINGS:
        assert spelling not in rendered, f"the fallback event spells out {spelling}: {rendered}"
    # Positive control for that search: it does find a coordinate when one is really there.
    assert any(s in json.dumps([{"event": "probe", "latitude": -150.0}]) for s in SPELLINGS)


def test_the_capture_read_logs_its_own_fallback(heifs):
    with capture_logs() as logs:
        result = capture.read_capture(heifs["idat"], HEIC_MIME)
    assert result == capture.NOTHING  # the fixture carries GPS, not a date
    events = [entry for entry in logs if entry.get("event") == "capture.heif_fallback"]
    assert len(events) == 1, logs
    assert events[0]["mime"] == HEIC_MIME


@pytest.mark.parametrize("name", ["iloc0", "iloc1", "iloc2", "large"])
def test_a_file_the_headers_can_address_logs_no_fallback_at_all(heifs, name):
    # The positive control for the cases above, and the guard on the measurement: the event fires
    # on the fallback and only there, so its ABSENCE here is what says the cheap numbers were
    # measured on the header path rather than on a full read that quietly answered instead.
    with capture_logs() as logs:
        assert _read(heifs[name]) == EXPECTED
        capture.read_capture(heifs[name], HEIC_MIME)
    assert [entry for entry in logs if "heif_fallback" in str(entry.get("event"))] == []


# --------------------------------------------------------------------------------------------
# Whose metadata is it — and a structure that cannot be trusted at all
# --------------------------------------------------------------------------------------------


def test_a_lone_exif_item_describing_a_secondary_image_is_declined(tmp_path):
    # One Exif item in the file does NOT make it the primary's: `heic-offset.heic` carries two
    # images, and retyping the entry that describes the primary leaves a single Exif belonging to
    # the image nobody asked for. libheif reports no Exif for the primary at all, so a walker that
    # hands those bytes over does not merely cost a fallback — it dates the memo from the wrong
    # picture (2019 instead of nothing), and a returned value is past every fallback there is.
    path = tmp_path / "secondary.heic"
    gen.heic_exif_describes_secondary(path)
    with Image.open(path) as im:
        assert im.info.get("exif") is None, "the fixture must leave the primary without an Exif"
    assert _walk(path) is None

    with capture_logs() as logs:
        assert capture.read_capture(path, HEIC_MIME) == capture.NOTHING  # what Pillow answers
    assert [entry for entry in logs if entry.get("event") == "capture.heif_fallback"]


@pytest.mark.parametrize(
    ("version", "index_size"), [(1, 0), (1, 4), (1, 8), (2, 0), (2, 4), (2, 8)]
)
def test_an_extent_index_does_not_move_the_declared_length(tmp_path, version, index_size):
    # Versions 1 and 2 may put an `extent_index` before each extent's offset and length, and its
    # width is declared in a nibble. Measuring the length from the wrong end of it reads four or
    # eight bytes too far: a declared 32 followed by the next box decodes as 137438953480, which
    # the cap then declines — the whole HEIC library falls back, silently and for good.
    path = tmp_path / f"index-{version}-{index_size}.heic"
    gen.heic_gps_index_size(path, version, index_size)
    with Image.open(path) as im:
        expected = im.info.get("exif")
    assert expected, "the fixture must carry an Exif item for this to prove anything"

    walked = _walk(path)
    assert walked == expected
    assert len(walked or b"") == len(expected)  # the LENGTH is the field that mis-decodes


@pytest.mark.parametrize("damage", [*sorted(gen.DAMAGES), "infe-overrun"])
def test_a_structure_the_walker_cannot_trust_is_declined(tmp_path, damage):
    # An undefined FullBox version, an entry count that does not match the entries, a child box
    # running past its parent: each leaves a table that is partly guesswork, and a reader that
    # answers from one has already decided which half of a malformed file to believe. The rule is
    # the same for all of them — hand the file to the reader that can judge it whole.
    path = tmp_path / f"{damage}.heic"
    if damage == "infe-overrun":
        gen.heic_gps_infe_overrun(path)
    else:
        gen.heic_gps_damaged(path, damage)
    assert _walk(path) is None

    # And the file's fate stays whatever the FULL read makes of it, which is the fate it had
    # before this walk existed: an answer where libheif can still read the file, a raise where it
    # cannot — never the walker's guess in either case.
    try:
        with Image.open(path) as im:
            pillow_reads_it = im.info.get("exif") is not None
    except OSError:
        pillow_reads_it = False
    if pillow_reads_it:
        assert _read(path) == EXPECTED
    else:
        with pytest.raises(OSError):
            location.read_location(path, HEIC_MIME)


TABLE_DAMAGES = {
    "reference-over-counted-after-the-match": gen.heic_gps_overcounted_reference,
    "reference-over-counted-before-it": partial(gen.heic_gps_overcounted_reference, first=True),
    "iloc-short-of-its-count-after-the-match": gen.heic_gps_overcounted_iloc,
    "iloc-short-of-its-count-before-it": partial(gen.heic_gps_overcounted_iloc, omit_exif=True),
}


@pytest.mark.parametrize("case", sorted(TABLE_DAMAGES))
def test_the_whole_table_is_validated_before_a_match_in_it_is_believed(tmp_path, case):
    # A match found halfway through a table says nothing about the rest of it. Both walks used to
    # answer on the first hit, so a file whose SECOND reference over-counts, or whose `iloc`
    # declares more items than it writes, came back with Exif bytes while libheif refused the file
    # outright — an unreadable original turned into a successful metadata read, and the fallback
    # that would have raised was never reached. The two "BEFORE it" cases are the controls: any
    # walk at all meets that damage, so they were already declined.
    path = tmp_path / "table.heic"
    TABLE_DAMAGES[case](path)
    assert _walk(path) is None

    with pytest.raises(OSError) as refused:
        with Image.open(path):
            pass  # pragma: no cover — the open is what raises
    # The file's fate stays the one the full read gives it, down to the exception class.
    with pytest.raises(type(refused.value)):
        location.read_location(path, HEIC_MIME)


def test_a_truncated_meta_is_declined_and_the_file_stays_unreadable(heifs, tmp_path):
    # Cut inside the `meta` box: there is no Exif item to find and no file to fall back onto
    # either. The answer must stay the one the caller settles as 'unreadable' and retries — a
    # None here would write a damaged original off as "carries no location" forever.
    broken = tmp_path / "truncated.heic"
    broken.write_bytes(heifs["iloc0"].read_bytes()[:200])
    assert _walk(broken) is None
    with pytest.raises(OSError):
        location.read_location(broken, HEIC_MIME)


def test_bytes_that_are_no_container_are_declined(tmp_path):
    junk = tmp_path / "junk.heic"
    junk.write_bytes(b"\x00" * 64)
    assert _walk(junk) is None


def test_an_exif_item_longer_than_the_cap_is_declined(heifs):
    # The cap against the REAL constant: the fixture's `iloc` claims a 3 MB extent at a real
    # offset, with 3 MB of filler behind the payload so that believing it would SUCCEED. That is
    # the whole hazard — a corrupt item table is answered with an allocation, not an error.
    path = heifs["overlong"]
    assert path.stat().st_size > capture.HEIF_EXIF_READ_CAP
    assert _walk(path) is None


def test_a_meta_box_longer_than_the_cap_is_declined(heifs, monkeypatch):
    # The same cap bounds the OTHER read this walk performs — the `meta` box it parses in memory.
    # Shrunk here below any real item table, so the branch is exercised without authoring one.
    monkeypatch.setattr(capture, "HEIF_EXIF_READ_CAP", 8)
    assert _walk(heifs["iloc0"]) is None
    assert _read(heifs["iloc0"]) == EXPECTED  # and the full read still answers


def test_the_cap_is_of_the_same_order_as_the_other_metadata_caps():
    # A cap that drifted to gigabytes would be no cap at all; one below a real Exif payload would
    # send every HEIC down the slow path silently.
    assert 64 * 1024 <= capture.HEIF_EXIF_READ_CAP <= capture.MOOV_READ_CAP


# --------------------------------------------------------------------------------------------
# AVIF — the same container, the same walk
# --------------------------------------------------------------------------------------------

AVIF_MIME = "image/avif"
AVIF_EXPECTED = LOCATIONS["binaries"]["avif-gps.avif"]["expect"]
AVIF_BINARY = BINARIES / "avif-gps.avif"
AVIF_NO_EXIF = BINARIES / "avif-no-gps.avif"


@pytest.fixture(scope="module")
def avifs(tmp_path_factory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("avif")
    built = {
        "large": gen.avif_gps_large,
        "dated": gen.avif_gps_dated,
        "heic-no-exif": gen.heic_no_gps,
    }
    paths = {}
    for name, build in built.items():
        paths[name] = out / f"{name}.avif"
        build(paths[name])
    return paths


class _OpenSpy:
    """The REAL `Image.open`, counting its calls. Not a stand-in: it opens the file.

    The fallback is what this feature exists to skip, and "it was skipped" is a claim about the
    DECISION, not about the answer — the full read returns the same coordinates, so an end-state
    assertion passes just as loudly on a file that took the slow path all the way.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.calls: list[object] = []

    def __call__(self, fp, *args, **kwargs):
        self.calls.append(fp)
        return self._real(fp, *args, **kwargs)


@pytest.fixture
def open_spy(monkeypatch) -> _OpenSpy:
    spy = _OpenSpy(Image.open)
    monkeypatch.setattr(location.Image, "open", spy)
    monkeypatch.setattr(capture.Image, "open", spy)
    return spy


def test_an_avif_with_exif_never_reaches_the_full_decode(open_spy):
    # (b) of the acceptance: the decision point. `location._read_image` and `capture._read_image`
    # reach `Image.open` only when the walk declined, so a zero here IS "the fallback was not
    # called" — and the no-Exif file below is the positive control that the spy can count at all.
    assert _read_avif(AVIF_BINARY) == AVIF_EXPECTED
    assert capture.read_capture(AVIF_BINARY, AVIF_MIME) == capture.NOTHING
    assert open_spy.calls == []

    location.read_location(AVIF_NO_EXIF, AVIF_MIME)
    assert len(open_spy.calls) == 1


def test_an_avif_answers_what_the_full_decode_answers_for_the_same_file(avifs, monkeypatch):
    # (a): the oracle is the SAME file read the slow way — the walk is only allowed to be cheaper.
    dated = avifs["dated"]
    walked_location, walked_capture = _read_avif(dated), capture.read_capture(dated, AVIF_MIME)
    assert walked_capture.capture_instant is not None  # else "equal" would be nothing == nothing

    monkeypatch.setattr(capture, "heif_exif_from", lambda f: None)  # force the fallback leg
    assert _read_avif(dated) == walked_location
    assert capture.read_capture(dated, AVIF_MIME) == walked_capture


def test_the_walker_hands_over_exactly_the_payload_pillow_finds_for_an_avif(avifs):
    # libheif has no AVIF codec in this build; Pillow's own libavif is the independent reader.
    for path in (AVIF_BINARY, avifs["large"], avifs["dated"]):
        with Image.open(path) as im:
            expected = im.info.get("exif")
        assert expected, f"{path.name} must carry an Exif item for this to prove anything"
        assert _walk(path) == expected


def test_a_multi_kilobyte_avif_costs_a_header_read_not_the_file(avifs):
    # (c), the same claim the HEIC case makes about its filler: what a header read may cost is the
    # `meta` box plus the one Exif extent, and nothing may scale with the image payload.
    large = avifs["large"]
    size = large.stat().st_size
    assert size > 32 * 1024, "the fixture has to be big enough for the claim to mean anything"

    with location.measuring() as small_meter:
        assert _read_avif(AVIF_BINARY) == AVIF_EXPECTED
    with location.measuring() as large_meter:
        assert _read_avif(large) == AVIF_EXPECTED
    assert large_meter.bytes_read < size // 16, f"read {large_meter.bytes_read} of {size} bytes"
    grown = size - AVIF_BINARY.stat().st_size
    assert large_meter.bytes_read - small_meter.bytes_read < 1024, (
        f"{grown} bytes of image cost {large_meter.bytes_read - small_meter.bytes_read} extra"
    )


def test_an_avif_without_exif_ends_exactly_where_a_heic_without_exif_ends(avifs, open_spy):
    # The other category. Neither file HAS an Exif item, so both are declined by the walk, both
    # log their fallback, and both let the full read answer — the AVIF must not be a special case
    # in either direction (a silent None, or an answer the HEIC does not give).
    heic = avifs["heic-no-exif"]
    answers = []
    for path, mime in ((AVIF_NO_EXIF, AVIF_MIME), (heic, HEIC_MIME)):
        assert _walk(path) is None
        with capture_logs() as logs:
            answers.append(
                (
                    location.read_location(path, mime),
                    capture.read_capture(path, mime),
                    sorted(e["event"] for e in logs if "heif_fallback" in str(e.get("event"))),
                )
            )
    fell_back = ["capture.heif_fallback", "location.heif_fallback"]
    assert answers[0] == (None, capture.NOTHING, fell_back)
    assert answers[0] == answers[1]
    assert len(open_spy.calls) == 4  # both files, both readers: every one of them fell back


def test_a_non_heif_image_never_goes_near_the_walker(monkeypatch):
    # The walk is entered on the mime, so a JPEG must reach the same answer it always did — and
    # the fixture's own artefact entry is what "always did" means.
    def _never(*args, **kwargs):
        raise AssertionError("a JPEG was walked as a HEIF")

    monkeypatch.setattr(capture, "heif_exif_from", _never)
    name = "jpeg-north-east.jpg"
    result = location.read_location(BINARIES / name, "image/jpeg")
    assert result is not None
    assert (result.latitude, result.longitude) == (
        LOCATIONS["binaries"][name]["expect"]["latitude"],
        LOCATIONS["binaries"][name]["expect"]["longitude"],
    )
