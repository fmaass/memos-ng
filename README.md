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

### v2.13.0

- Quarantined guest uploads can now be marked as seen. `GET /api/trip-ingest/quarantine` lists the
  batches nobody has resolved yet, plus a count of quarantines naming a pass this server never
  issued, which have no batch row. `POST /api/trip-ingest/quarantine/{batch_uid}/resolve` marks a
  batch, and `POST /api/trip-ingest/quarantine/unknown/resolve` with `{"through": <total>}` clears
  that count up to the total the reader saw, so any that arrived since stay counted. The pass
  manager shows both, each with a Mark seen button. Resolving files nothing, deletes nothing and
  sends nothing to the door: the batch stays `quarantined`, a quarantine delivery still owed goes
  out, and the guest's bytes stay on the door. `guest_batches_quarantined` now counts unresolved
  batches plus the open count, so an alert on it above zero clears once everything has been seen.
- The pass manager shows a pass the door has suspended, with an Unsuspend button on the existing
  `POST /api/passes/{uid}/unsuspend`. After each sync, home reads the door's new
  `GET /ingest/passes/suspended`, a complete list under home's bearer token, and pass answers carry
  the result as `doorSuspended` and `doorSuspendedAt`. It is display only and changes no pass state,
  because the door is not trusted to decide one. A failure there does not stop ingestion. An older
  door's 404, a malformed answer or more than 500 suspended passes leave the last view in place,
  and a view older than 15 minutes reads as `doorSuspended: null`, unknown, and shows nothing.
- Trip filing keeps a crash journal. Each rename's source and destination is committed to the new
  `ingest_moves` table before the rename happens, and the filing's commit deletes those rows, so a
  row that survives means that filing never committed. Before this, a kill between placing a file
  and the commit lost the list of moves and left the bytes orphaned. Each sync now walks surviving
  rows back, newest first, before it files anything, and it overwrites nothing: when both ends of a
  move exist and the destination is not that move's own placeholder, the row is flagged, an error
  is logged and both files stay. A destination that a committed attachment or print rendition names
  is never moved: the row is flagged, and that sync ends before any filing, as it does for a file
  that will not move back. A filing that fails in process and restores every file deletes its own rows, so a name it
  gave up is not read as its own later.
- Deleting a memo takes the trip ledger rows naming it before it takes the memo's own lock, the
  order a filing uses. A delete beside a running filing could deadlock, and Postgres aborted one of
  the two with `40P01`.
- Smaller hardening on the guest path. Refusals are ordered against a lift by event id under the
  pass lock, so one stamped after a lift but committed before it stays spent. A batch records the
  pass version that admitted it. The write recording a cancelled sync ends inside a
  2 s deadline. A part the upload sweep cannot remove now leaves a log line and the rest still go.
- One additive migration, `a13d0e5c7b21`, runs at boot on both databases. It adds the `ingest_moves`
  table, `ingest_batches.pass_version`, `resolved_at` and `resolved_by`,
  `trip_ingest_cursor.quarantined_unknown_total` and `quarantined_unknown_cleared`, and
  `pass_sync_state.door_suspended_at` and `door_suspended_seen_at`. `ck_pass_events_kind` also
  accepts `unsuspend_requested` and `unsuspended`, which nothing writes yet: lifts keep the form
  2.12.0 reads, and readers here accept either.
- Rolling back to 2.12.0 needs `ROLLBACK_MODE=1` on both services and keeps the schema
  (`RELEASING.md`, "Rollback across a migration"). A resolved batch reads as quarantined again, so
  `guest_batches_quarantined` counts it and the alert fires again until the roll-forward; its
  `resolved_at` stays on the row and nothing is lost. The count of quarantines for an unissued pass
  drops out of that gauge meanwhile, because 2.12.0 reads only batch rows. 2.12.0 ignores
  `ingest_moves`, so a surviving journal row waits for the next forward deploy, whose first sync
  walks it back. The exception is a row whose destination a committed attachment or print rendition
  now names, which happens when 2.12.0 refiled the part under the same name: that file stays where
  it is, the row is flagged and kept, and each such row ends one sync with an error before filing. Later syncs
  walk the batch's other rows and file whatever the batch still owes.

### v2.12.0

- File caps on the guest door. `POST /api/guest/uploads` answers 429 with `Retry-After: 3600`
  once a pass has opened 2 000 upload sessions in the last 24 hours (`INGEST_PASS_FILES_PER_DAY`)
  or a device has sent 1 000 files in all, its unfinished sessions included
  (`GUEST_FILES_PER_DEVICE`), so one leaked device token cannot spend a whole pass. That cap is per
  enrollment, not per physical phone: whoever holds the QR secret gets another 1 000-file allowance
  per redemption while the pass has enrollment slots left, and the 2 000-a-day pass cap still holds
  over all of them. The pass count
  is taken under the same pass lock as its bytes, so two creates cannot take the same last slot,
  and a refused file has sent no byte. Its `last_modified` is now bounded to the years 1 to 9999,
  where a value past either edge used to be a 500.
- `POST /api/passes/{uid}/unsuspend` lifts the guest door's automatic suspension of a pass (too
  many refused redemptions) and pushes it to the ingest host at once. The lift raises the pass's
  version: the door clears a suspension it held before that version, keeps one it sets later, and
  counts refusals afresh from the lift, so the next one does not suspend the pass straight back.
  It answers the same whether or not a suspension stood, and 410 for a revoked pass.
- A pass issued without an expiry now lasts until the later of the trip's end and now, plus 14
  days, so a pass for a trip that ended long ago is no longer expired the moment it is issued.
  In the pass manager, a pass someone else revoked meanwhile also drops its QR code from the
  sheet.
- The guest page in a browser that will not store the device credential durably (a private
  window, for one) keeps it for that tab, under a digest of the pass link, and leaves the link in
  the address bar: reloading it resumes the same device instead of redeeming another one against
  the pass's device limit. The link itself is never stored. A set whose confirmation retries ran
  out is shown as failed, with a Retry, instead of staying "Sent, confirming".
- A guest batch filed in several parts now lands in one memo with its caption once: every later
  part is appended to the memo the first part made, and starts a memo of its own only if the
  owner deleted that one. An album's members are written before the memo is tied to its pass,
  which removes a lock cycle with a collection being deleted at the same moment.
- The trip ingest puller judges what the ingest host sends before the database does. A device
  figure, timestamp or text the home columns cannot hold (a count past its integer column, a year
  outside 1 to 9999, a NUL) is refused or quarantined on its own instead of aborting the whole
  page on every tick, and a page whose next cursor is out of range is refused as malformed. A
  file whose length or hash disagrees with its manifest gets one fresh download and is then
  quarantined, so the batches behind it keep moving. A filing interrupted inside its commit reads
  back whether it committed before it moves any file back, and a tick cut off by the worker's
  time bound records `cancelled: the tick ran past its bound` as its last error.
