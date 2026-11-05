"""OIDC endpoint discovery (MEMNG-1089) and the configurable admin claim.

Every network answer comes from an `httpx.MockTransport` handed to `discover_oidc`, so a case can
both script the provider and count what was asked of it — the household regression below is an
assertion that NOTHING was asked.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from app import auth, main
from app.config import settings
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

ISSUER = "https://idp.example"
DOC = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/token",
    "jwks_uri": f"{ISSUER}/jwks",
    "userinfo_endpoint": f"{ISSUER}/userinfo",
    "end_session_endpoint": f"{ISSUER}/logout",
}
ENDPOINTS = {
    "OIDC_AUTHORIZE_ENDPOINT": "authorization_endpoint",
    "OIDC_TOKEN_ENDPOINT": "token_endpoint",
    "OIDC_JWKS_URI": "jwks_uri",
    "OIDC_USERINFO_ENDPOINT": "userinfo_endpoint",
    "OIDC_END_SESSION_ENDPOINT": "end_session_endpoint",
}


class Provider:
    """A scripted issuer that records every request it is sent."""

    def __init__(self, status: int = 200, body: object = DOC, error: Exception | None = None):
        self.status, self.body, self.error = status, body, error
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.error is not None:
                raise self.error
            payload = self.body if isinstance(self.body, str) else json.dumps(self.body)
            return httpx.Response(self.status, text=payload)

        return httpx.MockTransport(handle)


@pytest.fixture
def blank_endpoints(monkeypatch):
    """An instance configured with the issuer alone: every endpoint left empty."""
    monkeypatch.setattr(auth, "_discovered", {})
    # A lock is bound to the loop it first waited on, and each async case runs on its own loop.
    monkeypatch.setattr(auth, "_discovery_lock", asyncio.Lock())
    monkeypatch.setattr(settings, "OIDC_ISSUER", ISSUER)
    for field in ENDPOINTS:
        monkeypatch.setattr(settings, field, "")


async def test_discovery_fills_every_empty_endpoint(blank_endpoints):
    provider = Provider()
    await auth.discover_oidc(transport=provider.transport())
    assert [str(r.url) for r in provider.requests] == [f"{ISSUER}/.well-known/openid-configuration"]
    for field, key in ENDPOINTS.items():
        assert auth.oidc_endpoint(field) == DOC[key]


async def test_discovery_request_is_bounded_by_the_configured_timeout(blank_endpoints, monkeypatch):
    monkeypatch.setattr(settings, "OIDC_DISCOVERY_TIMEOUT_S", 3.5)
    provider = Provider()
    await auth.discover_oidc(transport=provider.transport())
    timeout = provider.requests[0].extensions["timeout"]
    assert set(timeout.values()) == {3.5}


async def test_issuer_with_trailing_slash_resolves_the_same_document(blank_endpoints, monkeypatch):
    monkeypatch.setattr(settings, "OIDC_ISSUER", f"{ISSUER}/")
    provider = Provider(body={**DOC, "issuer": f"{ISSUER}/"})
    await auth.discover_oidc(transport=provider.transport())
    assert str(provider.requests[0].url) == f"{ISSUER}/.well-known/openid-configuration"
    assert auth.oidc_endpoint("OIDC_TOKEN_ENDPOINT") == DOC["token_endpoint"]


async def test_an_explicit_endpoint_wins_over_discovery(blank_endpoints, monkeypatch):
    monkeypatch.setattr(settings, "OIDC_AUTHORIZE_ENDPOINT", "https://override.example/auth")
    monkeypatch.setattr(settings, "OIDC_JWKS_URI", "https://override.example/jwks")
    await auth.discover_oidc(transport=Provider().transport())
    assert auth.oidc_endpoint("OIDC_AUTHORIZE_ENDPOINT") == "https://override.example/auth"
    assert auth.oidc_endpoint("OIDC_JWKS_URI") == "https://override.example/jwks"
    assert auth.oidc_endpoint("OIDC_TOKEN_ENDPOINT") == DOC["token_endpoint"]


async def test_household_configuration_performs_no_discovery_request(monkeypatch):
    """All six set — the household's Authelia shape — must reach the provider zero times."""
    monkeypatch.setattr(auth, "_discovered", {})
    for field in ("OIDC_ISSUER", *ENDPOINTS):
        assert getattr(settings, field), f"{field} has no default: the regression premise moved"
    provider = Provider()
    await auth.discover_oidc(transport=provider.transport())
    assert provider.requests == []
    assert auth._discovered == {}
    assert auth.oidc_endpoint("OIDC_AUTHORIZE_ENDPOINT") == settings.OIDC_AUTHORIZE_ENDPOINT


