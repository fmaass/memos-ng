#!/bin/sh
set -e

# `command: worker` selects the media worker; default runs the app.
if [ "$1" = "worker" ]; then
    exec python -m app.worker
fi

# Trust scope for X-Forwarded-For / X-Forwarded-Proto. Traefik is the ONLY ingress: the app
# container publishes no host port and is reachable solely on the `apps_net` / `internal` docker
# bridges, so nothing outside those private networks can open a socket to uvicorn and forge the
# header. That makes the private ranges (plus loopback) a sound trust boundary, and it must be an
# explicit list rather than `*` — a wildcard would still be wrong the day a port gets published.
# Without it uvicorn trusts 127.0.0.1 only, every request keeps Traefik's address, and the per-IP
# login rate limiter degrades into ONE bucket shared by the whole household (MEMNG-28).
# Override with FORWARDED_ALLOW_IPS to narrow it (e.g. Traefik's exact container IP).
FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7}"

# App: migrate then serve — unless this is a ROLLBACK onto a database a newer image already
# migrated. This image's migration graph does not contain that revision, so migrating to the
# bundled head would fail outright ("Can't locate revision") and the container would never serve. It
# is expand-only by policy, so the newer columns are simply ignored by this code. Set on the
# rollback deploy, cleared on the roll-forward (RELEASING.md, "Rollback across a migration").
if [ "${ROLLBACK_MODE:-0}" = "1" ]; then
    echo "ROLLBACK_MODE=1: not migrating; the schema is newer than this image"
else
    alembic upgrade head
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 \
    --proxy-headers --forwarded-allow-ips "$FORWARDED_ALLOW_IPS"
