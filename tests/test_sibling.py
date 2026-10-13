"""The pure half of the sibling lookup (MEMNG-796): stem derivation, election, and the HTTP answer
classification of `lookup`.

`lookup` is driven through an `httpx.MockTransport` standing in for img-manager — the dependency,
not the unit: the request it builds and the status handling it applies are the real code.
"""

import json
from datetime import UTC, datetime

import httpx
import pytest
from app.config import settings
from app.services import sibling
from app.services.sibling import Candidate, SiblingUnavailable, derive_stems, elect


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("_ARA8207 - screen.png", ["_ARA8207"]),
        ("_AR53945.png", ["_AR53945"]),
        ("_AR5123-Edit - web.png", ["_AR5123-Edit", "_AR5123"]),
        ("_AR5123-2.png", ["_AR5123-2", "_AR5123"]),
        ("a - b - c.png", ["a"]),
        ("DSCF0001.JPG", ["DSCF0001"]),
        ("noext", ["noext"]),
        ("", []),
        (None, []),
    ],
)
def test_derive_stems_vectors(filename, expected):
    assert derive_stems(filename) == expected


def test_derive_stems_of_a_name_that_is_only_an_extension_is_empty():
    # Boundary: stripping the extension leaves nothing to ask about — never an empty-stem request.
    assert derive_stems(".png") == []
    assert derive_stems(" - x.png") == []


def test_a_lightroom_token_inside_the_stem_is_not_a_suffix():
    # Literal-in-string: "-Edit" and " - " are only Lightroom's when they END the stem / START the
    # export suffix; the same characters inside a camera name are part of the name.
    assert derive_stems("_AR-Edit-x.png") == ["_AR-Edit-x"]
    assert derive_stems("_AR-2x - web.png") == ["_AR-2x"]


def _cand(image_id: int, *, pick: bool = False) -> Candidate:
    return Candidate(
        image_id=image_id,
        datetime_original=datetime(2024, 1, image_id, 12, tzinfo=UTC),
        gps_latitude=None,
        gps_longitude=None,
        is_pick=pick,
    )


def test_elect_one_pick_among_several_is_unique_and_names_the_pick():
    chosen = _cand(2, pick=True)
    election = elect([_cand(1), chosen, _cand(3)])
    assert election.outcome == "unique"
    assert election.chosen == chosen


def test_elect_two_picks_is_ambiguous():
    election = elect([_cand(1, pick=True), _cand(2, pick=True), _cand(3)])
    assert election.outcome == "ambiguous"
    assert election.chosen is None


def test_elect_no_pick_and_one_candidate_is_unique():
    only = _cand(4)
    election = elect([only])
    assert election.outcome == "unique"
    assert election.chosen == only


def test_elect_no_pick_and_two_candidates_is_ambiguous():
    election = elect([_cand(1), _cand(2)])
    assert election.outcome == "ambiguous"
    assert election.chosen is None


def test_elect_a_truncated_list_is_ambiguous_even_with_one_pick():
    assert elect([_cand(1, pick=True), _cand(2)], truncated=True).outcome == "ambiguous"
    assert elect([_cand(1, pick=True), _cand(2)], truncated=False).outcome == "unique"


def test_elect_empty_is_none():
    election = elect([])
    assert election.outcome == "none"
    assert election.chosen is None


# MEMNG-797: the same RAW held in both img-manager tiers answers as two candidates that agree on
# everything; agreement on the second and on the exact GPS pair is a consensus, never a guess.
AGREED_AT = datetime(2023, 7, 14, 9, 30, 15, tzinfo=UTC)


def _twin(
    image_id: int,
    *,
    pick: bool = False,
    at: datetime | None = AGREED_AT,
    lat: float | None = 46.5,
    lon: float | None = 7.25,
) -> Candidate:
    return Candidate(
        image_id=image_id,
        datetime_original=at,
        gps_latitude=lat,
        gps_longitude=lon,
        is_pick=pick,
    )


def test_elect_two_agreeing_candidates_without_a_pick_is_consensus_on_the_first():
    first, second = _twin(1), _twin(2)
    election = elect([first, second])
    assert election.outcome == "consensus"
    assert election.chosen == first


def test_elect_agreeing_candidates_with_no_gps_at_all_is_consensus():
    first = _twin(1, lat=None, lon=None)
    election = elect([first, _twin(2, lat=None, lon=None)])
    assert election.outcome == "consensus"
    assert election.chosen == first


def test_elect_two_agreeing_picks_is_consensus_and_names_a_pick():
    pick = _twin(2, pick=True)
    election = elect([_twin(1), pick, _twin(3, pick=True)])
    assert election.outcome == "consensus"
    assert election.chosen == pick


def test_elect_one_pick_wins_over_a_disagreeing_non_pick():
    pick = _twin(1, pick=True)
    election = elect([pick, _twin(2, at=datetime(2020, 1, 1, tzinfo=UTC), lat=1.0, lon=2.0)])
    assert election.outcome == "unique"
    assert election.chosen == pick


def test_elect_gps_on_one_candidate_only_is_ambiguous():
    election = elect([_twin(1), _twin(2, lat=None, lon=None)])
    assert election.outcome == "ambiguous"
    assert election.chosen is None


