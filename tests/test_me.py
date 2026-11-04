"""MEMNG-972: `GET`/`PATCH /api/me` — the caller's own account preferences.

`default_visibility` seeds every memo posted without one (the same column `/api/settings` writes);
`notify_on_household_posts` is the opt-in MEMNG-967 reads to decide who gets a memo-live push.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from tests.test_attachments_taken import _sql

BEFORE_NOTIFY_REVISION = "85d081a451a1"


def test_me_requires_auth(client):
    assert client.get("/api/me").status_code == 401
    assert client.patch("/api/me", json={"notify_on_household_posts": True}).status_code == 401


def test_me_defaults_for_a_new_user(logged_in_client):
    client, _ = logged_in_client
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json() == {"default_visibility": "PUBLIC", "notify_on_household_posts": False}


def test_patch_default_visibility_seeds_the_next_memo(logged_in_client):
    """The AC's server test: a default set through /api/me is what the NEXT memo posted without an
    explicit visibility gets."""
    client, _ = logged_in_client
    before = client.post("/api/memos", json={"content": "before the patch"}).json()
    assert before["visibility"] == "PUBLIC"  # control: the default is not already PRIVATE

    r = client.patch("/api/me", json={"default_visibility": "PRIVATE"})
    assert r.status_code == 200
    assert r.json() == {"default_visibility": "PRIVATE", "notify_on_household_posts": False}

    after = client.post("/api/memos", json={"content": "no explicit visibility"}).json()
    assert after["visibility"] == "PRIVATE"


def test_patch_notify_persists_and_leaves_visibility_alone(logged_in_client):
    client, subject = logged_in_client
    r = client.patch("/api/me", json={"notify_on_household_posts": True})
    assert r.status_code == 200 and r.json()["notify_on_household_posts"] is True
    assert client.get("/api/me").json() == {
        "default_visibility": "PUBLIC",  # absent field = unchanged
        "notify_on_household_posts": True,
    }
    row = _sql("SELECT notify_on_household_posts FROM users WHERE subject = :s", {"s": subject})
    assert row == [(True,)]  # the column itself, not a response echo

    r = client.patch("/api/me", json={"notify_on_household_posts": False})
    assert r.status_code == 200
    assert client.get("/api/me").json()["notify_on_household_posts"] is False


def test_an_empty_patch_changes_nothing(logged_in_client):
    client, _ = logged_in_client
    client.patch(
        "/api/me", json={"default_visibility": "PROTECTED", "notify_on_household_posts": True}
    )
    r = client.patch("/api/me", json={})
    assert r.status_code == 200
    assert r.json() == {"default_visibility": "PROTECTED", "notify_on_household_posts": True}


def test_me_and_settings_share_one_default_visibility(logged_in_client):
    client, _ = logged_in_client
    client.patch("/api/settings", json={"default_visibility": "PROTECTED"})
    assert client.get("/api/me").json()["default_visibility"] == "PROTECTED"
    client.patch("/api/me", json={"default_visibility": "PRIVATE"})
    assert client.get("/api/settings").json()["default_visibility"] == "PRIVATE"


@pytest.mark.parametrize(
    "body",
    [
        {"default_visibility": None},
        {"notify_on_household_posts": None},
        {"default_visibility": "SECRET"},
        {"default_visibility": "private"},
        {"notify_on_household_posts": "yes"},
        {"notify_on_household_posts": 1},
    ],
)
def test_patch_rejects_null_and_invalid_values(logged_in_client, body):
    client, subject = logged_in_client
    r = client.patch("/api/me", json=body)
    assert r.status_code == 422, r.text
    # and nothing was written
    assert client.get("/api/me").json() == {
        "default_visibility": "PUBLIC",
        "notify_on_household_posts": False,
    }


async def test_bearer_reads_and_writes_me(pat_client):
    bearer, _, _ = pat_client
    assert (await bearer.get("/api/me")).status_code == 200
    patched = await bearer.patch("/api/me", json={"notify_on_household_posts": True})
    assert patched.status_code == 200
    assert (await bearer.get("/api/me")).json()["notify_on_household_posts"] is True


def test_a_user_row_written_without_the_column_defaults_to_false(db_engine):
    """The server default is what existing rows get on upgrade and what any writer that does not
    name the column gets — not just the ORM's Python-side default."""
    subject = "memng972_raw_insert"
    _sql("DELETE FROM users WHERE subject = :s", {"s": subject})
    try:
        _sql(
            "INSERT INTO users (subject, role, default_visibility, theme) "
            "VALUES (:s, 'user', 'PUBLIC', 'dark')",
            {"s": subject},
        )
        row = _sql("SELECT notify_on_household_posts FROM users WHERE subject = :s", {"s": subject})
        assert row == [(False,)]
    finally:
        _sql("DELETE FROM users WHERE subject = :s", {"s": subject})


