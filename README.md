# memos-ng

A from-scratch replacement for [Memos](https://github.com/usememos/memos), rebuilt on the
household default web stack. memos-ng is a self-hosted, phone-first family photo & memo app:
a FastAPI backend serving a Vue 3 single-page app, an async media pipeline, and
Authelia-backed single sign-on — all from one image.

It replaces the previous production deployment (`neosmemo/memos:0.29.1` plus its `nginx`,
`imgproxy`, and `resizer` sidecars and a separate NFS transcoder). **All of that collapses
into two containers from one image:** `memos-ng-app` (SPA + API + asset serving) and
`memos-ng-worker` (media derivation), backed by Postgres.

> Status: **live in production** at `https://memos.discomarder.live` (Phase-5 cutover complete).

## Architecture

```
                    ┌─────────────────────────────────────────┐
   Browser / PWA ──▶│ memos-ng-app (FastAPI + uvicorn)         │
                    │  • Vue 3 SPA (built, served as static)   │
                    │  • REST API  /api/*                       │
                    │  • compat    /api/v1/* · /file/*          │
                    │  • SSE       /api/events                  │
                    │  • Authelia OIDC BFF (httpOnly session)   │
                    └───────────────┬───────────────┬──────────┘
                                    │               │
                       Postgres ◀───┘               └──▶ signed-URL media bytes
                       (memos + media_jobs queue)        (HMAC, DB-free serve path)
                                    ▲
                    ┌───────────────┴──────────────────────────┐
                    │ memos-ng-worker (command: worker)         │
                    │  • drains the media_jobs queue            │
                    │    (FOR UPDATE SKIP LOCKED)               │
                    │  • Pillow/pillow-heif thumbs + posters    │
                    │  • ffmpeg H.264 transcode (slow lane)     │
                    │  • thermal-guarded (Saturn is heat-bound) │
                    └───────────────────────────────────────────┘
```

- **Backend:** FastAPI, SQLAlchemy 2 (async), Alembic, pydantic-settings, structlog. `uv` +
  `ruff` + `pyright`, `python:3.13-slim`.
- **Frontend:** Vue 3.5 + PrimeVue 4 (Aura, `.app-dark`) + Tailwind v4, Vite SPA, TanStack
  Query for server state. The API client types are **generated** from the OpenAPI schema
  (`openapi-typescript`) — never hand-written (ADR-0009); CI fails on drift.
- **Auth:** Authelia OIDC via a confidential **BFF** — tokens stay server-side behind an
  httpOnly rolling session cookie. Token/JWKS are fetched over public `https://auth…` URLs.
- **Media:** one image, two roles. The worker derives immutable, uid-keyed tiers; the app
  mints short-lived HMAC-signed URLs and serves the bytes with no DB or auth dependency.
  HEIC/HEVC originals are kept as the archive master (ADR-0008).

## Repository layout

| Path | What |
|------|------|
| `app/` | FastAPI app — `main.py`, `worker.py`, `routers/`, `services/`, `models.py`, `config.py` |
| `app/routers/` | `memos`, `assets`, `uploads`, `auth`, `settings`, `pats`, `push`, `export`, `compat` |
| `app/services/` | `storage`, `media`, `uploads`, `memos`, `variants`, `asset_urls`, `push`, … |
| `alembic/` | database migrations |
| `frontend/` | Vue 3 SPA (Vite); `src/api/schema.d.ts` is generated from `openapi.json` |
| `scripts/` | `gen-openapi.py` (schema dump) and ops/migration helpers |
| `tests/` | pytest suite (backend); frontend tests live under `frontend/src/**/__tests__/` |
| `Dockerfile` | two-stage build (Node frontend → python:3.13-slim runtime) |
| `docs/plans/` | the roadmap of record |

## Local development

Requires [uv](https://docs.astral.sh/uv/), Node 22, and Docker.

```bash
# 1. Postgres for local dev (host port 5433, matches the default DSN)
docker compose -f compose.dev.yaml up -d

# 2. Backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload         # http://localhost:8000

# 3. Frontend (separate terminal)
cd frontend
npm ci
npm run dev                                   # Vite dev server, proxied to :8000
```

Regenerate the API client after any backend schema change:

```bash
uv run python scripts/gen-openapi.py          # writes openapi.json
cd frontend && npm run gen:api                # writes src/api/schema.d.ts
```

## Tests & quality gates

The full local gate (must be green before pushing):

```bash
uv run ruff check app/ tests/ scripts/
uv run pyright app/
uv run pytest -q
uv run alembic check
cd frontend && npm run lint && npm run typecheck && npm run test && npm run build
```

CI (`.github/workflows/ci.yml`) runs four jobs on every push/PR: **backend**,
**frontend**, **api-drift** (regenerates the OpenAPI schema + client and fails on any diff),
and **release** (main only).

## Build & deploy

The container runs the SPA + API by default, or the media worker with `command: worker`:

```dockerfile
ENTRYPOINT ["/docker-entrypoint.sh"]   # `worker` → media worker; otherwise: migrate, then serve
```

On push to `main`, CI builds `ghcr.io/fmaass/memos-ng`, Trivy-scans it (HIGH/CRITICAL gate
**before** publish), pushes `:latest` + `:sha-<commit>`, and uploads an SPDX SBOM. The running
container's `APP_VERSION` build-arg is the commit SHA (surfaced at `/health`).

Production runs on Saturn (Synology NAS) as two compose services (`memos-ng-app` +
`memos-ng-worker`) sharing the household Postgres. Deploy is `git pull` of the stack repo
followed by `docker compose up -d` (never `restart` — it reuses the old image); verify the
running image digest and a live request before calling it done.

## Configuration

All settings come from the environment (or a `.env` file). Key variables — see
`app/config.py` for the complete, documented list:

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` / `DATABASE_URL_SYNC` | local dev DSN | async (app) + sync (Alembic) Postgres DSNs |
| `DATA_ROOT` | `./data` | media storage root (bind-mounted at `/app/data` in prod) |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `5` / `5` | connection cap — Postgres is **shared** with prod; never exhaust it |
| `ASSET_URL_SECRET` | derived from `SESSION_SECRET` | HMAC key for signed media URLs (`_PREV` = rotation slot) |
| `WORKER_LANES` | `fast,slow` | which media lanes a worker drains (`fast` = thumbs/posters, `slow` = transcode) |
| `THERMAL_ZONE` / `THERMAL_MAX_C` | k10temp sensor / `70` | defer transcode while the (heat-bound) host is hot |
| `MAX_UPLOAD_MIB` | `1024` | upload size cap |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | empty (push disabled) | Web Push (VAPID) keypair |
| Authelia OIDC | — | confidential BFF client; public `https://auth…` endpoints only |

## License

Private household project. Not licensed for redistribution.