def test_elect_different_gps_is_ambiguous():
    election = elect([_twin(1), _twin(2, lon=7.250001)])
    assert election.outcome == "ambiguous"
    assert election.chosen is None


def test_elect_datetimes_one_second_apart_are_ambiguous():
    election = elect([_twin(1), _twin(2, at=AGREED_AT.replace(second=16))])
    assert election.outcome == "ambiguous"
    assert election.chosen is None


def test_elect_datetimes_differing_only_below_the_second_are_consensus():
    first = _twin(1, at=AGREED_AT.replace(microsecond=999_999))
    election = elect([first, _twin(2)])
    assert election.outcome == "consensus"
    assert election.chosen == first


def test_elect_a_candidate_without_a_datetime_is_ambiguous():
    assert elect([_twin(1), _twin(2, at=None)]).outcome == "ambiguous"
    assert elect([_twin(1, at=None), _twin(2, at=None)]).outcome == "ambiguous"


def test_elect_a_truncated_list_of_agreeing_candidates_is_ambiguous():
    election = elect([_twin(1), _twin(2)], truncated=True)
    assert election.outcome == "ambiguous"
    assert election.chosen is None


def test_elect_a_single_candidate_is_still_unique_not_consensus():
    only = _twin(1)
    election = elect([only])
    assert election.outcome == "unique"
    assert election.chosen == only


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "IMGMANAGER_URL", "http://img-manager.test:8000/")
    monkeypatch.setattr(settings, "IMGMANAGER_TOKEN", "tok-xyz")
    monkeypatch.setattr(settings, "SIBLING_OWNER_IDS", "3")


def _candidate_json(image_id: int, **over) -> dict:
    body = {
        "image_id": image_id,
        "folder_path": "/raw/2024",
        "filename": f"_AR{image_id}.ARW",
        "datetime_original": "2024-05-06T07:08:09Z",
        "gps_latitude": 46.5,
        "gps_longitude": 7.25,
        "is_pick": False,
    }
    body.update(over)
    return body


async def test_lookup_sends_one_post_with_every_variant_bearer_and_picks_only_false(enabled):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"stem": "_AR5123-Edit", "candidates": [], "truncated": False},
                    {"stem": "_AR5123", "candidates": [_candidate_json(9)], "truncated": False},
                ]
            },
        )

    results = await sibling.lookup(
        ["_AR5123-Edit", "_AR5123"], transport=httpx.MockTransport(handler)
    )

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "http://img-manager.test:8000/api/lookup/stems"
    assert request.headers["Authorization"] == "Bearer tok-xyz"
    assert json.loads(request.content) == {
        "stems": ["_AR5123-Edit", "_AR5123"],
        "picks_only": False,
    }
    assert results[0] == sibling.StemResult([], False)
    assert results[1].truncated is False
    assert results[1].candidates == [
        Candidate(
            image_id=9,
            datetime_original=datetime(2024, 5, 6, 7, 8, 9, tzinfo=UTC),
            gps_latitude=46.5,
            gps_longitude=7.25,
            is_pick=False,
        )
    ]


@pytest.mark.parametrize("status", [401, 403, 404, 500, 502, 503])
async def test_lookup_every_non_200_non_422_answer_is_unavailable(enabled, status):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json={}))
    with pytest.raises(SiblingUnavailable) as caught:
        await sibling.lookup(["_AR1"], transport=transport)
    # The job's `error` column is the exception text: a status, never a stem or the token.
    assert str(status) in str(caught.value)
    assert "_AR1" not in str(caught.value) and "tok-xyz" not in str(caught.value)


async def test_lookup_422_is_a_refusal_of_the_stems_and_yields_no_candidates(enabled):
    transport = httpx.MockTransport(lambda request: httpx.Response(422, json={"detail": []}))
    assert await sibling.lookup(["_AR1", "_AR2"], transport=transport) == [
        sibling.StemResult([]),
        sibling.StemResult([]),
    ]


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("x")],
)
async def test_lookup_transport_failures_are_unavailable(enabled, error):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(SiblingUnavailable):
        await sibling.lookup(["_AR1"], transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        json.dumps({"nope": []}).encode(),
        json.dumps({"results": []}).encode(),  # fewer results than stems asked
        json.dumps({"results": [{"stem": "_AR1", "candidates": [{"image_id": "x"}]}]}).encode(),
    ],
)
async def test_lookup_a_200_it_cannot_read_is_unavailable_never_an_absence(enabled, body):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    with pytest.raises(SiblingUnavailable):
        await sibling.lookup(["_AR1"], transport=transport)


