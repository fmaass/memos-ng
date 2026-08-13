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
  Because that cookie is SameSite (**site**-scoped, so every `*.discomarder.live` sibling is
  same-site), one ASGI middleware (`app/csrf.py`) refuses any cookie-authenticated mutation whose
  `Origin`/`Referer` is not this app's own origin — which is read from `OIDC_REDIRECT_URI`, so a
  host change must update that one setting (as OIDC requires anyway). PAT/Bearer clients send no
  session cookie and are unaffected.
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
python3 scripts/check_release_version.py
cd frontend && npm run lint && npm run typecheck && npm run test && npm run build
```

Alembic reads its DSN from `DATABASE_URL_SYNC` only (`alembic.ini` carries no URL), so export
it — `export DATABASE_URL_SYNC=postgresql+psycopg2://memosng:memosng@localhost:5433/memosng` —
before `alembic upgrade head` / `alembic check`. pytest and the app fall back to that same DSN
on their own.

CI (`.github/workflows/ci.yml`) runs five jobs on every push/PR: **backend** (version
agreement, ruff, pyright, migrations + `alembic check`, pytest), **frontend**, **api-drift**
(regenerates the OpenAPI schema + client and fails on any diff), **image** (builds the
production Dockerfile — no push, no registry login — so a dependency that needs a compiler on
`python:3.13-slim` fails the PR instead of `main`), and **release** (main only).

## Build & deploy

The container runs the SPA + API by default, or the media worker with `command: worker`:

```dockerfile
ENTRYPOINT ["/docker-entrypoint.sh"]   # `worker` → media worker; otherwise: migrate, then serve
```

On push to `main`, CI builds `ghcr.io/fmaass/memos-ng`, Trivy-scans it (HIGH/CRITICAL gate
**before** publish), pushes `:latest` + `:sha-<commit>`, and uploads an SPDX SBOM. The running
container's `APP_VERSION` build-arg is `<version>+<short-sha>` — the `pyproject.toml` version
plus the 7-character commit — surfaced at `/health` and on the in-app settings page.

Production runs on Saturn (Synology NAS) as two compose services (`memos-ng-app` +
`memos-ng-worker`) sharing the household Postgres. Deploy is `git pull` of the stack repo
followed by `docker compose up -d` (never `restart` — it reuses the old image); verify the
running image digest and a live request before calling it done.

### Cutting a release

`pyproject.toml` `[project] version` is the **single version of record**;
`frontend/package.json`, `frontend/package-lock.json` (twice — root `.version` and
`.packages[""].version`) and `uv.lock` mirror it, and `README.md` documents it.
`scripts/check_release_version.py` enforces all of that: CI runs it in the **backend** and
**release** jobs, and the release procedure runs it once more against the tag itself — before
the tag exists. The lockfiles need their own check because nothing else has one: `npm ci`
compares dependency trees, not the root `version` field (it exits 0 against a stale lockfile
version), and `uv sync --frozen` skips freshness checks entirely.

```bash
# 1. bump the authority, then its mirrors
$EDITOR pyproject.toml                                    # [project] version = "X.Y.Z"
uv lock                                                   # refresh uv.lock's project version
cd frontend && npm version X.Y.Z --no-git-tag-version && cd ..

# 2. add the "### vX.Y.Z" changelog section to README.md

# 3. the tag must agree with the authority — this gate runs BEFORE the tag is created
#    (bare python3, not `uv run` — uv run would re-lock uv.lock and hide a stale one)
python3 scripts/check_release_version.py --tag vX.Y.Z

# 4. only then
git tag -a vX.Y.Z -m "..."
```

The workflow only fires on `main` pushes and pull requests, never on tag refs — so the
`--tag` agreement is the release procedure's gate, not CI's.

## Configuration

