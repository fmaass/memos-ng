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
uv run ruff format --check app/ tests/ scripts/
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

There is no CI — the LOCAL gate is the gate (see `RELEASING.md`): pyright, ruff, pytest and
`alembic check` run bare with their exit codes cited, plus the frontend suite and the
api-drift check (`python3 scripts/gen-openapi.py` then `npm run gen:api` in `frontend/` must
produce no diff). A change touching
`pyproject.toml` or the `Dockerfile` additionally needs a local image build — through
`scripts/build-image.sh <version>+<short-sha>`, never a bare `docker build` — because a
dependency that needs a compiler fails on `python:3.13-slim` but not on a dev machine.

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
| `THERMAL_ZONE` / `THERMAL_MAX_C` | empty (the `coretemp` `Package id 0` sensor, resolved by name) / `90` | defer media work while the CPU package is above the limit; a path in `THERMAL_ZONE` overrides the by-name lookup |
| `SLOW_TRANSCODE_TIMEOUT_S` | `3600` | ceiling for one slow-lane transcode; on expiry the ffmpeg process tree is killed and the job fails visibly instead of holding the worker slot |
| `MAX_UPLOAD_MIB` | `1024` | upload size cap |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | empty (push disabled) | Web Push (VAPID) keypair |
| Authelia OIDC | — | confidential BFF client; public `https://auth…` endpoints only |

## Changelog

### v1.7.2

- `/places` no longer carries a timeline. The map itself is unchanged: pins, clusters, the cluster
  drawer and the viewer all stay, and the whole located set is on the map at once. The playhead,
  its histogram, the play/speed controls and their keyboard shortcuts are gone, and the drawer and
  the grid read the same one set the map shows.
- A worker told to stop hands its in-flight media work back to the queue. On the SIGTERM a
  `--force-recreate` sends, the supervisor tears both consumers down and each cancelled job's row
  returns to `queued` with its attempt count untouched — provided that row is still `processing`
  under this worker's own claim, so a job that had already finished, failed or been deleted is left
  as it is. The next worker start re-runs the requeued job, and the per-attachment order guard then
  stops withholding that attachment's later jobs. The limit is honest: the stop path's bounds are
  designed, not guaranteed, and docker's stop grace is 10 seconds. A stop that outlasts the grace
  ends in SIGKILL, and a claim that was in flight then stays `processing` until the stale-claim
  sweep requeues it — `MEDIA_CLAIM_STALE_SECONDS` (7200 s) plus up to one sweep interval. Deploy
  the worker in a quiet window, which is what keeps that case off the table.
- Every housekeeping step the worker supervisor awaits has a total bound. Each of those steps is
  time between two heartbeat stamps, so an unbounded one — the hosted-media retention sweep
  against a slow remote is the case this was raised on — could let the healthcheck call a working
  worker dead and recreate the container mid-job. A step that reaches its bound is logged by name
  and the loop carries on with the same answer its own error isolation would have given. The
  bounds sum to under half the healthcheck's staleness bar, and a test holds them there.
- The print-ready encode runs off the event loop. A 40–60 MP decode, reduction and re-encode used
  to hold the worker's one event loop for its whole duration, freezing the other consumer's
  progress writes and the supervisor's heartbeat; it now occupies one worker thread instead, at
  the same CPU cost. A stop waits out the rendition already being encoded: a thread cannot be
  interrupted, and an abandoned one would go on writing the rendition into its final path after
  the coroutine that fences a photo deleted mid-job is gone.
- The reclaim reads the attachment's current state under its own lock. It re-selected the row FOR
  UPDATE but then decided from the copy the caller's session had already loaded, so a photo
  re-attached to another memo mid-run was deleted anyway and the read-back reported success. Both
  implementations now read the row's current columns under the lock, which is what makes the
  re-read a revalidation rather than a formality.
- The app's loggers stay alive when alembic loads inside the app process. Alembic configures
  logging through `fileConfig`, which by default disables every logger that already exists, and in
  the app process those loggers carry the privacy filters — so the migration run at container boot
  could silently switch redaction off.