def test_a_patch_touches_only_the_callers_row(logged_in_client):
    client, _ = logged_in_client
    other = "memng972_bystander"
    _sql("DELETE FROM users WHERE subject = :s", {"s": other})
    try:
        _sql(
            "INSERT INTO users (subject, role, default_visibility, theme) "
            "VALUES (:s, 'user', 'PUBLIC', 'dark')",
            {"s": other},
        )
        r = client.patch(
            "/api/me", json={"default_visibility": "PRIVATE", "notify_on_household_posts": True}
        )
        assert r.status_code == 200
        row = _sql(
            "SELECT default_visibility::text, notify_on_household_posts FROM users "
            "WHERE subject = :s",
            {"s": other},
        )
        assert row == [("PUBLIC", False)]
    finally:
        _sql("DELETE FROM users WHERE subject = :s", {"s": other})


def test_a_patch_of_one_field_does_not_undo_a_concurrent_write_of_the_other(
    logged_in_client, monkeypatch
):
    """Two devices racing: while this PATCH (notify only) is in flight, another writer changes
    default_visibility. The PATCH must write only the field it was given, so the other write
    survives instead of being overwritten with the value this request loaded."""
    from app.models import User
    from sqlalchemy.ext.asyncio import AsyncSession

    client, subject = logged_in_client
    real_commit = AsyncSession.commit
    raced = []

    async def racing_commit(self):
        if not raced and any(isinstance(o, User) for o in self.dirty):
            raced.append(True)
            _sql(
                "UPDATE users SET default_visibility = 'PROTECTED' WHERE subject = :s",
                {"s": subject},
            )
        await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", racing_commit)
    r = client.patch("/api/me", json={"notify_on_household_posts": True})
    assert r.status_code == 200 and raced  # control: the race really ran inside the PATCH
    row = _sql(
        "SELECT default_visibility::text, notify_on_household_posts FROM users WHERE subject = :s",
        {"s": subject},
    )
    assert row == [("PROTECTED", True)]


def _notify_column() -> list:
    return _sql(
        "SELECT is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='users' "
        "AND column_name='notify_on_household_posts'"
    )


def test_the_migration_round_trips(logged_in_client):
    """Down to the revision below it drops exactly the column; up again restores it NOT NULL with
    a false default — and an existing user row comes back false, never NULL."""
    _, subject = logged_in_client
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    users_before = _sql("SELECT count(*) FROM users")[0][0]
    assert _notify_column()  # control: head carries it
    try:
        command.downgrade(cfg, BEFORE_NOTIFY_REVISION)
        assert _notify_column() == []
    finally:
        command.upgrade(cfg, "head")
    [(nullable, default)] = _notify_column()
    assert nullable == "NO" and "false" in default
    assert _sql("SELECT count(*) FROM users")[0][0] == users_before
    row = _sql("SELECT notify_on_household_posts FROM users WHERE subject = :s", {"s": subject})
    assert row == [(False,)]