All settings come from the environment (or a `.env` file). Key variables — see
`app/config.py` for the complete, documented list:

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` / `DATABASE_URL_SYNC` | local dev DSN | async (app) + sync (Alembic) Postgres DSNs |
| `DATA_ROOT` | `./data` | media storage root (bind-mounted at `/app/data` in prod) |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `5` / `5` | connection cap — Postgres is **shared** with prod; never exhaust it |
| `SESSION_ROLLING_THROTTLE_S` | `600` | min interval between session `last_seen` refreshes (avoids a hot-row write convoy on the feed fan-out) |
| `PAT_LAST_USED_THROTTLE_S` | `300` | min interval between PAT `last_used` stamps (same hot-row reason, for a satellite's media burst) |
| `ASSET_URL_SECRET` | derived from `SESSION_SECRET` | HMAC key for signed media URLs (`_PREV` = rotation slot) |
| `WORKER_LANES` | `fast,slow` | which media lanes a worker drains (`fast` = thumbs/posters, `slow` = transcode) |
| `THERMAL_ZONE` / `THERMAL_MAX_C` | k10temp sensor / `70` | defer transcode while the (heat-bound) host is hot |
| `MAX_UPLOAD_MIB` | `1024` | upload size cap |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | empty (push disabled) | Web Push (VAPID) keypair |
| Authelia OIDC | — | confidential BFF client; public `https://auth…` endpoints only |

## Changelog

### v1.3.0

- **Feed — photos render at their real shape.** Tiles cropped every photo to a small square
  regardless of its intrinsic ratio. A single photo now renders uncropped at its own aspect
  ratio under a 35rem height cap, pairs share a two-column grid, larger sets reuse the
  justified-rows layout, blurhash placeholders follow the tile ratio, and image tiles open by
  keyboard exactly like video tiles. Home/Explore/Archived/Search share one content-width rule
  that grows from 672px to 896px on xl screens.
- **Media — right-sized fetches and honest zoom ceilings.** Tiles fetched the 800px thumb no
  matter how large the rendered box, and lightbox slides declared the original's dimensions
  while serving the 2048px mobile tier, so deep zoom rendered upscaled blur. Tiles now emit
  `srcset` with each tier's actually-served width (never upscaled, deduped when tiers coincide)
  and `sizes` stating the real rendered width per layout; slides declare the mobile tier's true
  pixels.
- **Dimension integrity end to end.** Width and height now persist only as a pair (a
  half-populated pair is skipped and counted, so a fresh read can't be paired with a stale
  value into an absurd ratio); the terminal media-ready SSE event carries owner-scoped width,
  height and blurhash, so a fresh upload leaves the fallback box without a reload; video tiles
  fall back to the neutral shape rather than trusting dimensions ffprobe records without the
  rotation tag.
- **Backfill for migrated attachments.** A one-shot script fills the NULL `width`/`height`/
  `blurhash` on attachments imported from the old memos (dimensions read from the original with
  the worker's EXIF-orientation semantics, blurhash from the served thumb so placeholders match
  the tiles). Writes are per-column `COALESCE` updates so a concurrent worker always wins;
  dry-run by default, `--execute` writes, `--limit` paces the run.
- **Frame share — one-shot #curated album backfill.** Album delivery is event-driven, so
  #curated memos predating the album integration — or delivered by the retired SMTP path — were
  absent from the Aura album. The backfill uploads them via the same render + upload path
  (print masters excluded, idempotent on re-run, dry-run by default).
- **Release engineering.** One authoritative version source (`pyproject.toml`, checked against
  `frontend/package.json` and this changelog by `scripts/check_release_version.py`);
  `alembic check` is now a CI gate, so a model change without a migration fails the build; the
  production image is built on every PR, so a dependency needing a compiler on
  `python:3.13-slim` can no longer reach `main` unseen; `APP_VERSION` is `<version>+<short-sha>`
  instead of a bare commit SHA. The release procedure itself is now codified in `RELEASING.md`
  (version manifest, CI gates, jupiter deploy and image-retag rollback).
- **Docs.** The frame-api-upload plan is tracked in git alongside its siblings, and the
  photo-book print tier + export API is specified (measured evidence, 4096px geometry, encode
  profile).