- The web client says so when the server stops answering: a banner offering an immediate retry
  while requests keep failing, and a short notice once it is reachable again. The verdict comes
  only from real traffic — a request's outcome, or the health probe — never from
  `navigator.onLine`, which stays true when what dropped is the path between the device and the
  server.
- Release tooling: a build meant only to prove the slim image still compiles can leave the deploy
  tag alone, with `IMAGE_TAG=memos-ng:<name>` in front of `scripts/build-image.sh` — same lock,
  same refusal of a tree that differs from `HEAD`, and `memos-ng:local` keeps naming the image
  that is running, which is what the rollback step depends on. A pytest run now claims its database
  with a session-level advisory lock, so a second run against the same database refuses up front
  instead of dropping the first run's tables mid-flight, and the idle-in-transaction test has room
  for a loaded host.
- Documentation: the release procedure carries the production backup step for any release with a
  migration — a custom-format dump, its checksum and table of contents, and a restore into a
  throwaway database whose counts are compared with production — and the settings table describes
  the thermal guard the code has, the CPU package sensor resolved by name and a 90 °C limit,
  instead of the previous host's sensor and its 70 °C.
- No migration, and the API contract is unchanged.

### v1.7.1

- The Places map shows its icons. MapLibre does not resolve a root-relative sprite address, so
  the sprite sheet 1.7.0 named was never requested; the style now carries absolute addresses on
  the page's own origin for the sprite and the glyphs, and the unit tests pin both.
- The first view of `/places` keeps every pin clear of the timeline bar. The padding of the first
  fit is measured from the overlay that really covers the map, per side, on desktop and phone.
- The empty-map message appears only once the markers have been asked for and came back empty.
  While they load that message stays away, and a failed request has a message of its own.
- Photos uploaded while a long video is transcoding no longer wait for it. The one worker process
  drains the queue with two consumers, `transcode` (a slow-lane `derive` job on a video) and
  `light` (everything else), split by a predicate at claim time; nothing is stored, so there is no
  migration. A per-attachment order guard keeps one attachment's jobs in sequence. The supervisor
  owns the heartbeat and stamps it while both consumers are alive and inside their inactivity
  bound, so a transcode longer than the healthcheck bar no longer makes a working worker look
  dead. `WORKER_CONCURRENCY`, which nothing read, gives way to `WORKER_TRANSCODE_SLOTS`,
  `WORKER_LIGHT_SLOTS`, `WORKER_CPU_BUDGET` and `WORKER_TRANSCODE_CPU_COST`, checked at startup.
- The thermal guard reads the CPU package sensor by its hwmon name instead of a fixed thermal-zone
  number, which on the worker's host was not the CPU; it says at startup which sensor it
  resolved, and its limit is 90 °C, set for the host the worker runs on. A host without that sensor reports UNKNOWN
  and keeps working.
- `scripts/tiles/extract.sh --user uid:gid` publishes the basemap archives as the owner of the
  tiles share: with it, every write into the tiles directory runs in a container as that user.
- Release tooling: `npm run smoke:places` opens the production build of `/places` in a real
  headless browser on a desktop and a phone viewport and is a gate for every release that touches
  `frontend/`; `scripts/check-mutations.sh` and the new `scripts/build-image.sh` share a lock, so
  a mutation patch can never be copied into an image.
- No migration, and the API contract is unchanged.

### v1.7.0

- `/places` puts every located photo and video on a map. The coordinates come from the originals
  the library already holds: each one is read once at upload, from the file's own metadata, and
  kept in three nullable columns on the attachment row. Nothing else ever sees a coordinate — the
  markers are served through one authenticated endpoint, the derived tiers and prints keep
  stripping GPS, every transcode and poster now drops container metadata, and the log lines carry
  counts rather than values.
- The basemap is served from the app's own disk, so the browser asks no third party for a map: two
  immutable PMTiles archives cut from the daily Protomaps build (the whole world to z7, street
  detail to z15 over the regions that actually hold pins) plus the style's fonts and sprites,
  pinned by commit and versioned in their URLs so a bumped pin reaches a client instead of sitting
  behind a week-long immutable cache header.
