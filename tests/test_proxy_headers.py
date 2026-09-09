"""The login rate limiter must see the REAL client IP, not Traefik's (MEMNG-28).

Traefik is the only ingress, so without proxy-header handling every request arrives from the
same proxy address: `request.client.host` is Traefik for everyone, all clients land in ONE
rate-limit bucket, and a single noisy client starves login for the whole household.

The rewrite happens in exactly one layer — uvicorn's ProxyHeadersMiddleware, configured by
`docker-entrypoint.sh`. So these tests drive the real middleware with the trust list the
deployed artifact actually ships (parsed out of the entrypoint), against the real
`/api/auth/login` route and its real `Depends(auth.rate_limit())`. Weakening either the flags
or the trust list turns them red.
"""

import re
from pathlib import Path
from typing import Any, cast

import pytest
from app import auth
from app.main import app
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

# Every case that drives `_login` reaches `rate_limit()` and its 60-second monotonic window, so
# the module is pinned wholesale; the two that only read `docker-entrypoint.sh` touch no clock
# and say so with `real_clock` (MEMNG-296).
pytestmark = pytest.mark.usefixtures("controlled_clock")

ENTRYPOINT = Path(__file__).resolve().parent.parent / "docker-entrypoint.sh"

# Stand-ins for the deployed topology: Traefik on the shared `apps_net` bridge, and two
# household clients whose addresses Traefik forwards.
TRAEFIK = "172.20.0.9"
CLIENT_A = "192.0.2.11"
CLIENT_B = "203.0.113.77"


def _serve_command() -> str:
    """The `exec uvicorn …` line from the entrypoint, with `\\`-continuations joined."""
    text = ENTRYPOINT.read_text()
    joined = re.sub(r"\\\n\s*", " ", text)
    for line in joined.splitlines():
        if line.startswith("exec uvicorn"):
            return line
    raise AssertionError("docker-entrypoint.sh no longer execs uvicorn")


def _shipped_trust_list() -> str:
    """The default `--forwarded-allow-ips` value baked into the deployed artifact."""
    m = re.search(
        r'^FORWARDED_ALLOW_IPS="\$\{FORWARDED_ALLOW_IPS:-([^}]*)\}"',
        ENTRYPOINT.read_text(),
        re.MULTILINE,
    )
    assert m, "docker-entrypoint.sh no longer defines a default FORWARDED_ALLOW_IPS trust list"
    return m.group(1)


def _behind_proxy(peer: str = TRAEFIK) -> TestClient:
    """The real app behind the real proxy-header middleware, configured exactly as deployed.

    `peer` is the address uvicorn sees on the socket — Traefik in production. The casts only
    bridge uvicorn's private ASGI type aliases to starlette's; they are stub-level, not runtime.
    """
    stack = ProxyHeadersMiddleware(cast(Any, app), trusted_hosts=_shipped_trust_list())
    return TestClient(cast(Any, stack), client=(peer, 41234))


def _login(client: TestClient, forwarded_for: str):
    return client.get(
        "/api/auth/login", headers={"X-Forwarded-For": forwarded_for}, follow_redirects=False
    )


# --- deploy-artifact pin ------------------------------------------------------------------------


@pytest.mark.real_clock
def test_entrypoint_serves_uvicorn_with_forwarded_ip_handling():
    # Dropping either flag silently re-collapses every client into Traefik's single bucket,
    # and nothing else in the repo would notice — so the shipped command line is pinned here.
    cmd = _serve_command()
    assert "--proxy-headers" in cmd
    assert '--forwarded-allow-ips "$FORWARDED_ALLOW_IPS"' in cmd


