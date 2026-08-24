"""GET /api/members — the household roster the iOS filter sheet is built from (MEMNG-133).

Every case drives the real route through the real authentication dependency; nothing here mocks
`auth.require_user`, so deleting the roster query or its ORDER BY turns these red.
"""

from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

# All-lowercase-letter subjects and display names on purpose: the ordering assertions compare
# Postgres' collation against Python's, and anything with a space, digit or punctuation can sort
# differently under the two without either being wrong.
SEEDED = (
    # (subject, display_name, avatar) — inserted in THIS order, which is not the sorted one, so an
    # ORDER BY that stopped working would hand back insertion order and fail the ordering test.
    ("zzmembera", "zzzdelta", None),
    ("zzmemberb", "zzzalpha", "data:image/png;base64,iVBORw0KGgo="),
    ("zzmemberc", None, None),
    ("zzmemberd", None, None),
)
# display_name ASC (Postgres puts NULLs last), then subject ASC.
EXPECTED_ORDER = ["zzmemberb", "zzmembera", "zzmemberc", "zzmemberd"]


def _engine():
    return create_engine(SYNC_URL)


def _seed_members():
    eng = _engine()
    try:
        with eng.begin() as c:
            for subject, display_name, avatar in SEEDED:
                c.execute(
                    text(
                        "INSERT INTO users (subject, display_name, avatar, role, "
                        "default_visibility, theme) VALUES (:s, :d, :a, 'user', 'PUBLIC', 'dark')"
                    ),
                    {"s": subject, "d": display_name, "a": avatar},
                )
    finally:
        eng.dispose()


def _drop_members():
    eng = _engine()
    try:
        with eng.begin() as c:
            c.execute(text("DELETE FROM users WHERE subject LIKE 'zzmember%'"))
    finally:
        eng.dispose()


def _mine(payload: list[dict]) -> list[dict]:
    """Only the members this test seeded — other tests' users share the table."""
    return [m for m in payload if m["subject"].startswith("zzmember")]


def test_a_session_cookie_gets_the_roster(logged_in_client):
    client, subject = logged_in_client
    _seed_members()
    try:
        response = client.get("/api/members")
        assert response.status_code == 200, response.text
        subjects = [m["subject"] for m in response.json()]
        # The caller is on their own roster, and so is everyone else.
        assert subject in subjects
        assert [m["subject"] for m in _mine(response.json())] == EXPECTED_ORDER
    finally:
        _drop_members()


async def test_a_bearer_token_gets_the_same_roster(pat_client):
    bearer, subject, _ = pat_client
    _seed_members()
    try:
        response = await bearer.get("/api/members")
        assert response.status_code == 200, response.text
        subjects = [m["subject"] for m in response.json()]
        assert subject in subjects
        assert [m["subject"] for m in _mine(response.json())] == EXPECTED_ORDER
    finally:
        _drop_members()


def test_an_anonymous_caller_is_refused(client: TestClient):
    """No cookie, no bearer — the roster is household-internal, not a public directory."""
    assert client.get("/api/members").status_code == 401


def test_a_member_with_no_visible_memos_is_still_a_member(logged_in_client):
    """The roster is membership, not authorship — the filter sheet has to be able to offer a
    member whose memos are all PRIVATE, or an empty result would be indistinguishable from a
    member who does not exist."""
    client, _ = logged_in_client
    _seed_members()
    try:
        # zzmembera posts nothing at all; zzmemberb posts only a PRIVATE memo the caller cannot see.
        eng = _engine()
        with eng.begin() as c:
            c.execute(
                text(
                    "INSERT INTO memos (uid, owner_id, content, visibility, pinned, created_at, "
                    "updated_at) SELECT 'zzprivmemo', id, 'hidden', 'PRIVATE', false, now(), now() "
                    "FROM users WHERE subject = 'zzmemberb'"
                )
            )
        eng.dispose()
        try:
            listed = client.get("/api/memos").json()["items"]
            assert not [m for m in listed if m["uid"] == "zzprivmemo"]  # genuinely invisible
            roster = [m["subject"] for m in client.get("/api/members").json()]
            assert "zzmembera" in roster and "zzmemberb" in roster
        finally:
            eng = _engine()
            with eng.begin() as c:
                c.execute(text("DELETE FROM memos WHERE uid = 'zzprivmemo'"))
            eng.dispose()
    finally:
        _drop_members()


def test_a_member_carries_exactly_subject_display_name_and_avatar(logged_in_client):
    """The shape the SPA's own user model uses, minus what nobody needs about someone else: a
    widening that started publishing `email` or `role` about other people must fail here."""
    client, _ = logged_in_client
    _seed_members()
    try:
        members = {m["subject"]: m for m in _mine(client.get("/api/members").json())}
        assert set(members["zzmemberb"]) == {"subject", "display_name", "avatar"}
        assert members["zzmemberb"]["display_name"] == "zzzalpha"
        # Passed through verbatim — the data: URL the settings feature stores (app/routers/
        # settings.py accepts only `data:image/…`), not a re-derived URL.
        assert members["zzmemberb"]["avatar"] == "data:image/png;base64,iVBORw0KGgo="
        assert members["zzmemberc"]["display_name"] is None
        assert members["zzmemberc"]["avatar"] is None
    finally:
        _drop_members()


def test_the_schema_publishes_the_roster_and_the_credentials_it_takes():
    """The generated iOS/SPA clients are built from this document, so the route existing in the
    app is only half of shipping it (ADR-0009)."""
    operation = app.openapi()["paths"]["/api/members"]["get"]
    assert operation["security"] == [{"bearerAuth": []}, {"sessionCookie": []}]
    items = operation["responses"]["200"]["content"]["application/json"]["schema"]["items"]
    assert items["$ref"].endswith("/MemberOut")
    member = app.openapi()["components"]["schemas"]["MemberOut"]
    assert member["required"] == ["subject", "display_name"]
    assert member["properties"]["subject"]["type"] == "string"
    assert member["properties"]["display_name"]["type"] == ["string", "null"]
    assert member["properties"]["avatar"]["type"] == ["string", "null"]