- The map itself clusters the pins natively and fits its first view to them. A cluster opens a
  drawer with its photos and a Zoom-in button; a single pin opens the lightbox, which offers "Show
  in memo". On a phone the map acts as a filter above a grid of the photos in view. The map chunk
  and its worker load only on `/places` and are kept out of the service worker's precache.
- A bar under the map plays the located photos through time: a per-month histogram with
  proportional year ticks, a playhead that moves at 1×, 2× or 4×, and a choice between
  accumulating the pins and a rolling twelve-month window. Each step hands the map the playhead's
  subset, so the clusters are re-computed rather than filtered, and the in-view grid and the phone
  layout follow the playhead. Opening the photo viewer pauses playback.
- A HEIC original's Exif is read from its box headers instead of through a full decode: the walker
  follows the file's boxes to the primary image's Exif item and reads only that extent. On the 4 MB
  fixture that is 597 bytes instead of 4,195,036, so the read deadline no longer depends on the
  size of the picture, and a layout the walker does not handle falls back to the existing full read
  rather than losing a date or a location.
- Every optimized JPEG save sizes libjpeg's write buffer for the call — bytes per pixel over the
  16-pixel blocks a JPEG actually codes, plus the Exif and colour-profile payloads — and grows it
  and re-encodes if it still overflows. A high-entropy image that used to fail the print or mail
  encode with "broken data stream when writing image file" now saves, and optimize stays on.
- Both native exchange endpoints, the login exchange and the step-up exchange, carry the same rate
  limit as the login legs: twenty requests per sixty-second window per client address, refused with
  429. Defence in depth — a handoff code was already single-use and bound to its verifier.
- A memo's attachments publish the capture read the server already performed: `capture_instant`,
  `capture_civil` and `capture_source` are additive and nullable beside the unchanged
  `captured_at`, so a client can tell an exact date from an approximate one after the fact, and the
  memo card and permalink show the memo's date plainly.
- The OpenAPI document was narrowed to what the server already does, and rewritten for the clients
  that read it: the 404 a vanished memo answers on PATCH and DELETE is declared, the upload-offset
  HEAD is declared bodyless, the four native self-service routes are published as bearer-only, and
  every exported description now says what a thing means to a client instead of carrying tracker
  keys, repository paths or internal names. One wire-visible consequence: a session-cookie caller
  sending an invalid body to the native self-service lane is answered 401 before body validation,
  where it used to see 422.
- Every runtime dependency gained an upper bound one major above the version the lock resolves
  (structlog and tzdata stay unbounded on purpose — their major is a calendar year, and a ceiling
  would hold back time-zone data), and the image build takes uv from a pinned tag and digest rather
  than `latest`.
- `ruff format` is adopted across `app/`, `tests/` and `scripts/` with its settings written down,
  and `ruff format --check` joins the gate, so restyling noise stops riding along in diffs.
- The tests that redeem a handoff code or exercise the limiter run against a controlled clock that
  proves itself: the fixture counts every application read of the pinned clock and fails a test by
  name when the application never read it.
- `reconcile --victim <id>` exits nonzero with the reason on stderr when it reclaims nothing — 2
  for an empty leaked set or an id outside it, 3 for a victim another run took first, 1 for a file
  that could not be removed — instead of reporting success for a run that did nothing.

Operator notes:

- One expand-only migration, `d3f81b06c9a4`: three nullable columns on `attachments` (`latitude`,
  `longitude`, `location_source`) and two CHECK constraints, one forbidding half a coordinate and
  one refusing an out-of-range magnitude. Both hold trivially for every existing row, all of which
  read NULL.
- The existing library's coordinates come from a one-off run of
  `python -m app.scripts.backfill_location`, which is a dry run unless given `--execute`, resumes
  where a killed run stopped (`location_source` is its ledger), and with `--regions-out` writes the
  GeoJSON of the 1° cells holding pins — the input the basemap extraction needs.