@pytest.mark.real_clock
def test_entrypoint_trust_list_is_explicit_and_covers_the_compose_networks():
    trusted = _shipped_trust_list()
    # `*` would trust a forged header from anywhere the socket is reachable — the trust scope
    # is "private docker networks only" and must stay written down as such.
    assert "*" not in trusted
    entries = trusted.split(",")
    # Docker's default bridge pools live in 172.16/12 and 192.168/16; user-defined pools and
    # the household LAN in 10/8. Loopback keeps uvicorn's own default behaviour.
    for expected in ("127.0.0.1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert expected in entries


# --- AC1: buckets are keyed per real client IP ---------------------------------------------------


def test_forwarded_clients_get_their_own_buckets(controlled_clock):
    client = _behind_proxy()
    assert _login(client, CLIENT_A).status_code == 302
    assert _login(client, CLIENT_B).status_code == 302

    # Keyed by who actually made the request, not by the proxy that relayed it.
    assert set(auth._RL) == {CLIENT_A, CLIENT_B}
    assert TRAEFIK not in auth._RL
    assert len(auth._RL[CLIENT_A]) == 1
    assert len(auth._RL[CLIENT_B]) == 1
    # The pin reached the limiter through the real middleware: every stamp is the controlled
    # instant, so what these buckets hold is a count and not an elapsed time.
    assert set(auth._RL[CLIENT_A]) == {controlled_clock.monotonic}
    assert set(auth._RL[CLIENT_B]) == {controlled_clock.monotonic}


def test_same_forwarded_client_shares_one_bucket(controlled_clock):
    client = _behind_proxy()
    _login(client, CLIENT_A)
    _login(client, CLIENT_A)

    assert set(auth._RL) == {CLIENT_A}
    assert len(auth._RL[CLIENT_A]) == 2
    # Both stamps are the controlled instant, so the two requests really did share one window.
    assert set(auth._RL[CLIENT_A]) == {controlled_clock.monotonic}


def test_one_noisy_client_cannot_starve_another(controlled_clock):
    # The actual bug: pre-fix, client A's flood exhausted the shared bucket and B got a 429.
    # The clock is pinned for the whole flood, so A's own window cannot expire underneath it and
    # turn the 429 this asserts into a 302.
    client = _behind_proxy()
    for _ in range(20):  # rate_limit()'s default limit
        assert _login(client, CLIENT_A).status_code == 302
    assert _login(client, CLIENT_A).status_code == 429

    assert _login(client, CLIENT_B).status_code == 302
    # The pin held for all twenty: one instant, twenty stamps — A was refused on the count.
    assert set(auth._RL[CLIENT_A]) == {controlled_clock.monotonic}
    assert len(auth._RL[CLIENT_A]) == 20

    # A's window is the controlled one: crossing it lets A back in, with B's bucket untouched.
    controlled_clock.advance(60)
    assert _login(client, CLIENT_A).status_code == 302
    assert auth._RL[CLIENT_A] == [controlled_clock.monotonic]


def test_forwarded_for_is_ignored_from_an_untrusted_peer(controlled_clock):
    # A direct (non-proxy) caller must not be able to pick its own bucket by asserting a header.
    outsider = _behind_proxy(peer="198.51.100.4")
    assert _login(outsider, "192.0.2.250").status_code == 302

    assert set(auth._RL) == {"198.51.100.4"}
    # The stamp the peer's own bucket took is the controlled instant — the pin reached this leg.
    assert set(auth._RL["198.51.100.4"]) == {controlled_clock.monotonic}


def test_without_proxy_header_handling_every_client_shares_traefiks_bucket(controlled_clock):
    # Negative control: the bare app is exactly the pre-fix deployment, and it does collapse
    # the two clients into one bucket — so the assertions above are detecting the middleware,
    # not passing vacuously.
    bare = TestClient(app, client=(TRAEFIK, 41234))
    _login(bare, CLIENT_A)
    _login(bare, CLIENT_B)

    assert set(auth._RL) == {TRAEFIK}
    assert len(auth._RL[TRAEFIK]) == 2
    # Same pin as the positive cases above, so the collapse is the middleware's absence and not
    # two requests that happened to land in different windows.
    assert set(auth._RL[TRAEFIK]) == {controlled_clock.monotonic}