- An upload append (`PATCH /api/uploads/{upload_uid}`, and the guest door's
  `PATCH /api/guest/uploads/{upload_uid}`) cancelled mid-write now waits for its write to finish
  before it lets go of the upload, and records no new offset, so a retry at the same offset
  overwrites those bytes rather than writing beside them.
- Creating an upload, on either side, makes its part file last, so a create that fails before its
  commit no longer leaves a part file behind. The guest door's purge also removes files under
  `parts/` that belong to no live upload session: an expired session's at once, any other once it
  is older than `UPLOAD_EXPIRY_HOURS`, counted as `strays` in its `ingest.purge` log line.
- The guest door's `/.well-known/apple-app-site-association` now claims only a single-segment
  pass path `/p/<secret>` for the app. `/p/`, any path with a second segment (a trailing slash
  included) and `/p/joined` are excluded and stay in the browser.
- An upload body that goes silent is ended by the server. Each wait for the next chunk of
  `PATCH /api/uploads/{upload_uid}` is bounded by `UPLOAD_BODY_IDLE_SECONDS` (30 s); a stalled
  body is treated exactly as a disconnect, its bytes so far kept and its upload slot released,
  instead of holding that slot until the proxy's read timeout and answering every retry with 429
  meanwhile. The bound is per wait, so a slow body that keeps moving is never cut, and the answer
  is the usual 200 with the offset the next PATCH starts at.
- `/metrics` counts terminal media job failures by why they failed:
  `memosng_media_jobs_failed_total{lane,reason}`, with `lane` one of `transcode` and `light` and
  `reason` one of `decode_error`, `source_missing`, `storage_error`, `ffmpeg_failed`, `timeout`,
  `out_of_memory`, `sibling_unavailable`, `stale_claim` and `other`, every pair published.
  `memosng_upload_sessions_total` now carries `result="expired"` too, for the sessions the worker
  reaps. The worker keeps both as durable tallies in the database that the app's scrape reads,
  bumped in the transaction that does the work, and a tally that cannot be written is logged and
  never costs the job or the session.
- Migration `85d081a451a1` (expand-only) adds two new tables for those tallies,
  `media_job_failure_tallies` and `upload_session_tallies`. No existing table or row is touched.

### v2.11.0

- Upload-only capture grants, the server half of sharing into memos from the iOS share sheet.
  `POST /api/tokens/capture-grant`, presented with a device's own personal access token, mints a
  `memos_cap_…` credential that acts as that token's owner on exactly three operations: opening an
  upload (`POST /api/uploads`), appending bytes (`PATCH /api/uploads/{upload_uid}`) and reading the
  offset (`HEAD /api/uploads/{upload_uid}`). Every other route answers it 401, and a grant that does
  not authenticate is a 401 with no fallback to the session cookie. A session cookie cannot mint one.
- A grant lives for 24 hours (`CAPTURE_GRANT_TTL_S`) or until its token expires, whichever comes
  first, and is checked against that token on every use. Each token holds at most one grant: a new
  mint retires the previous one in the same transaction, and concurrent mints leave exactly one
  live. Revoking the token (Manage Tokens, native logout or renewal) revokes its grant with it, and
  `DELETE /api/tokens/capture-grant/{grant_id}` revokes it alone, answering 404 for anything the
  presenting token did not mint. Grants are listed in Manage Tokens as "Capture grant, phone".
- A grant is stored under its own domain-separated hash and refused by the token lookup whatever
  its hash, so it can never act as a personal access token, an older build after a rollback
  included. Bytes appended with a grant are counted in `memosng_upload_bytes` under
  `principal="capture_grant"`.
- Migration `f9af5db6b98c` (expand-only) adds the nullable `pats.parent_pat_id` with a unique
  index and a self-referencing foreign key that cascades on delete; every existing token reads
  NULL. Its downgrade deletes the grant rows before dropping the column.

### v2.10.0

- `GET /api/events` is now a live stream instead of a poll that closed once nothing was in
  flight. An attachment's status change (an upload finalised, a media job's progress, its finish
  or failure) is announced with a Postgres `NOTIFY` in the same transaction that writes it, and
  each process fans it out to its subscribers from one listener connection outside the pool. Every
  frame carries an `id:`; a client that reconnects with `Last-Event-ID` is replayed what it missed
  from the last five minutes (and from the same server process), and one the server cannot replay
  from gets the snapshot of its non-ready attachments first, as a fresh connect does. The stream
  sends `retry: 3000` and a keep-alive comment every 25 seconds, and no longer closes when nothing
  is in flight; a subscriber that falls 512 frames behind is closed rather than waited for.
- A memo's own `idempotency_key` is returned to its owner on every memo answer (null for every
  other reader), so a client whose create got no answer can find the memo it already made on the
  timeline instead of posting it twice.
- Issuing a pass on a trip without a tag no longer answers 409: the trip is given a tag in the
  same transaction, its title lower-cased with everything but letters and digits removed
  ("Cyprus 2026" becomes `cyprus2026`, or `trip` plus the first eight letters and digits of its
  uid when that leaves nothing), and memos already carrying that tag join the trip. An existing
  tag is never replaced; `trip_has_no_tag` now means only a legacy tag a memo cannot carry. A
  collection deleted while its pass is being issued answers 404.
- The guest door's API is published as `openapi-guest.json`, generated by `scripts/gen-openapi.py`
  next to `openapi.json` from the very app the `ingest` role serves, with its security derived
  from each route's auth dependency the same way. The door itself still serves no schema.
- The trip-pass guest-sync and upload-queue collectors on `/metrics` describe their families
  without reading the database, so registering them no longer runs a collection at import time.

### v2.9.0

- `GET /api/uploads/limits` tells a client the upload limits before it starts: `max_bytes` (the
  `MAX_UPLOAD_MIB` cap, the same threshold the 413 enforces), `wifi_recommended_above_bytes` (new
  setting `UPLOAD_WIFI_RECOMMENDED_MIB`, default 200; advisory, the server enforces nothing at it)
  and `bodies_per_user`. Settings are read per request.
- Upload and media metrics on the internal `/metrics`: `memosng_upload_bytes_total{principal}`,
  `memosng_upload_sessions_total{result}` (`completed`, `deduped`, `rejected_size`),
  `memosng_upload_patch_seconds`, `memosng_upload_admission_rejections_total{scope}` and
  `memosng_events_subscribers` are counted in process; `memosng_upload_sessions_open`,
  `memosng_media_jobs_queued{lane}` and `memosng_media_job_seconds{lane,kind}` are read from the
  tables on each scrape.