- The basemap is published before the map is useful: `scripts/tiles/fetch-assets.sh <tiles-dir>`
  for the fonts and sprites, `scripts/tiles/extract.sh <tiles-dir> <regions.geojson>` for the two
  archives. The directory is `<DATA_ROOT>/assets/tiles` unless `TILES_DIR` overrides it.
- Rolling back across this migration is one environment variable from 1.7.0 onwards:
  `ROLLBACK_MODE=1` on both services makes the entrypoint skip the migration and the worker's
  schema gate accept a head its graph does not know. Production never downgrades; the rollback
  keeps the new columns. See RELEASING.md § "Rollback across a migration".
- The Content-Security-Policy gained `worker-src 'self' blob:`, which is what lets MapLibre decode
  vector tiles in a web worker, and nothing else.

### v1.6.2

- Every path that finalizes or consumes an upload now takes its row locks in one total order — the
  memo, then the upload, then the job, then the attachment, then the rendition rows, ascending by
  id in a single statement. That closes the deadlock cycles between a memo create, a standalone
  finalize and a PATCH that shared a deduplicated attachment, and a dedup candidate is judged on
  the row the locking statement returned rather than on one read before the lock.
- Deleting a memo locks the memo row first, so a PATCH or DELETE whose memo vanished in between
  answers 404 instead of a 500 or a silent 204.
- The expiry reaper claims an upload under its row lock before unlinking its part, so a finalize
  in progress can no longer lose its file; an append whose upload row is gone answers 404.
- `docs/LOCK-ORDER.md` states the protocol, `docs/lock-census.tsv` lists every explicit lock site
  with a test that proves the list is complete, and `scripts/check-mutations.sh` runs the
  repo-carried mutation controls and requires each to fail exactly its named probes.
- No migration, and the API contract is unchanged: the new 404s are runtime answers, and
  documenting them is a later change.

### v1.6.1

- A memo whose line is nothing but hashtags no longer becomes the digest mail's heading. The mail
  applies the same tag-only-line rule the web view does — one shared vector file holds the rule and
  its cases for both — and shows the memo's tags as chips below its body, and as a tag line below
  the text in the plain-text alternative that archive search reads.
- The part-splitting estimate counts a memo's rendered body: its HTML, its chip row (each tag name
  measured as the template escapes it) and its tag line. A digest carrying no pictures was
  previously estimated at zero bytes however much text it held, and could be sent as one message
  over the configured size cap.
- The size of the mail's inline pictures is pinned by tests that decode the bytes the message
  carries, for a landscape and a portrait source, together with the layout heights they produce.
- A hosted video's poster keeps its box when its image has not been downloaded yet: the anchor
  around it is block-level and bounded to the layout width, instead of shrinking to the size of the
  play badge.

### v1.6.0

- The daily photo digest moves into memos-ng, replacing the decommissioned satellite: settings, recipients, runs and per-recipient deliveries live in the database behind an admin-only API, and the worker selects, renders, hosts and sends on a Europe/Zurich calendar gate, with videos hosted through Dropbox links held in a durable ownership ledger. The feature ships **disabled**; the cutover that imports the satellite's registry and enables it is a separate step.
- `/settings` gains General and Mail tabs: admins manage digest recipients, trigger a manual run and read run history from the Mail tab, which also deep-links as `/settings?tab=mail`. Non-admins see no Mail tab.
- Digest observability: `memosng_digest_*` gauges and counters on `/metrics`, guarded by a staleness rule that publishes nothing once the snapshot is older than 300 s — a dead refresher trips an absence alert instead of freezing a healthy-looking `enabled=0`.
- Print and mail image profiles are editable in a new Export settings tab, an export renders every image in a date range through the print profile, and the digest's pictures go out through the mail profile of the same pipeline.
- Two expand-only migration chains: the five digest tables, and `export_settings` plus `media_jobs.profile_key`.

### v1.5.2