### v1.2.0

- **Aura frame share for #curated photos.** The worker renders image attachments of `#curated`
  memos and delivers them to the household Aura frame, driven by a `frame_share_jobs` queue
  whose `claimed` tombstone commits before the send (at-most-once, never resend). Renders are
  capped at a 2048px longest edge — production backfill showed full-res exports at ~8600px /
  82MB producing 20-46MB JPEGs that iCloud rejects and that blow Aura's ~9.5MB cap — and carry
  `DateTimeOriginal` stamped from the **memo's** date, not `attachment.captured_at` (mostly
  NULL, else the upload time), so frame ordering is truly chronological.
- **Frame-share hardening (review round).** Drain errors are isolated from the worker loop
  (a DB error no longer crashes the whole worker and halts media processing); terminal
  `skipped`/`failed` jobs resurrect on re-enqueue while `sent` stays terminal; render failures
  fail terminally on the first attempt instead of retrying a corrupt file five times; the
  enqueue runs inside a SAVEPOINT so an optional feature can never 500 the memo write path; the
  Aura-address predicate is enforced at every read point, not just the PATCH validator; and the
  settings UI surfaces save errors instead of swallowing them.
- **Delivery moved from SMTP to the aura-frames album API.** The send step is now an HTTP
  upload to the system-level album, which owns delivery (200 dedup and 201 both success;
  transient errors retry with backoff, other 4xx fail terminally). The dead SMTP path, the
  per-user Aura settings UI, the frame-test endpoint and `aiosmtplib` were then removed;
  `users.frame_send_to/from` are kept nullable for one rollback release. Realizes ADR-0002.
- **Search — filter chips + date range (Tier A1).** A pure `MemoFilter` serialization module
  maps filter ↔ query ↔ URL, with day-granularity dates translated to the backend's strict
  bounds (no epsilon, no spillover). The new filter bar edits tags, visibility, date range,
  archived and free text as removable pills, syncs into the URL (linkable and reload-stable),
  and re-seeds on query-only navigation. Home's hand-rolled `?tag` handling now parses through
  the same module, so Home and Search agree on one serialization.
- **Media loading resilience.** The SSE lifecycle is client-managed: the events stream opens
  only while attachments are in flight, closes when idle, and invalidates the memo caches once
  per reconnect — so a `ready` event that lands during a disconnect no longer strands a tile at
  "Processing" until a full reload. Thumbnail retries back off (1s/4s) instead of exhausting on
  any one-second blip, the Resources grid gets the feed's failed-tile recovery, and because
  signed URLs are bucket-quantized (a re-mint inside the same bucket returns an identical URL),
  the browser's `online` event now triggers a fresh attempt.
- **Docs.** Phase 4 deploy findings (digest pins, paced backfill, the 2048px cap superseding
  the full-res decision) and the post-cutover feature-parity roadmap vs upstream memos 0.29.1.

### v1.1.0

- **Performance — front-page load convoy fixed.** `require_user` wrote `sessions.last_seen` +
  committed on every authenticated request, so the feed's concurrent fan-out lock-convoyed one
  hot session row (measured 3.7 s for a 6-request burst). The refresh is now throttled
  (`SESSION_ROLLING_THROTTLE_S`, in-process gate + conditional UPDATE) — warm `/api/memos` ~30 ms,
  burst ~0.2 s. The unused per-page `COUNT` is opt-in (`?with_total`); `totalCount` is now nullable.
- **UI.** Removed the redundant header username text + in-app logout button (login is Authelia SSO).
  iOS PWA lightbox: top toolbar clears the Dynamic Island, and the download/delete/print buttons use
  PhotoSwipe's native icon pipeline (correctly aligned + sized). Thumbnails recover from load
  failures (bounded retry + feed re-mint) and decode off the main thread.
- **API.** The asset download route accepts a PAT (original/print tier).

### v1.0.0

First production release.

## License

Private household project. Not licensed for redistribution.