- Trip pass guest-sync gauges on the same `/metrics`: `guest_sync_snapshot_age_seconds`,
  `guest_sync_required`, `guest_pull_lag_seconds`, `guest_blobs_pending` and
  `guest_batches_quarantined`, read from the database with one bounded statement per scrape. A
  failed or slow read publishes no guest family at all, so an absence alert fires instead of a
  healthy-looking zero.
- Trip pass guest page: a separate guest build (`frontend/vite.guest.config.ts`) that the ingest
  role serves at `/p/<secret>` and `/guest-assets/<name>`. It hashes and uploads a guest's
  originals through the ingest door. New setting `INGEST_GUEST_APP_URL` (an https App Store or
  TestFlight link, offered on the landing; empty hides it). The home app answers 404 on both
  prefixes, so a pass link never opens the member app.

### v2.8.0

- Trip passes: a guest pass lets people without an account add pictures to a collection.
  `POST /api/collections/{uid}/passes` issues one (201) with a `label`, a `scope` (`contribute`,
  the default, or `contribute_view`, which no route serves yet), `expiresAt` (default: the trip's
  span end plus 14 days, else 30 days from now), `maxDevices` (1–200, default 30) and `notes`. The
  answer carries the secret, the guest URL and that URL as an SVG QR code, once; only the secret's
  sha256 is stored. A trip without a tag answers 409 `trip_has_no_tag`; a `TRIP_PASS_BASE_URL`
  that is not an absolute https origin answers 503 and issues nothing.
- Every signed-in member manages every pass, as with collections. `GET
  /api/collections/{uid}/passes` lists a collection's passes, revoked ones included; `GET`, `PATCH`
  and `DELETE /api/passes/{uid}` read, edit and revoke one. A change raises the pass's `version`.
  A revocation is terminal and revokes every device on the pass; reading, editing or revoking it
  again answers 410 `pass_revoked`. Deleting a collection revokes its passes first; their
  contributions stay.
- `GET /api/passes/{uid}/devices` lists the guest devices on a pass; `DELETE
  /api/passes/{uid}/devices/{device_uid}` revokes one (410 `device_revoked` if it already is).
  `GET /api/passes/{uid}/contributions` lists every picture that came in through a pass with its
  contributor name, device, caption and whether it is hidden, also after a revoke.
  `POST /api/passes/{uid}/contributions/hide`, `/unhide` and `/remove` take up to 500
  `attachmentUids` and answer per uid `hidden`, `unhidden`, `removed`, `notContribution` or
  `error`. Hiding excludes a picture from a trip or takes it out of an album; `remove` deletes the
  original and every derived file.
- Web: Settings > General has a "Guest passes" section listing the trips. Each opens a sheet that
  issues a pass (label, scope, expiry, device limit), shows its QR code and link once, lists the
  passes with their devices, and revokes a pass or a single device.
- The guest side is a separate installation from the same image, picked by `APP_ROLE`: `home`
  (the default, the household app), `ingest` or `ingest-home`. Both ingest roles serve
  `app.ingest.asgi:app`, which loads no member route, OpenAPI document or SPA; `app.main` refuses
  an ingest role, `app.ingest.asgi` refuses `home`, and the media worker refuses an ingest role.
- `ingest` is the guest door: `GET /p/{secret}` (a static page), the
  `/.well-known/apple-app-site-association` file for `/p/*` (404 without `APPLE_TEAM_ID`),
  `POST /api/pass/redeem` (enrolls a device and returns its `trip_dev_…` token; 404 for an unknown
  secret, 410 for a pass no longer open, 429 past the rate, address or device limit),
  `GET /api/guest/me`, resumable uploads through `POST /api/guest/uploads` and `HEAD` / `PATCH
  /api/guest/uploads/{upload_uid}` with `Upload-Offset`, `POST /api/guest/memos` to complete a
  batch (a replayed completion answers the same receipt) and `GET /api/guest/batches`. A new
  upload answers 507 past the pass's byte cap or the `INGEST_MIN_FREE_BYTES` floor. A pass with
  more than `INGEST_REFUSALS_PER_HOUR` refused redemptions within an hour is suspended there.
  A suspension that arises between the owner's unsuspend and the door applying it is cleared by
  that lift, on purpose: the owner asked for this pass to open, the door counts refusals afresh
  from the lift, so a real attack suspends it again within the same limit, and the owner sees it.
- `ingest-home` is the interface home pulls from, under `/ingest/`, for one bearer token,
  `INGEST_SERVICE_TOKEN` (at least 86 characters, `secrets.token_urlsafe(64)`); without one it
  refuses every request. It takes pass state and device revocations, pages devices and batches,
  serves files by sha256 with Range, and takes acks and quarantines. Every
  `INGEST_PURGE_INTERVAL_S` it deletes batches acknowledged more than `INGEST_RETENTION_DAYS` (7)
  ago and expired upload sessions; unacknowledged and quarantined batches are kept.
- Home's media worker runs a puller every 60 s once `TRIP_INGEST_URL` and
  `TRIP_INGEST_TOKEN_FILE` are set, and is off otherwise. It pushes pass changes and revocations,
  pulls devices and batches, and downloads each file, resuming by Range. A file is filed only once
  it hashes to its manifest sha256 and is the type its name claims; a batch that fails this is
  quarantined, and a transport failure is retried on the next tick. A filed batch becomes a
  protected memo owned by the pass's issuer (several memos for a large one), carrying the trip's
  tag or added to the album. A guest's name and caption are kept in their own fields and never
  become memo text or tags.
- Operators: the ingest roles serve without proxy headers or an access log, and the entrypoint
  reads `INGEST_SERVICE_TOKEN_FILE` and `DATABASE_PASSWORD_FILE` (with `DATABASE_HOST`,
  `DATABASE_NAME`, `DATABASE_USER`) when set. `ingest` migrates by default and `ingest-home` does
  not; `RUN_MIGRATIONS` overrides either. An ingest role refuses to start with a member-side
  secret set (`SESSION_SECRET`, `OIDC_CLIENT_SECRET` and the like); home refuses
  `INGEST_SERVICE_TOKEN` and `INGEST_TRUST_CF_CONNECTING_IP`. New dependency: `segno`, a
  pure-Python QR encoder.
- Migrations `79cdd613ea6b` (passes, devices, events, guest uploads, ingest batches and files,
  sync state; nullable contributor columns on `attachments` and pass columns on `memos`),
  `a7d2c41e9b03` (pass suspension, change cursors, completion key, refusal counter) and
  `c3e8b5a1d2f4` (puller cursor, revocation ack, filed-at stamp) — all additive only.

