#!/bin/sh
set -e

# `command: worker` selects the media worker; default runs the app.
if [ "$1" = "worker" ]; then
    if [ "${APP_ROLE:-home}" != "home" ]; then
        echo "APP_ROLE=$APP_ROLE runs no media worker" >&2
        exit 64
    fi
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

# The ingest roles (trip pass P2) serve their own app on the public VPS. `ingest` is the guest
# door behind cloudflared and `ingest-home` the interface home pulls from over its port forward.
# Neither trusts a forwarded header from uvicorn (the door reads CF-Connecting-IP itself, by
# setting) and neither writes an access log, because the pass secret travels in the /p/ path. Only
# the door migrates by default, so the two processes on one database never race on alembic.
# Both listen on 0.0.0.0:8000 inside their containers: what keeps ingest-home off the internet is
# its host publish on 127.0.0.1 and its networks, not a loopback bind, which a docker publish could
# never reach.
APP_ROLE="${APP_ROLE:-home}"
case "$APP_ROLE" in
    home)
        MIGRATE=1 ;;
    ingest)
        MIGRATE="${RUN_MIGRATIONS:-1}" ;;
    ingest-home)
        MIGRATE="${RUN_MIGRATIONS:-0}" ;;
    *)
        echo "unknown APP_ROLE=$APP_ROLE" >&2
        exit 64 ;;
esac

# Compose file secrets, ingest roles only: the token and the database password are read from
# their files here, so neither has to sit in an env file. The password is URL-quoted into both
# database URLs; DATABASE_HOST, DATABASE_NAME and DATABASE_USER name the rest.
if [ "$APP_ROLE" != "home" ]; then
    if [ -n "${INGEST_SERVICE_TOKEN_FILE:-}" ]; then
        INGEST_SERVICE_TOKEN=$(cat "$INGEST_SERVICE_TOKEN_FILE") || exit 64
        export INGEST_SERVICE_TOKEN
    fi
    if [ -n "${DATABASE_PASSWORD_FILE:-}" ]; then
        pw=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(open(sys.argv[1]).read().strip(), safe=""))' \
            "$DATABASE_PASSWORD_FILE") || exit 64
        dsn="${DATABASE_USER:?DATABASE_USER is required with DATABASE_PASSWORD_FILE}:$pw@${DATABASE_HOST:?DATABASE_HOST is required with DATABASE_PASSWORD_FILE}/${DATABASE_NAME:?DATABASE_NAME is required with DATABASE_PASSWORD_FILE}"
        DATABASE_URL="postgresql+asyncpg://$dsn"
        DATABASE_URL_SYNC="postgresql+psycopg2://$dsn"
        export DATABASE_URL DATABASE_URL_SYNC
        unset pw dsn
    fi
fi

# Migrate then serve — unless this is a ROLLBACK onto a database a newer image already
# migrated. This image's migration graph does not contain that revision, so migrating to the
# bundled head would fail outright ("Can't locate revision") and the container would never serve. It
# is expand-only by policy, so the newer columns are simply ignored by this code. Set on the
# rollback deploy, cleared on the roll-forward (RELEASING.md, "Rollback across a migration").
# One migration line for every role keeps the single-migrator pin (MEMNG-86) a count of one.
if [ "${ROLLBACK_MODE:-0}" = "1" ]; then
    echo "ROLLBACK_MODE=1: not migrating; the schema is newer than this image"
elif [ "$MIGRATE" != "1" ]; then
    echo "APP_ROLE=$APP_ROLE: not migrating (RUN_MIGRATIONS=$MIGRATE)"
else
    alembic upgrade head
fi
if [ "$APP_ROLE" != "home" ]; then
    exec uvicorn app.ingest.asgi:app --host "${BIND_HOST:-0.0.0.0}" \
        --port "${BIND_PORT:-8000}" --no-proxy-headers --no-access-log
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 \
    --proxy-headers --forwarded-allow-ips "$FORWARDED_ALLOW_IPS"