async def test_no_issuer_means_no_discovery(blank_endpoints, monkeypatch):
    monkeypatch.setattr(settings, "OIDC_ISSUER", "")
    provider = Provider()
    await auth.discover_oidc(transport=provider.transport())
    assert provider.requests == []


@pytest.mark.parametrize(
    "provider",
    [
        Provider(error=httpx.ConnectError("refused")),
        Provider(error=httpx.ReadTimeout("slow")),
        Provider(status=503),
        Provider(body="<html>not json</html>"),
        Provider(body=["not", "an", "object"]),
        Provider(body={**DOC, "issuer": "https://someone-else.example"}),
        Provider(body={k: v for k, v in DOC.items() if k != "token_endpoint"}),
        Provider(body={**DOC, "jwks_uri": 42}),
    ],
    ids=[
        "connect",
        "timeout",
        "http-503",
        "not-json",
        "not-object",
        "issuer-mismatch",
        "required-missing",
        "not-a-string",
    ],
)
async def test_discovery_failure_is_a_named_startup_error(blank_endpoints, provider):
    with pytest.raises(auth.OidcDiscoveryError) as raised:
        await auth.discover_oidc(transport=provider.transport())
    assert ISSUER in str(raised.value)
    assert auth._discovered == {}  # nothing half-resolved is left behind


async def test_a_missing_required_key_is_ignored_when_its_endpoint_is_overridden(
    blank_endpoints, monkeypatch
):
    monkeypatch.setattr(settings, "OIDC_TOKEN_ENDPOINT", "https://override.example/token")
    body = {k: v for k, v in DOC.items() if k != "token_endpoint"}
    await auth.discover_oidc(transport=Provider(body=body).transport())
    assert auth.oidc_endpoint("OIDC_TOKEN_ENDPOINT") == "https://override.example/token"


async def test_optional_endpoints_may_be_absent(blank_endpoints):
    body = {k: v for k, v in DOC.items() if k not in ("userinfo_endpoint", "end_session_endpoint")}
    await auth.discover_oidc(transport=Provider(body=body).transport())
    assert auth.oidc_endpoint("OIDC_USERINFO_ENDPOINT") == ""
    assert auth.oidc_endpoint("OIDC_END_SESSION_ENDPOINT") == ""
    # Without a userinfo endpoint the id_token's own claims are all there is.
    assert await auth.fetch_userinfo("access-token") == {}


async def test_concurrent_discoveries_ask_the_provider_once(blank_endpoints):
    provider = Provider()
    transport = provider.transport()
    await asyncio.gather(*(auth.discover_oidc(transport=transport) for _ in range(3)))
    assert len(provider.requests) == 1
    assert auth.oidc_endpoint("OIDC_JWKS_URI") == DOC["jwks_uri"]


async def test_lifespan_refuses_to_start_when_discovery_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    refusal = auth.OidcDiscoveryError(f"OIDC discovery from issuer {ISSUER} failed: boom")
    monkeypatch.setattr(auth, "discover_oidc", AsyncMock(side_effect=refusal))
    with pytest.raises(auth.OidcDiscoveryError):
        async with main.lifespan(main.app):
            pass
    auth.discover_oidc.assert_awaited_once()