async def test_lookup_drops_a_naive_date_and_an_unpaired_or_out_of_range_coordinate(enabled):
    body = {
        "results": [
            {
                "stem": "_AR1",
                "candidates": [
                    _candidate_json(1, datetime_original="2024-05-06T07:08:09"),
                    _candidate_json(2, gps_longitude=None),
                    _candidate_json(3, gps_latitude=91.0),
                ],
                "truncated": False,
            }
        ]
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    [found] = await sibling.lookup(["_AR1"], transport=transport)
    cands = found.candidates
    assert cands[0].datetime_original is None  # a zone the server cannot prove is no date
    assert (cands[1].gps_latitude, cands[1].gps_longitude) == (None, None)
    assert (cands[2].gps_latitude, cands[2].gps_longitude) == (None, None)


def test_sibling_enabled_needs_all_three_settings(monkeypatch):
    monkeypatch.setattr(settings, "IMGMANAGER_URL", "http://x")
    monkeypatch.setattr(settings, "IMGMANAGER_TOKEN", "t")
    monkeypatch.setattr(settings, "SIBLING_OWNER_IDS", "3, 5")
    assert settings.sibling_enabled
    assert settings.sibling_owner_ids == frozenset({3, 5})
    for name in ("IMGMANAGER_URL", "IMGMANAGER_TOKEN", "SIBLING_OWNER_IDS"):
        with monkeypatch.context() as m:
            m.setattr(settings, name, "")
            assert not settings.sibling_enabled


@pytest.mark.parametrize(
    "present",
    [
        ("IMGMANAGER_URL",),
        ("IMGMANAGER_TOKEN",),
        ("SIBLING_OWNER_IDS",),
        ("IMGMANAGER_URL", "IMGMANAGER_TOKEN"),
        ("IMGMANAGER_URL", "SIBLING_OWNER_IDS"),
        ("IMGMANAGER_TOKEN", "SIBLING_OWNER_IDS"),
    ],
)
def test_validate_runtime_secrets_refuses_a_partial_sibling_configuration(monkeypatch, present):
    values = {"IMGMANAGER_URL": "http://x", "IMGMANAGER_TOKEN": "t", "SIBLING_OWNER_IDS": "3"}
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value if name in present else "")
    with pytest.raises(
        RuntimeError, match="IMGMANAGER_URL, IMGMANAGER_TOKEN and SIBLING_OWNER_IDS"
    ):
        settings.validate_runtime_secrets()


def test_validate_runtime_secrets_accepts_all_or_none_of_the_sibling_settings(monkeypatch):
    for name in ("IMGMANAGER_URL", "IMGMANAGER_TOKEN", "SIBLING_OWNER_IDS"):
        monkeypatch.setattr(settings, name, "")
    settings.validate_runtime_secrets()
    monkeypatch.setattr(settings, "IMGMANAGER_URL", "http://x")
    monkeypatch.setattr(settings, "IMGMANAGER_TOKEN", "t")
    monkeypatch.setattr(settings, "SIBLING_OWNER_IDS", "3")
    settings.validate_runtime_secrets()


def test_a_non_integer_owner_id_is_refused_at_startup(monkeypatch):
    monkeypatch.setattr(settings, "IMGMANAGER_URL", "http://x")
    monkeypatch.setattr(settings, "IMGMANAGER_TOKEN", "t")
    monkeypatch.setattr(settings, "SIBLING_OWNER_IDS", "3,admin")
    with pytest.raises(RuntimeError, match="SIBLING_OWNER_IDS"):
        settings.validate_runtime_secrets()


def test_the_token_never_appears_in_the_settings_repr(monkeypatch):
    monkeypatch.setattr(settings, "IMGMANAGER_TOKEN", "tok-secret-value")
    assert "tok-secret-value" not in repr(settings)


async def test_lookup_a_candidates_value_that_is_not_a_list_is_unavailable(enabled):
    # A 200 whose candidates are a mapping would otherwise iterate as no candidates at all and
    # be recorded as a durable `none`.
    body = {"results": [{"stem": "_AR1", "candidates": {}, "truncated": False}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    with pytest.raises(SiblingUnavailable, match="unreadable answer"):
        await sibling.lookup(["_AR1"], transport=transport)


async def test_lookup_a_candidates_string_is_unavailable(enabled):
    body = {"results": [{"stem": "_AR1", "candidates": "x", "truncated": False}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    with pytest.raises(SiblingUnavailable, match="unreadable answer"):
        await sibling.lookup(["_AR1"], transport=transport)


@pytest.mark.parametrize("truncated", ["yes", None, 1])
async def test_lookup_a_truncated_flag_that_is_not_a_bool_is_unavailable(enabled, truncated):
    body = {"results": [{"stem": "_AR1", "candidates": [], "truncated": truncated}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    with pytest.raises(SiblingUnavailable, match="unreadable answer"):
        await sibling.lookup(["_AR1"], transport=transport)


def test_an_owner_list_with_no_id_in_it_disables_the_feature(monkeypatch):
    monkeypatch.setattr(settings, "IMGMANAGER_URL", "http://x")
    monkeypatch.setattr(settings, "IMGMANAGER_TOKEN", "t")
    monkeypatch.setattr(settings, "SIBLING_OWNER_IDS", ", ")
    assert not settings.sibling_enabled
    with pytest.raises(RuntimeError, match="SIBLING_OWNER_IDS"):
        settings.validate_runtime_secrets()
    monkeypatch.setattr(settings, "SIBLING_OWNER_IDS", "3")
    assert settings.sibling_enabled
    settings.validate_runtime_secrets()
