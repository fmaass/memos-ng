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