### v2.6.0

- Albums: a collection can now be an album — a title and pictures picked one by one, with no tag
  and no span. `POST /api/collections` takes `kind` (`trip`, the default, or `album`); an album with
  a tag or a span answers 422. Every collection carries `kind`, and `GET /api/collections?kind=`
  narrows the list; the list shows the trips as before, then the albums, the most recently changed
  first. `kind` cannot be changed: sending it in a `PATCH` answers 422, as does a tag or a span on
  an album; a title or cover change succeeds.
- `POST /api/collections/{uid}/attachments` adds pictures to an album one by one — only the named
  picture, never its memo's other pictures, and without editing any memo. Anyone signed in may add
  any picture they can see. Each uid answers `added`, `alreadyMember`, `notVisible` (it does not
  exist or the caller cannot see it), `orphan` (no memo, or detached) or `error` (that one write
  failed; the others stand). Trips answer exactly as before.
- `DELETE /api/collections/{uid}/members/{attachment_uid}` takes one picture out of an album (204,
  idempotent). On a trip it answers 409 `tripHasNoMemberRows`; the exclusion routes answer 409
  `albumHasNoExclusions` on an album. Adding or removing a picture moves the album's `updatedAt`.
- Migration `294410e70c5f` adds `collections.kind` (default `trip`, so existing collections become
  trips without a rewrite), rewrites the source check per kind, and adds the `collection_members`
  table (additive only).

### v2.5.0

- Tag, month and year collections can carry a chosen cover picture. `GET /api/collection-covers`
  lists every chosen cover as `{kind, key, attachmentUid}`, limited to pictures the reader can see.
  `PUT /api/collection-covers/{kind}/{key}` with `{"attachmentUid"}` sets one: any signed-in member
  may choose any picture they can see, and covers are household-wide. `DELETE` on the same path
  clears it (204). A bad kind or key answers 422; an unknown or unviewable picture answers 404.
- Migration `a584468dae25` adds the `collection_covers` table (additive only).

### v2.4.1

- Favorites are family-wide. Anyone signed in who can see a picture may now set and clear its
  `favorites` heart through `PUT` and `DELETE /api/attachments/{uid}/tags/favorites`. Every other
  picture tag name stays owner-only, and a picture the caller cannot see still answers 404.
- No migration.

### v2.4.0

- A picture can now carry its own tags, `favorites` for example, separate from the tags of the memo
  it is on. `PUT` and `DELETE /api/attachments/{uid}/tags/{name}` set and clear one on the
  caller's own picture, both idempotent; `GET /api/attachments/picture-tags` lists every picture
  tag with the number of visible media carrying it, and `?pictureTag=` filters the attachment
  list to media that itself carries that tag. A memo's tags never match a picture tag.
- The attachment detail now returns the file name, the location with its source, and who added
  the picture.
- Uploads now read the camera EXIF of a picture: make, model, lens, aperture, exposure time, focal
  length, exposure bias and ISO, returned as `camera` on the attachment detail. A one-off,
  resumable `backfill_camera` script reads the same fields from pictures uploaded before; it is
  a dry run unless given `--execute`.
- Two migrations: picture tags, and the camera columns.

### v2.3.3

- The sibling lookup no longer takes a file name's counter for an identity. Phone and camera
  counters repeat across bodies and years, so a RAW whose stem matched could be another frame
  entirely. A candidate is now refused when it was shot more than a day after the export it would
  date already existed, or more than 90 days before the memo the photo was posted on; refused
  candidates neither win nor make the election ambiguous, and when none are left nothing is
  borrowed. A memo dated back to an old shoot moves that window with it.
- PNG and JPEG files whose EXIF carries no date are now dated from the `DateTimeOriginal` in
  their XMP packet, as Lightroom's PNG exports write it, with `capture_source` `xmp`. EXIF still
  wins when it has a date, and such a file is never handed a borrowed date.
- `backfill_sibling_metadata --audit` is a read-only report: it re-judges every date already
  borrowed under the new rule, lists the rows it would have refused, and lists the borrowed rows
  whose own file now dates them. It writes and enqueues nothing and refuses `--execute`.

### v2.3.2

- Reading memo data now always takes a credential: an anonymous or bad-token request to the compat
  memo list, the single compat memo or the compat file route answers 401 instead of the public
  slice. Nothing changes for a signed-in browser or a caller holding a valid token, and a signed
  media URL still serves without a credential, as before.

### v2.3.1

- The sibling lookup now believes a consensus: when every RAW img-manager returns for a stem has
  the same capture second and the identical GPS pair (or none at all), they are one RAW held in
  both of its tiers, so I borrow the date and the place and record the outcome as `consensus`.
  Candidates that disagree in time or place stay `ambiguous` unless exactly one of them is a
  pick, which wins as before; `--include-ambiguous` re-runs the ambiguous rows.

### v2.3.0

- A photo whose own file says nothing about when or where it was taken — a Lightroom PNG export
  carries no EXIF — can now borrow both from the RAW original img-manager indexes. A new `sibling`
  media job, enqueued at upload after the derive job for an image of an enabled owner that lacks a
  capture date or a location, asks img-manager's stem lookup once (`POST /api/lookup/stems`, the
  export suffix and Lightroom's `-Edit` / `-<n>` rename stripped) and fills only what the file left
  empty: a date the file provided, even one without an offset, and a location the file provided
  are never overwritten. A borrowed value is recorded as `capture_source` / `location_source`
  `'sibling'`. Only an unambiguous sibling is believed — exactly one pick, or with no pick exactly
  one image; a stem shared by several RAWs stays undated. The lookup never runs on a request path,
  never touches the original, and an img-manager that cannot be reached is retried rather than
  recorded as an absence.
- `attachments.sibling_outcome` (additive, nullable, no default) records the lookup's answer:
  `unique`, `ambiguous` or `none`; NULL means never asked.
- Three settings switch it on, all together or not at all (startup refuses a partial set):
  `IMGMANAGER_URL`, `IMGMANAGER_TOKEN` (a PAT, never logged) and `SIBLING_OWNER_IDS`, the
  comma-separated user ids whose photos may be enriched — the RAW library is one member's; a list
  with no id counts as unset. `IMGMANAGER_TIMEOUT_S` (default 5 s) is optional. Unset, nothing is
  enqueued and nothing changes.
- `python -m app.scripts.backfill_sibling_metadata` enqueues the same job for existing photos: dry
  run by default, `--execute`, `--limit N`, and the disjoint `--retry-failed` and
  `--include-ambiguous` populations. It never asks img-manager itself and writes no attachment
  column; the worker drains the queue.