- The HDR poster tone-map uses the hable operator: mobius, shipped hours earlier in 1.5.1, blew ~7% of pixels past Y=250 on a real clip and read as an overdone HDR look on the device; hable restores the same chroma and clips essentially nothing on either test clip.

### v1.5.1

- An HDR video's poster frame is tone-mapped to BT.709 SDR at derivation: HLG and PQ sources are linearised, tone-mapped and written back as SDR, so the still on the tile matches the clip that plays instead of looking flat and washed out. SDR sources keep the byte-identical extraction command.
- `app.scripts.backfill_hdr_posters` re-derives the posters of HDR videos already on disk — dry run by default, one attachment at a time with `--uid`, the file it replaces copied aside first, the blurhash and the capture-date mtime restamped, and a completion marker that makes a rerun a no-op.
- Every API datetime is serialized in one canonical shape, `YYYY-MM-DDTHH:MM:SSZ` in UTC, with no sub-second part.
- A memo permalink resolves to the memo again: the route is ordered so a literal path segment can no longer shadow it.
- The upload queue is released when the scope that owns it goes away, not only on an explicit clear.

### v1.5.0

- A device can read and rename its own token over the bearer lane (`GET`/`PATCH /api/auth/native/token`), strictly self-scoped through the presenting token's hash.
- Step-up token management: a `purpose=manage` sign-on round trip mints a short-lived hashed grant that alone authenticates `/api/pats/*` (list, create, revoke). Login and manage codes cannot be cross-redeemed, and a grant presented beside a session cookie is refused outright.
- One expand-only migration: `management_grants`, and `purpose` on `native_auth_codes` (existing rows read as login codes).

### v1.4.1

Media library: the owning memo on each item, and oldest-first paging (2026-08-27).

**Added**
- `GET /api/attachments` items carry `memoUid` — the memo the attachment hangs on, `null` for
  orphans. Read from the row the visibility join already produces, so a page costs no extra
  query (MEMNG-171).
- `GET /api/attachments?order=asc` walks the same keyset oldest-first; `order=desc` (the default)
  is unchanged. The cursor is unchanged and direction-agnostic (MEMNG-190).

### v1.4.0

Photo-book print tier, streaming export, and media-worker reliability (2026-08-17).