def test_login_redirects_to_the_discovered_authorize_endpoint(blank_endpoints, client):
    auth._discovered.update({"OIDC_AUTHORIZE_ENDPOINT": DOC["authorization_endpoint"]})
    r = client.get("/api/auth/login", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith(f"{DOC['authorization_endpoint']}?")


def test_logout_without_end_session_endpoint_returns_the_post_logout_page(blank_endpoints, client):
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    assert r.json()["logout_url"] == settings.OIDC_POST_LOGOUT_REDIRECT_URI


# --- the admin claim ---


@pytest.mark.parametrize(
    ("admin_claim", "claims", "role"),
    [
        ("", {"groups": ["admins"]}, "admin"),  # today's behaviour, unchanged
        ("", {"groups": ["users"]}, "user"),
        ("", {}, "user"),
        ("", {"admins": True}, "user"),  # the default rule never read a boolean claim
        ("memos-admin", {"groups": ["memos-admin"]}, "admin"),  # a group name
        ("memos-admin", {"memos-admin": True}, "admin"),  # a boolean claim
        ("memos-admin", {"groups": ["admins"]}, "user"),  # the default no longer applies
        ("memos-admin", {"memos-admin": "true"}, "user"),  # only a real boolean grants
        ("memos-admin", {"groups": "memos-admin"}, "user"),  # a string is not a group list
        ("memos-admin", {}, "user"),
    ],
)
def test_role_for_claims(monkeypatch, admin_claim, claims, role):
    monkeypatch.setattr(settings, "OIDC_ADMIN_CLAIM", admin_claim)
    assert auth.role_for_claims(claims) == role


@pytest.mark.parametrize(("groups", "role"), [(["family-admins"], "admin"), (["admins"], "user")])
def test_callback_maps_the_configured_admin_claim(client, db_engine, monkeypatch, groups, role):
    subject = "oidc_admin_claim_user"
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "OIDC_ADMIN_CLAIM", "family-admins")
    monkeypatch.setattr(auth, "exchange_code", AsyncMock(return_value={"id_token": "x"}))
    monkeypatch.setattr(
        auth, "validate_id_token", lambda t: {"preferred_username": subject, "groups": groups}
    )
    try:
        login = client.get("/api/auth/login", follow_redirects=False)
        tx = login.cookies[auth.TX_COOKIE]
        client.cookies.set(auth.TX_COOKIE, tx)
        state = auth.decrypt(tx)["state"]
        cb = client.get(f"/api/auth/callback?code=c&state={state}", follow_redirects=False)
        client.cookies.set(auth.SID_COOKIE, cb.cookies[auth.SID_COOKIE])
        me = client.get("/api/auth/user")
        assert me.status_code == 200
        assert me.json()["role"] == role
    finally:
        eng = create_engine(SYNC_URL)
        with eng.begin() as c:
            c.execute(text("DELETE FROM users WHERE subject = :s"), {"s": subject})
        eng.dispose()


@pytest.mark.parametrize("key", list(ENDPOINTS.values()))
async def test_a_discovered_plain_http_endpoint_fails_startup_naming_it(blank_endpoints, key):
    """A discovery answer an on-path attacker could forge must not point any endpoint — userinfo
    above all, whose claims become the identity — at a channel nobody authenticates."""
    insecure = DOC[key].replace("https://", "http://")
    provider = Provider(body={**DOC, key: insecure})
    with pytest.raises(auth.OidcDiscoveryError) as raised:
        await auth.discover_oidc(transport=provider.transport())
    assert ISSUER in str(raised.value)
    assert insecure in str(raised.value)
    assert auth._discovered == {}


async def test_a_plain_http_issuer_is_refused_before_any_request(blank_endpoints, monkeypatch):
    http_issuer = "http://idp.example"
    monkeypatch.setattr(settings, "OIDC_ISSUER", http_issuer)
    provider = Provider(body={**DOC, "issuer": http_issuer})
    with pytest.raises(auth.OidcDiscoveryError) as raised:
        await auth.discover_oidc(transport=provider.transport())
    assert http_issuer in str(raised.value)
    assert provider.requests == []


async def test_explicit_http_endpoints_keep_their_rule_and_skip_discovery(monkeypatch):
    """The https rule is discovery's: an endpoint the operator configured is used as given."""
    monkeypatch.setattr(auth, "_discovered", {})
    monkeypatch.setattr(settings, "OIDC_ISSUER", "http://idp.example")
    for field in ENDPOINTS:
        monkeypatch.setattr(settings, field, f"http://idp.example/{field.lower()}")
    provider = Provider()
    await auth.discover_oidc(transport=provider.transport())
    assert provider.requests == []
    assert (
        auth.oidc_endpoint("OIDC_USERINFO_ENDPOINT") == "http://idp.example/oidc_userinfo_endpoint"
    )
