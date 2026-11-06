"""Local username + password sign-in behind LOCAL_LOGIN=1 (MEMNG-1097).

Users are written straight into the database the way an operator's `app.local_password` would
leave them, and the route under test is the real one, reached through the real app.
"""

from unittest.mock import AsyncMock

import pytest
from app import auth, local_password
from app.config import settings
from argon2 import PasswordHasher
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

SUBJECT = "local_login_user"
PASSWORD = "correct horse battery staple"
LOGIN = "/api/auth/local/login"


@pytest.fixture
def local_user(db_engine, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_LOGIN", True)
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    with db_engine.begin() as c:
        c.execute(
            text("INSERT INTO users (subject, role, password_hash) VALUES (:s, 'user', :h)"),
            {"s": SUBJECT, "h": auth.hash_password(PASSWORD)},
        )
    try:
        yield SUBJECT
    finally:
        with db_engine.begin() as c:
            c.execute(text("DELETE FROM users WHERE subject = :s"), {"s": SUBJECT})


def _sessions(db_engine, subject: str) -> int:
    with db_engine.connect() as c:
        return c.execute(
            text(
                "SELECT count(*) FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE u.subject = :s"
            ),
            {"s": subject},
        ).scalar_one()


def _cookie_attrs(set_cookie: str) -> set[str]:
    """The attributes of a Set-Cookie header with the cookie's VALUE removed."""
    name_value, *attrs = (part.strip().lower() for part in set_cookie.split(";"))
    return {name_value.split("=", 1)[0], *attrs}


def test_route_absent_when_local_login_unset(client, local_user, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_LOGIN", False)
    r = client.post(LOGIN, json={"username": SUBJECT, "password": PASSWORD})
    assert r.status_code == 404
    assert auth.SID_COOKIE not in r.cookies


def test_local_login_is_off_by_default():
    assert settings.model_fields["LOCAL_LOGIN"].default is False


def test_login_ok_issues_a_working_session(client, local_user, db_engine):
    r = client.post(LOGIN, json={"username": SUBJECT, "password": PASSWORD})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    sid = r.cookies[auth.SID_COOKIE]
    assert _sessions(db_engine, SUBJECT) == 1
    client.cookies.set(auth.SID_COOKIE, sid)
    me = client.get("/api/auth/user")
    assert me.status_code == 200
    assert me.json()["subject"] == SUBJECT
    assert me.json()["auth_kind"] == "session"


@pytest.mark.parametrize(
    "body",
    [
        {"username": SUBJECT, "password": "wrong"},
        {"username": SUBJECT, "password": ""},
        {"username": "nobody_by_this_name", "password": PASSWORD},
    ],
    ids=["wrong-password", "empty-password", "unknown-user"],
)
def test_refusals_are_one_indistinguishable_401(client, local_user, db_engine, body):
    r = client.post(LOGIN, json=body)
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid username or password"}
    assert auth.SID_COOKIE not in r.cookies
    assert _sessions(db_engine, SUBJECT) == 0


def test_user_without_a_password_cannot_sign_in_locally(client, local_user, db_engine):
    """An OIDC-only account has no password_hash; no password may open it."""
    with db_engine.begin() as c:
        c.execute(text("UPDATE users SET password_hash = NULL WHERE subject = :s"), {"s": SUBJECT})
    for password in (PASSWORD, "", "null"):
        r = client.post(LOGIN, json={"username": SUBJECT, "password": password})
        assert r.status_code == 401
    assert _sessions(db_engine, SUBJECT) == 0


def test_cross_origin_login_is_refused(client, local_user, db_engine):
    r = client.post(
        LOGIN,
        json={"username": SUBJECT, "password": PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert _sessions(db_engine, SUBJECT) == 0


def test_login_is_rate_limited_like_the_oidc_paths(client, local_user):
    codes = [
        client.post(LOGIN, json={"username": SUBJECT, "password": "wrong"}).status_code
        for _ in range(21)
    ]
    assert codes[:20] == [401] * 20
    assert codes[20] == 429


def test_session_cookie_matches_the_oidc_path(client, local_user, monkeypatch):
    local = client.post(LOGIN, json={"username": SUBJECT, "password": PASSWORD})

    oidc_subject = "local_login_oidc_twin"
    monkeypatch.setattr(auth, "exchange_code", AsyncMock(return_value={"id_token": "x"}))
    monkeypatch.setattr(auth, "validate_id_token", lambda t: {"preferred_username": oidc_subject})
    try:
        client.cookies.clear()
        login = client.get("/api/auth/login", follow_redirects=False)
        tx = login.cookies[auth.TX_COOKIE]
        client.cookies.set(auth.TX_COOKIE, tx)
        state = auth.decrypt(tx)["state"]
        cb = client.get(f"/api/auth/callback?code=c&state={state}", follow_redirects=False)
        oidc_cookie = next(
            v for v in cb.headers.get_list("set-cookie") if v.startswith(auth.SID_COOKIE)
        )
    finally:
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(text("DELETE FROM users WHERE subject = :s"), {"s": oidc_subject})
        eng.dispose()
    local_cookie = next(
        v for v in local.headers.get_list("set-cookie") if v.startswith(auth.SID_COOKIE)
    )
    assert _cookie_attrs(local_cookie) == _cookie_attrs(oidc_cookie)
    assert len(local.cookies[auth.SID_COOKIE]) == len(cb.cookies[auth.SID_COOKIE])


def test_logout_of_a_local_session_does_not_send_the_browser_to_the_provider(client, local_user):
    r = client.post(LOGIN, json={"username": SUBJECT, "password": PASSWORD})
    client.cookies.set(auth.SID_COOKIE, r.cookies[auth.SID_COOKIE])
    out = client.post("/api/auth/logout")
    assert out.status_code == 200
    assert out.json()["logout_url"] == settings.OIDC_POST_LOGOUT_REDIRECT_URI
    client.cookies.set(auth.SID_COOKIE, r.cookies[auth.SID_COOKIE])
    assert client.get("/api/auth/user").status_code == 401


def test_an_outdated_hash_is_upgraded_on_login(client, local_user, db_engine):
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    with db_engine.begin() as c:
        c.execute(
            text("UPDATE users SET password_hash = :h WHERE subject = :s"),
            {"h": weak, "s": SUBJECT},
        )
    assert client.post(LOGIN, json={"username": SUBJECT, "password": PASSWORD}).status_code == 200
    with db_engine.connect() as c:
        stored = c.execute(
            text("SELECT password_hash FROM users WHERE subject = :s"), {"s": SUBJECT}
        ).scalar_one()
    assert stored != weak
    assert stored.startswith("$argon2id$")
    assert not auth.password_needs_rehash(stored)


def test_hash_password_is_argon2id_and_verifies():
    hashed = auth.hash_password(PASSWORD)
    assert hashed.startswith("$argon2id$")
    assert auth.hash_password(PASSWORD) != hashed  # a fresh salt every time
    assert auth.verify_password(hashed, PASSWORD) is True
    assert auth.verify_password(hashed, "wrong") is False
    assert auth.verify_password("not-a-hash", PASSWORD) is False
    assert auth.verify_password(None, PASSWORD) is False


def test_set_local_password_writes_a_verifiable_hash(local_user, db_engine):
    assert local_password.set_local_password(SYNC_URL, SUBJECT, "a new one") is True
    with db_engine.connect() as c:
        stored = c.execute(
            text("SELECT password_hash FROM users WHERE subject = :s"), {"s": SUBJECT}
        ).scalar_one()
    assert auth.verify_password(stored, "a new one") is True
    assert auth.verify_password(stored, PASSWORD) is False


def test_set_local_password_refuses_an_unknown_user_and_an_empty_password(local_user):
    assert local_password.set_local_password(SYNC_URL, "nobody_by_this_name", "pw") is False
    with pytest.raises(ValueError):
        local_password.set_local_password(SYNC_URL, SUBJECT, "")


def test_set_local_password_create_makes_a_user_who_can_sign_in(client, local_user, db_engine):
    subject = "local_login_created"
    try:
        assert local_password.set_local_password(
            SYNC_URL, subject, "made here", create=True, role="admin"
        )
        with db_engine.connect() as c:
            role = c.execute(
                text("SELECT role FROM users WHERE subject = :s"), {"s": subject}
            ).scalar_one()
        assert role == "admin"
        r = client.post(LOGIN, json={"username": subject, "password": "made here"})
        assert r.status_code == 200
    finally:
        with db_engine.begin() as c:
            c.execute(text("DELETE FROM users WHERE subject = :s"), {"s": subject})


def test_local_password_cli_reads_the_password_from_stdin(local_user, db_engine, monkeypatch):
    import io

    monkeypatch.setattr(settings, "DATABASE_URL_SYNC", SYNC_URL)
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin\n"))
    assert local_password.main([SUBJECT]) == 0
    with db_engine.connect() as c:
        stored = c.execute(
            text("SELECT password_hash FROM users WHERE subject = :s"), {"s": SUBJECT}
        ).scalar_one()
    assert auth.verify_password(stored, "from stdin")
    monkeypatch.setattr("sys.stdin", io.StringIO("x\n"))
    assert local_password.main(["nobody_by_this_name"]) == 1


def test_route_absent_when_unset_even_for_an_invalid_body(client, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_LOGIN", False)
    assert client.post(LOGIN, json={}).status_code == 404
    assert client.post(LOGIN, content=b"not json").status_code == 404
    # A body FastAPI would parse as JSON before any handler code: still absence, never a 422.
    malformed = {"Content-Type": "application/json"}
    assert client.post(LOGIN, content=b"{not json", headers=malformed).status_code == 404
    assert client.post(LOGIN, content=b'{"username": 1}', headers=malformed).status_code == 404


def test_enabled_route_still_validates_its_body(client, local_user):
    malformed = {"Content-Type": "application/json"}
    assert client.post(LOGIN, content=b"{not json", headers=malformed).status_code == 422
    assert client.post(LOGIN, json={"username": "", "password": "x"}).status_code == 422
    assert client.post(LOGIN, json={"password": PASSWORD}).status_code == 422


def _password_column(db_engine) -> list[tuple]:
    with db_engine.connect() as c:
        return c.execute(
            text(
                # This schema's table only: the trip-ingest suites keep a `pluto` copy of every
                # table for the whole session, and which of them ran first is file order.
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name='users' "
                "AND column_name='password_hash'"
            )
        ).all()


def test_the_migration_is_additive_and_round_trips(local_user, db_engine):
    """Down one revision drops exactly the column and no user; up again restores it nullable with
    no default, so every row — this user's included — reads as having no local password."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    with db_engine.connect() as c:
        users_before = c.execute(text("SELECT count(*) FROM users")).scalar_one()
    assert _password_column(db_engine) == [("YES", None)]  # control: head carries it
    try:
        command.downgrade(cfg, "22f3df18f2df")
        assert _password_column(db_engine) == []
    finally:
        command.upgrade(cfg, "head")
    assert _password_column(db_engine) == [("YES", None)]
    with db_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM users")).scalar_one() == users_before
        stored = c.execute(
            text("SELECT password_hash FROM users WHERE subject = :s"), {"s": SUBJECT}
        ).scalar_one()
    assert stored is None