**Added**
- Streaming export pipeline carrying media payloads (#73).
- Photo-book print tier: a 4096-px on-demand `printready` rendition + `GET /api/exports/photobook`
  manifest/archive API; adds the additive `media_jobs.kind` column (#74).
- Print-master management in the memo view (#75).

**Changed**
- Bare `- glossy` / `- matte` attachments are recognized as print variants and bind to their screen
  sibling instead of duplicating (#76).
- Worker startup is gated on the migrated schema so a deploy can't run the worker against an
  un-migrated DB (#77, MEMNG-86).

**Fixed**
- Media worker no longer holds a DB transaction open across the transcode: the claim is committed
  before processing, the result is written in a fresh transaction (re-locking the job row before the
  attachment to avoid a delete-path deadlock), a stale-claim sweep recovers a crashed worker's job,
  and the failure recorder rolls back first so attempts increment — fixing the idle-in-transaction
  queue wedge (#78, MEMNG-90).

### v1.3.4

API auth patch (2026-08-14).

**Changed**
- `GET /api/attachments/{uid}` accepts a Personal Access Token as well as a session cookie,
  so a headless client that may already download the attachment's bytes can read the same
  attachment's metadata (including `captured_at`). Visibility rules and the response shape
  are unchanged.

### v1.3.3

Frontend correctness patch (2026-08-14).

**Fixed**
- Clicking a rendered task checkbox toggles the correct source line: the enumeration is
  now the renderer's own parse, so blockquoted tasks count, fenced literals don't, and
  ordered-list tasks and marker-on-previous-line boxes toggle correctly.
- A failed print upload from the memo feed shows an error toast; both print-upload call
  sites share one mutation with unified feedback.
- Clearing the upload queue releases every preview object URL and File reference - on
  save, on re-edit, and on the edit Cancel button - instead of leaking them for the
  page's lifetime.
- Videos tile at their probed aspect ratio now that dimensions respect rotation; the
  neutral fallback box remains only for videos with no usable dimensions.
- The responsive sizes attribute accounts for the sidebar between 768 and 912px, so the
  browser stops fetching images up to 240px wider than the layout can render.

### v1.3.2

Media reliability patch (2026-08-13).

**Fixed**
- The upload reaper commits its deletions - expired rows stop resurrecting every cycle
  (their part files had already been unlinked). Spool files of live sessions are never
  reaped regardless of age; only sessions that are gone or expired lose theirs.
- Frame-share jobs stranded in `claimed` by a worker crash recover: claims carry a lease,
  a startup + hourly sweep re-queues stale claims with attempt accounting (failing visibly
  at the ceiling), and every post-claim write is fenced on the lease so a superseded
  attempt cannot overwrite a newer one. Pre-existing stranded jobs recover on this deploy.
- One poisoned media job can no longer crash-loop the worker: error text is sanitized
  (NUL bytes included), the failure record commits under its own guard, and the media
  branch is isolated like the frame-share drain always was.
- Slow-lane transcodes are time-bounded: expiry kills the whole process group (leader,
  descendants, zombie-aware), unlinks partial output, and fails the job visibly.
- Deleting an attachment no longer blocks behind an in-flight transcode's row lock -
  deletion returns promptly and the worker discards the orphaned job as a logged outcome.
- Video dimensions respect the rotation tag (both metadata forms), so portrait phone
  videos report their displayed shape; probe, poster and transcode all measure the same
  stream, and cover art can no longer be mistaken for the video.
- Transcode progress is real end to end: per-job, visible mid-transcode through a
  committed side channel, monotonic within an attempt, and rendered live in the feed.
- Async request handlers no longer make inline blocking filesystem calls; an AST guard
  keeps the class closed.
- Print handling: a re-upload with the same client filename replaces the stored print
  in place instead of orphaning numbered duplicates, and a print uploaded minutes after
  its screen binds onto the existing photo instead of appearing as a duplicate - the
  cause of duplicated images in the daily mail.
- PAT last_used stamping runs off the request path (no pooled-connection hold-and-wait).

### v1.3.1

Security hardening patch (2026-08-13).

**Security**
- Upload size limits enforce at chunk-append time: bodies stream to a private per-request
  spool with a running byte count (413 mid-stream on overshoot), offset gaps and replays
  answer before any body is consumed, and the part-file merge runs under a row lock so a
  failing request can only ever discard its own bytes.
- The `/file/` compat surface honors memo visibility: anonymous requests see only public
  memos' derived tiers, flips and deletions revoke on the next request, and every denial
  is byte-identical to an unknown uid. Served tiers carry `Cache-Control: private, no-store`.
- Upload sessions are bound to their creating user (additive migration): cross-user probe,
  append, finalize — and a foreign upload uid submitted through memo create/edit — all
  behave exactly like an unknown uid. Pre-migration rows fail closed and age out.
- Cookie-authenticated mutations refuse cross-origin requests: Origin (or Referer) must
  match the app's own origin, closing the cross-subdomain CSRF vector that SameSite's
  site-scoping leaves open. PAT and cookie-less clients are unaffected.
- The login rate limiter keys on the real client IP behind the reverse proxy
  (`--proxy-headers` with an explicit private-range trust list).
- The production image clears the Trivy HIGH gate again: aiohttp/cryptography/pillow at
  fixed releases, no uv cache in the final layer, no unused interpreter pip stack.
- `/metrics` is no longer publicly reachable on the production hostname (kept scrapeable
  container-direct for internal consumers).

**Fixed**
- PAT `last_used` persists on the compat surface (it was flushed but rolled back on every
  request) — stamped at most once per window in a dedicated session that neither holds
  the row lock for the request's lifetime nor can fail authentication.
- The destructive test bootstrap refuses to run against anything but an allowlisted local
  throwaway database, including libpq query-string and environment rerouting.
- Session absolute expiry and OIDC state mismatch are pinned by mutation-killing tests.

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