### v2.2.0

- `GET /api/attachments`, `/api/attachments/buckets`, `/api/attachments/tags` and a collection's
  member read `GET /api/collections/{uid}/attachments` take two optional filters: `mine=true` keeps
  only the caller's own attachments, orphans included, and `visibility=PUBLIC|PROTECTED|PRIVATE`
  keeps only media on a memo of that visibility, so no orphan. Both only narrow what the caller may
  already see — `visibility=PRIVATE` is the caller's own private memos — and the four surfaces
  answer every combination with the same rows. Without them every response is unchanged.
- Every library item (`GET /api/attachments`, the member read, a collection's `cover`) now carries
  `visibility`, its memo's visibility, or null for an orphan. Additive; a client that ignores it
  is unaffected.
- `POST /api/collections/{uid}/attachments` with `{"attachmentUids": [...]}` makes pictures members
  of a tagged collection: the collection's tag is appended once to each of the caller's memos the
  pictures hang on (its other tags kept), and each named picture's exclusion is cleared. Tagging
  a memo admits its other pictures too; their own exclusions stay. One answer per uid — `added`,
  `alreadyMember`, `notOwner` (another member's memo, or a uid that is missing or not visible),
  `orphan`, or `error` when that memo's edit failed and was rolled back. A collection without a
  tag answers 409.
- Two edits creating the same new tag at once no longer fail one of them on the unique name.
- The published schema of a nullable enum now lists `null` among its values, so a validator
  accepts the null the field allows.

### v2.1.0

- `total_size` on a memo is now populated — the sum of its attached files' sizes in bytes — on the
  create, get, list, edit (`PATCH`), pin, unpin and reaction responses, where it was always 0
  before; same field and type, so the iOS client needs no change and may now read it.
- Every attachment payload that carries `webUrl` (memo attachments, `GET /api/attachments` and
  `/api/attachments/{uid}`, map markers) now also carries `webReady`, true only when the web
  rendition behind `webUrl` is on disk — always false for a picture and for a video still
  transcoding. `webUrl` itself is unchanged; a 2.x iOS client may play `webUrl` only when
  `webReady` is true instead of discovering the 404, and a client that ignores it is unaffected.
- Collections, a household record kept on the server: a title plus a tag, a date range
  (`spanStart` and `spanEnd`, both ends inclusive), or both, with an optional IANA `tz` and an
  optional cover. Its members are the pictures and videos on memos that carry the tag or whose
  `takenAt` lies inside the range, less the ones taken out of it; media on no memo never belongs.
  Members are filtered per viewer exactly as `GET /api/attachments` filters them, so a collection
  never shows anyone another member's private media.
- `GET` and `POST /api/collections`, and `GET`, `PATCH` and `DELETE /api/collections/{uid}`: any
  signed-in member may list, create, edit and delete any collection, and the last write wins.
  The list puts the newest `spanStart` first and collections without a range last. A create may
  carry an `idempotencyKey`; a retry with the same key answers 201 with the record already stored,
  as memo creation does. On `PATCH` an omitted field stays as it is and an explicit null clears
  the tag, the range, `tz` or the cover; the title can be changed but not cleared, so a null title
  is a 422. A record left with neither a tag nor a range, with one end of the range only, or with
  the range reversed is a 422 and nothing is stored. Each record carries `memberCount`, counted for the
  caller, and `cover`, which is null when the caller may not see that picture.
- `PUT` and `DELETE /api/collections/{uid}/exclusions/{attachment_uid}` take one picture out of a
  collection and put it back; both answer 204 however often they are sent. An unknown collection,
  and an attachment that is unknown or not visible to the caller, are a 404.
- `GET /api/collections/{uid}/attachments` lists a collection's members in the item shape and with
  the `X-Next-Cursor` paging of `GET /api/attachments`, taking `type`, `limit`, `cursor` and
  `order` (default `taken_desc`).
- `GET /api/attachments` takes `bucket=<key>` beside `order=taken_desc` or `taken_asc` and opens
  the walk at that day, month or year — the `key` exactly as `GET /api/attachments/buckets` names
  it for the same `tz` (default Europe/Zurich) — so the first item is that bucket's newest, or its
  oldest; an empty bucket opens at the next item beyond it. A key the buckets could not name, an
  unknown `tz`, or `bucket` under an upload order is a 422.
- Every item of `GET /api/attachments` now carries `tags`, the tags of its memo by name (empty for
  media on no memo or on an untagged memo), and every entry of `GET /api/attachments/buckets`
  carries `tags`, the tags of the memos its items hang on, the tag on the most of them first, then
  by name, every one of them.

### v2.0.0

- memos-ng and the iOS client keep independent semantic versions and share only the MAJOR, which
  is the API contract's major. Within a major the contract only grows: a minor may add paths,
  operations, optional parameters and response properties, while removing or retyping anything,
  or making a request demand something it did not before, ships only with a new major. A 2.x
  client works with any 2.x server. `GET /health` reports that major as the integer `apiMajor`,
  without a credential and in its `ok` and `degraded` answers alike; from this release it is 2.
- The major moves to 2 without breaking a 1.11.0 client: nothing 1.11.0 served is removed,
  retyped or made stricter, and every change since 1.11.0 is an addition: `apiMajor` above and
  the library's routes, parameters and properties below.
  `tests/test_ios_client_contract.py` holds the served schema to the `openapi.json` the iOS client
  was generated from, which is byte for byte the 1.11.0 document: every path, operation,
  parameter, request body, response and property it names must still be served with the same
  type, and no operation it names may gain a required input.
- The library can be read by the date a picture was taken (#211). Every item of
  `GET /api/attachments` carries `takenAt` and `filename`; `order=taken_desc` or `taken_asc` walks
  the library by taken date, and `tag=` keeps only media whose memo carries exactly that tag (an
  unknown tag is an empty page). `GET /api/attachments/buckets?size=day|month|year&tz=` counts the
  visible library per taken day, month or year, newest first, each bucket naming its newest item
  as the cover; `GET /api/attachments/tags` counts the visible media per tag. The default order,
  the bare-array body and the id cursors are unchanged.
- The picture library at `/resources` reads like a Photos app. It pages through the whole library
  instead of stopping at the first 200 tiles (#210), in taken order under sticky month headers,
  with a Library header whose menu sets sort, kind and one tag, and an app-wide dock (#212). Years
  shows a card per year, Months a mosaic of each month's busiest days, and a day opens All at that
  day's first picture, under the sticky header (#213, #214). Select mode in All shares or deletes
  several of my own pictures at once (#216). Four gallery tile defects are fixed with it (#210): a
  video's play overlay opens the lightbox, a tile whose thumbnail failed heals when a fresh URL
  arrives, each blurhash placeholder is cached per picture, and Enter and Space open a tile.
- No migration. The one migration since the last tag, `a4e7c1b20f36`, shipped with 1.8.0.

### v1.11.0

- The tus 1.0 endpoint at `/api/tus`, added in 1.10.0, is removed. No client used it, and
  resumable uploads are no longer pursued: a failed upload is retried whole. `/api/uploads` is
  unchanged and remains the one upload contract.
- `openapi.json` and the generated frontend schema no longer carry the four `tus_*` operations
  (73 operations → 69). No migration.

### v1.10.0

- The server speaks tus 1.0 (core, creation and expiration) at `/api/tus`, over the same upload
  store as `/api/uploads`, so a stock tus client can upload without learning this server's own
  dialect. `OPTIONS /api/tus` needs no credential and announces `Tus-Resumable` and `Tus-Version`
  1.0.0, `Tus-Extension: creation,expiration` and `Tus-Max-Size`. `POST /api/tus` creates a session
  from `Upload-Length` and an `Upload-Metadata` carrying `filename`, `filetype` and an optional
  `lastModified` in epoch milliseconds, and answers 201 with a path-absolute `Location` and
  `Upload-Expires`. `HEAD` reports `Upload-Offset`. `PATCH` with
  `application/offset+octet-stream` appends at the offset the server holds and answers 409 for any
  other offset, leaving the upload untouched. Every request but `OPTIONS` without
  `Tus-Resumable: 1.0.0` is refused with 412. Completion stays
  `POST /api/uploads/{upload_uid}/finalize` with the uid the `Location` named.
- The JSON upload routes are unchanged. A `last_modified` sent to `POST /api/uploads` (integer
  epoch milliseconds) is now applied as integer milliseconds instead of through a float division
  by 1000, so the stored instant is the millisecond that was sent; 0 still means no date.

### v1.9.0

- A `PATCH /api/uploads/{upload_uid}` body may be any length from 1 byte to the bytes the upload
  still needs: `chunk_size` is a hint for clients that chunk, not a size a body has to match, and
  the upload contract now says so in its descriptions. When a client disconnects in the middle of
  a body, the bytes that reached the server are kept — `HEAD` reports the offset they reached, and
  the next `PATCH` continues from there instead of sending them again. A disconnect before any
  byte arrived changes nothing, and a cut body whose starting offset another append has already
  moved past is discarded whole, exactly as a complete body would be.

### v1.8.0

- One memo save can name its whole picture list, in order. `PATCH /api/memos/{uid}` takes an
  optional `attachment_uids`: the memo's COMPLETE list of pictures, the index of an entry becoming
  that picture's place. Omitted, the pictures are untouched and the request stays a true partial
  update, exactly as `upload_uids` is; sent as an empty list, the memo keeps no pictures at all.
  An attachment the memo has and the list does not name is removed — until now removal was a
  separate `DELETE` per attachment, and order was whatever the appends left behind. An entry is
  either an attachment uid or an `upload_uid` named in the same request's `upload_uids`, because a
  picture being uploaded here has no attachment uid the client could know: that uid is minted while
  the request runs. Two entries may resolve to one attachment — identical bytes dedup, and a print
  binds onto its screen — and the attachment takes the EARLIEST place its entries asked for, so the
  stored list can be shorter than the one sent. An edit that only re-declares the list finalizes
  nothing, yet still links, reorders and detaches, so it asks for the same single ascending
  attachment acquisition finalization takes.
- An entry that cannot be resolved fails the WHOLE request with 422, never a silent skip: the
  append path skips a uid it cannot link, which on a declared list would answer 200 to a list one
  picture short, having already detached every picture that list left out. Each rejected entry comes
  back as one `detail` item whose `loc` ends with the uid and whose `type` names the reason —
  `attachment_uid.missing`, `attachment_uid.foreign_owner`, `attachment_uid.other_memo` or
  `attachment_uid.unresolvable_upload_uid` — in the validation-error shape this router already
  answers 422 in. A uid that is no attachment of anyone's but is an upload of this owner's gets the
  last of those, which is the difference between a client adding one field and a client hunting a
  picture it thinks the server lost. The judgement is taken after the one attachment acquisition
  and before a byte is moved, whether or not the request finalizes an upload — so a refused
  request has nothing on disk to put back. Taken any later, the same deterministic 422 could fire
  after a sibling print bind had replaced a committed master in place: those bytes are
  overwritten, no tracked move names them, and the upload row the rollback restores promises a
  part file that replacement consumed.
- What a list omits is detached, not deleted. A file this request cannot delete must not undo an
  edit the user was told was saved, so each detached row is reclaimed after the commit, judged
  against the instant that detached it — and a crash in the gap leaves a row that still names its
  files. A memo delete hands that instant to its own after-commit reclaim too, so a row a
  concurrent upload has claimed in between is live media and is left alone. Deleting an attachment
  directly now runs through that same unit, the one the hourly sweep below uses: every reclaim in
  the process is ONE unit — a task, a session and a transaction of its own, awaited through a
  shield. The unlinking inside it can be undone by no cancellation — a request that goes away, a
  stopping process whose wait runs out, the process's own teardown — so a row's lock always
  outlives the deletion of its files, and a row is never adoptable while its files are being
  unlinked. What a cancellation can still take is the transaction that follows, the files by then
  gone: the row stays, naming them. Detached, the next sweep finishes it; still on its memo,
  deleting that picture again does, an absent file counting as deleted. A reclaim that did not
  complete is logged either way, and on a memo delete that line names the memo the row came off.
- An attachment records when it left its memo, and an hourly sweep reclaims the ones that never
  came back. `attachments.detached_at` is written at the sites that null `memo_id`; nothing on the
  table could say that before, `created_at` being the public create time — and creation age is the
  wrong age anyway, because a file uploaded last month and detached ten seconds ago is a month old
  by it. Nothing looked for those rows either: the commit-race reconcile wants a memo, and the
  leaked-orphan predicate the maintenance script runs wants an original under a dated memo folder,
  which an original imported from 0.29.1 never has, because it stays flat. The sweep rides the
  hourly housekeeping cadence and its grace period is that same interval, so a row becomes a
  candidate one cadence after it was detached. The cutoff is computed once per pass and carried into
  each reclaim, so selection and revalidation judge every row against the same instant: a row
  adopted since carries no instant, a row detached again since carries a newer one, and both are
  left to whoever touched them last. Discovery holds no lock, and a candidate someone else holds is
  waited for rather than skipped, because waiting is how the pass gets to judge the row under the
  lock. A row detached before this release carries no instant and is never a candidate.
- That sweep's bound is a bound. Its unlinks are blocking calls against the data volume and it runs
  on the supervisor's own event loop, so one stalled unlink stopped the loop itself: the timeout
  meant to end the pass could not fire, the heartbeat could not be stamped and the media consumers
  stopped draining. The unlinks go off the loop now and the remaining time is checked BETWEEN
  rows, so a spent pass takes no further row and the row in flight keeps its lock to the end. The
  count is logged on every pass that returns, zero and the caught-error path included, since a
  pass that found nothing and a worker that never reached its hourly block are the same silence.
- A stopping process waits for the reclaims still in flight instead of cutting them short: the
  worker after its consumers, the API after its requests, ten seconds at most, nothing cancelled,
  and a record of how many outlive the wait. Running out of that wait does not cut them short
  either — the teardown that follows cancels what is left, and each one holds that cancellation
  until its files are gone. Only killing the process outright ends a reclaim mid-unlink.
- An attachment whose files are no longer there is not handed to a memo. A row left behind by a
  removal that rolled back after its first unlink still matches a dedup candidate by every column it
  has, and linking it would hand the memo a picture that cannot be served while destroying the
  upload that still held the bytes. Both dedup paths and the direct link now stat every
  non-regenerable file the row names — the screen original, the legacy print master, every rendition
  master, and for a video that has an original its poster tier — under the lock they already hold,
  and off the request loop, since every stat is a network round-trip on the deployed volume. A dedup
  caller refused this way still holds the bytes and mints its own row; a caller that named the uid
  itself is told the picture is missing. An adopted row's detach instant is cleared with the link it
  gains.
- A sign-in callback that arrives twice is answered instead of failing. The second arrival presents
  an authorization code the provider has already spent, and the refusal used to raise out of the
  exchange and surface as a 500. It is a handled outcome now: the browser lane gets a plain page
  with a way to start again, the app lane gets the usual redirect with one constant error value and
  no code, and both drop the transaction cookie. The first arrival's result is not replayed, because
  that would make a session or a one-shot hand-back code collectable twice, and the error value is
  the server's own constant with nothing the caller sent reflected into it. The state check is
  unchanged and the new branch sits after it. One warning event records the provider's status and
  the lane, and nothing else, so an outage at the provider still leaves a trace.
- A user's personal access tokens come back newest first. An unordered select let the list arrive in
  whatever order the planner chose, so a token just minted could appear anywhere; the id keeps the
  order total when two mints share a `created_at`.
- The worker says which schema it cleared even when it never waited. It logged only after a
  wait, so the ordinary start — the schema already at head on the first poll — left no record of
  which revision the worker's code is running against, and no way to tell a healthy worker from one
  that never got that far. That line now carries the head revision and the version on every pass
  the worker clears the schema on.
- Migration `a4e7c1b20f36`: one nullable column, `attachments.detached_at`, expand-only, with no
  backfill and no data rewrite. NULL means nothing detached that row — a standalone upload that was
  never linked, a 0.29.1 import, a row the foreign key's `ON DELETE SET NULL` nulled without the
  application, and every row detached before the migration. An instant invented for those would be a
  lie about when their bytes stopped being wanted. The column being nullable is what makes a
  rollback of this release expand-preserving: the previous image's ORM does not mention it and runs
  against this schema unchanged.
- API contract: `openapi.json` gains one property. `#/components/schemas/MemoPatch` now carries
  `attachment_uids`, optional, `"type": ["array", "null"]` with string items, and its description
  states the rules above — the two different requests `null` and `[]` are, the removal of what the
  list does not name, the two kinds of entry, the collapse onto the earliest place, and the four
  422 reason types. Nothing else in the document changed. A client that vendors `openapi.json` must
  re-vendor it.

### v1.7.3

- The SPA's hashed assets are served compressed, long-cached and never ranged. `/assets`, where
  every file name is content-hashed, goes out gzipped to a client that accepts it and carries
  `Cache-Control: public, max-age=31536000, immutable` on the 200 and on the 304; the compressor
  wraps that mount alone, so the media tiers, the Range-served basemap archives and the event
  stream keep going out as they did. A `Range` request there used to be answered `206` with a
  slice cut from the identity file and then gzipped, under the same strong ETag as the full 200,
  so a browser resuming an interrupted map chunk spliced it into garbage; the tier now drops
  `Range` and `If-Range` and answers `Accept-Ranges: none`. The shell keeps its stable name and
  `no-cache`, since a copy of it kept across a deploy names chunks that no longer exist.
- A tap on Places is answered at once. The map loads beside the route behind a fallback pane
  instead of in front of it, so the view renders while the megabyte of map code arrives, and a
  navigation that keeps the viewer waiting past 120 ms raises a progress bar in the app shell —
  cached routes never flash it, and a tap on the route already open takes it down. The chunk is
  warmed in the background once the app is up and the browser is idle. The map's stylesheet is
  repairable again: Vite's preload helper skips a CSS dependency it has already seen, so a
  `map-*.css` that failed once stayed skipped for the life of the document and the map mounted
  unstyled with nothing on screen to say why. Failed links are now recorded and replaced with
  fresh elements before the map loads, concurrent loads share the one repair in flight, and a link
  leaves the failed set only once its replacement has loaded. The pane's last rung is a reload,
  the only retry certain to reach the network once the browser remembers a module fetch as failed.
- An AVIF original's Exif is read from the file's headers, so its location and capture instant
  cost a header walk instead of a full decode. AVIF is the same ISO-BMFF container as HEIC and the
  walk never touches a codec, so `image/avif` joins the mime set that routes to it. A test holds
  the cost to the headers: on an AVIF of more than 32 KiB the read stays under a sixteenth of the
  file, and the image payload the larger fixture adds costs under a kilobyte more of it than the
  small one. Thumbnailing and the derived tiers are
  untouched, and a file whose Exif item the headers cannot address takes the existing fallback.
- A route chunk the current build no longer ships gets one reload onto that build. After a deploy,
  a page opened before it asked for a hashed chunk that is gone and the tap did nothing at all;
  the router now reloads once, keyed by route path so the query and fragment variants of a route
  share one marker, and answers a second failure of the same route within ten minutes with a
  message offering the reload rather than taking it again. A browser that blocks storage gets that
  message and never a reload on a bet. The recovery is wired in the document that is running, so a
  page opened before this deploy still gets the dead tap; it holds from the next deploy on.
- From the next update on, an open tab is not reloaded mid-session: a build that takes control is
  picked up on the next navigation, so nothing half-typed is lost, and an open page asks for an
  update hourly while it is visible instead of never. On this update it is the 1.7.2 document that
  decides: it registers its worker in `autoUpdate` mode with no `onNeedReload`, and the worker
  calls `skipWaiting()` and `clientsClaim()` in both versions, so that build's own helper reloads
  such a tab once as 1.7.3 takes control.
- What a short connection blip broke is refetched even when the banner never showed. The banner's
  monitor drives the query client's online state, so a blip that never got past `suspect` — one
  failed request, or failures inside the 3-second debounce — changed nothing, and a query that
  does not retry, today the session query, stayed in its error state until something else
  refetched it. Every recovery now invalidates exactly the queries that are in an error state,
  once per recovery, and never a healthy one.
- The Resources viewer offers the jump to the memo. A photo opened from the library was reachable
  only by scrolling the feed for it; the viewer now navigates with the same helper Places uses,
  and still withholds the action on a slide that names no memo. The coordinate rounding that keys
  a place is pinned by vectors read from the runtime itself — the exact binary tie, the negative
  zero that must never reach a key, and eight near-ties — so the web and the native client cannot
  drift apart on it while both suites stay green.
- A worker stop delivered inside the claim's own commit no longer strands the job. The claim
  identity is read before the commit and the commit sits under the cancel handler that hands the
  row back, so such a stop requeues the row instead of leaving it `processing` under a claim
  nobody answers to until the stale-claim sweep — the one window 1.7.2 disclosed as uncovered. If
  the commit never landed, the conditional hand-back matches no row. An ordinary error raised in
  the best-effort push while a cancellation was unwinding used to replace it and swallow the stop;
  it is re-raised now, so the consumer stops instead of draining on.
- Every checkpoint a worker stop can skip is fenced in the print-ready path. A stop waits out the
  print encode, so the rendition had already been replaced into its final path by the time the
  checkpoint that sweeps what a job published for a photo deleted mid-run would have run — and
  nothing swept it later either, the delete having taken the job row with the attachment. That
  fence now runs on the stop path itself, under a session of its own and a one-second bound, and
  it covers the light lane's three publishing sites, not the print encode alone; the transcode's
  checkpoint unlinks its own temporary file instead. The heavy image decode moved off the event
  loop into a thread, in the shape the print encode already used, so the seconds of libheif decode
  a HEIC or AVIF photo costs no longer hold the heartbeat and the transcode consumer's progress
  writes for that whole time. A failing `unlink` in the stop's cleanup no longer replaces the
  cancellation it is cleaning up after.
- Documentation: the release procedure's stop budget names two kinds of job the stop waits out, a
  print-ready encode and a heavy image decode, instead of the encode alone — both run in a thread,
  which cannot be interrupted, and the wait sits in front of the bounded steps, so the 5 s quoted
  for the light path is the bounded part only and neither wait is new. The light consumer's bound
  now includes up to one second for the checkpoint fence, at most once per stopped job; the
  transcode path takes no bounded second of its own, its fence being a single synchronous unlink.
- The hourly housekeeping bounds are proven by behaviour. The declared-bounds test compared a set
  of step names, while `frame_share_recover` and `media_recover` are each wrapped twice — before
  the loop and hourly — so an hourly wrapper could be dropped with that set, and the test,
  unchanged. Both are now reached through the real loop, and an hourly recover that never returns
  is proven not to stop the heartbeat. A stop and a bound expiring in the same iteration is pinned
  too: the supervisor still ends with the stop. A mutation control replaces the claim `commit()`
  before the work with a `flush()` and must turn exactly three named tests red, so none of the
  three can be deleted with the gate silent.
- API contract: `openapi.json` changed shape. Under `#/components/schemas/MapManifest`, the
  `planet` and `detail` properties no longer carry an `anyOf` of `$ref:
  #/components/schemas/MapArchive` and `{"type": "null"}`. Each now states the archive schema
  inline — the `name`, `url` and `size` properties, all three required, under
  `"type": ["object", "null"]` — and `MapArchive` itself stays in `components/schemas`. The
  rewrite is for generated clients: swift-openapi-generator has no support for a bare `null` arm
  and drops the property that carries one rather than failing, so a client generated from the
  previous document had neither basemap archive. A client that vendors `openapi.json` must
  re-vendor it.
- No migration.

### v1.7.2

- `/places` no longer carries a timeline. The map keeps everything it showed: pins, clusters,
  the cluster drawer and the viewer all stay, and the whole located set is on the map at once.
  The playhead, its histogram, the play/speed controls and their keyboard shortcuts are gone, and
  the drawer and the grid read the same one set the map shows.
- A worker told to stop tries to hand its in-flight media work back to the queue. On the SIGTERM a
  `--force-recreate` sends, the supervisor tears both consumers down and each cancelled job's row
  is put back to `queued` with its attempt count untouched — provided that row is still
  `processing` under this worker's own claim, so a job that had already finished, failed or been
  deleted is left as it is. The next worker start re-runs the requeued job, and the per-attachment
  order guard then stops withholding that attachment's later jobs. The hand-back is an attempt,
  not a guarantee. Its bounds are designed, not guaranteed, and docker's stop grace is 10 seconds:
  a stop that outlasts the grace ends in SIGKILL. One window is known to be uncovered even inside
  the grace: a stop that lands while the claim itself is being committed can leave that job
  `processing` although the worker exits cleanly. In both cases the claim stays `processing`
  until the stale-claim sweep requeues it — `MEDIA_CLAIM_STALE_SECONDS` (7200 s) plus up to one
  sweep interval — and that recovery charges the job an attempt. Deploy the worker in a quiet
  window, which is what keeps both cases off the table.
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
  the coroutine that fences a photo deleted mid-job is gone. Known limit: on that stop path the
  fence itself does not run. A photo deleted during the very encode a stop is waiting out
  keeps that one print rendition on disk; no row points at it and no route serves it.
- The reclaim reads the attachment's current state under its own lock. It re-selected the row FOR
  UPDATE but then decided from the copy the caller's session had already loaded, so a photo
  re-attached to another memo mid-run was deleted anyway and the read-back reported success. Both
  implementations now read the row's current columns under the lock, which is what makes the
  re-read a revalidation rather than a formality.
- Alembic no longer disables loggers that already exist. It configures logging through
  `fileConfig`, which by default switches off every logger created before it runs. The migration
  at container boot was never affected, since it runs in its own process before the server
  starts; a process that loads `alembic/env.py` after the app's logging is set up, as the test
  suite does, lost those loggers. `alembic/env.py` now passes `disable_existing_loggers=False`.
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
