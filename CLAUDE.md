# memos-ng — Memos rebuilt on the household default web stack

## What this is / current state

A from-scratch replacement for the old `memos` (`neosmemo/memos:0.29.1`) plus its three support
containers (`memos-nginx`, `memos-imgproxy`, `memos-resizer`) and the Merkur `memos-transcoder`.
**All four collapse into memos-ng**: FastAPI serves the SPA + API + asset tiers (no nginx), and an
in-stack `memos-ng-worker` (Pillow/pillow-heif + ffmpeg, Postgres `media_jobs` queue) replaces imgproxy +
resizer + the 5-minute NFS transcoder.

**State: SHIPPED — memos-ng IS production** at `https://memos.discomarder.live`; cutover done, the
old `neosmemo/memos` stack retired. Work is ticket-scoped maintenance/features, not greenfield
phases. (`docs/plans/PLAN_2026-06-20_memos-ng.md` is the historical build roadmap;
`fixtures/live-0.29.1/` remains the compat ground truth — see below.)

### Deployed topology (the "jupiter" host = the local machine, merkur)

- **App + worker** run on the **local Docker** (jupiter), NOT on Saturn: containers
  `memos-ng-app` and `memos-ng-worker`, both the locally-built image **`memos-ng:local`**, user
  `1027:100`. Routed by Traefik router `memos-ng-cutover` → `Host(memos.discomarder.live)`.
- **Deploy config (stack)**: `/opt/jupiter-stacks/stacks/memos-ng/docker-compose.yml` — its own
  git repo, **pins-to-main** convention (compose bumps commit to its `main`, needs
  `ALLOW_MAIN_COMMIT=1`). Build context = **this** repo's `Dockerfile`.
- **DB**: Postgres17 on **Saturn**, `192.168.1.50:5455`, DB/role `memosng`. Reach with
  `ssh saturn.local 'sudo /usr/local/bin/docker exec postgres17 psql -U memosng -d memosng -c "…"'`.
- **Data volumes**: originals on Saturn NFS `/volume1/docker/memos-ng`; derived tiers on NVMe
  `/volume2/nvme2/cache/memos` → mounted `/app/data` and `/app/data/assets`.
- **This repo is NOT the deployed path** — the image is built FROM this repo and referenced by the
  `/opt/jupiter-stacks` stack. Deploy = LOCAL build (`RELEASING.md` local path), retag the running
  image `memos-ng:rollback-<sha>` FIRST, then `up -d --force-recreate`; verify running image Id ==
  built `memos-ng:local` Id and a live `/health` before calling it done. See `/safe-deploy`.

## CARDINAL RULE — this IS production; a bad deploy is a real outage

There is no separate staging; every deploy hits household prod data at `memos.discomarder.live`.
Treat every deploy and every prod DB migration as production-affecting:
- **Never** `docker compose restart` to pick up code (reuses the old image) — `up -d
  --force-recreate` (hook-enforced; `ALLOW_RESTART=1` only for genuine config reloads).
- **Never** overwrite `memos-ng:local` without first retagging the running image
  `memos-ng:rollback-<sha>` (the ≤5-min rollback reference).
- **Never** run a prod DB migration without a plan through the plan gate first; apply
  expand/contract, no data loss.
- Old-memos data lives only as read-only archives now; never move or mutate it in place.

### Shared-infra touchpoints (handle with care)
1. **Authelia restart** (any OIDC client change) interrupts *all* household SSO. Changes are
   additive; **run `authelia validate-config` before restart**, in a quiet window. Mirror every
   live change into the sanitized in-repo template the same session.
2. **Shared Postgres17 + Saturn host.** The real coupling is **thermal** — a transcode storm can
   trip a host shutdown. Reuse existing derived mp4s (no mass re-transcode); 1-thread throttle;
   stay <70 °C. That rule was set for **Saturn**, which shuts down fast when hot. The worker —
   and so every transcode — has run on **jupiter** since 2026-07-15; jupiter's thermal guard
   (sensor and threshold) is tracked in MEMNG-354.
3. **Traefik** routes per-service (own router) — additive and isolated, but a bad router config
   can still cascade: do router changes as their own change in a quiet window.

## Architecture

- **Backend:** FastAPI, SQLAlchemy 2 async, Alembic, pydantic-settings, structlog;
  **uv + ruff + pyright**, `python:3.13-slim`. Serves SPA + `/api/*` (clean REST) +
  `/api/v1/*` + `/file/*` (read-only compat for unmigrated satellites) + `/api/events` (SSE).
- **Frontend:** Vue 3.5 + PrimeVue 4 (Aura, `.app-dark`) + Tailwind v4, Vite SPA,
  **TanStack Query** for server-state (no Pinia), types **generated** from the OpenAPI schema.
- **Auth:** Authelia OIDC via a confidential **BFF** — tokens server-side, httpOnly rolling
  session cookie. Token/JWKS via **public** `https://auth.discomarder.live/...` URLs only
  (never `http://authelia:9091`). `client_secret_post` + `one_factor`.
- **Media:** two containers, one image — `memos-ng-app` + `memos-ng-worker`
  (`command: worker`; its limits live in the stack compose, which is the authority — `cpus: 8`,
  `mem_limit: 8g` since the 2026-07-15 move to jupiter). Queue = Postgres `media_jobs`
  with `FOR UPDATE SKIP LOCKED`. HEIC/HEVC originals kept as archive master (ADR-0008).
  The one worker process drains the queue with **two in-process consumers**, routed by a
  claim-time predicate: `transcode` (a slow-lane `derive` job whose attachment is a video) and
  `light` (everything else), so a long transcode no longer parks the photos behind it. One slot
  each (`WORKER_TRANSCODE_SLOTS=1`, `WORKER_LIGHT_SLOTS=1`; more than 1 is refused at startup),
  checked against `WORKER_CPU_BUDGET=8` at a measured `WORKER_TRANSCODE_CPU_COST=3` per transcode
  by `worker.validate_worker_slots()`. `worker.run()` is the supervisor and owns the heartbeat:
  after one stamp at startup (before the consumers exist) it stamps only while both consumers
  are alive, under their error streak, and inside their inactivity bound
  (`WORKER_TRANSCODE_STALL_SECONDS=3905`, `WORKER_LIGHT_STALL_SECONDS=900`). `claimed_by` ends in
  `/transcode` or `/light`.

Full detail: `docs/plans/PLAN_2026-06-20_memos-ng.md`.

## Hosts: jupiter (local, runs the app) + Saturn (NAS, runs the DB & holds data)

The app/worker containers run on the **local machine (jupiter)** — deploy there with the local
`docker`, no SSH. Saturn hosts Postgres17 and the data volumes. Saturn conventions:

- SSH alias: **`ssh saturn.local`** (Host in `~/.ssh/config`, port 27022, user `fabian`).
  **merkur** (`ssh merkur.local`) is the Mac mini M1 **build host for the iOS client** — no app
  containers run there.
- **The orchestrator drives merkur over SSH end-to-end — never hand iOS build/archive/upload
  steps back to the user.** `git pull`, `./bin/generate.sh`, the gate, and the release all run via
  `ssh merkur.local`; export `PATH=/opt/homebrew/bin:$PATH` and
  `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer` first (neither `xcodegen` nor the real
  `xcodebuild` is on the non-interactive PATH). The TestFlight release is **`fastlane beta`** (App
  Store Connect API key + a dedicated `memosng-ci` keychain — codesign can't reach the GUI login
  keychain over SSH, `errSecInternalComponent`); it archives and hand-assembles the IPA because
  Xcode 26.6's `-exportArchive` fails with an opaque `Copy failed`. Distribution to the `household`
  internal group is automatic. Full procedure: the iOS repo's `docs/TESTFLIGHT.md` §8b.
  (2026-08-26: two bad TestFlight builds came from hand-driven archives.)
- Non-interactive SSH needs **full paths + sudo** for docker/git:
  `sudo /usr/local/bin/docker …`, `/usr/local/bin/git …`. docker/git are in the sudo
  exclusion list (no password). Other commands (mkdir, tee) have **no** sudo — write via
  `docker exec` into mounted volumes, or write to `/tmp` then `sudo cp`.
- **No scp/SFTP.** Move files with `cat local | ssh saturn.local 'cat > /path'` or git.
- **Protected-path writes:** write to `/tmp` on Saturn, then `sudo cp` — do **not**
  heredoc-through-`sudo tee` (produces 0-byte files). sudo password (when needed) is in
  `/volume1/docker/.sudo_pass` on Saturn.
- **Deploy stack (jupiter):** `/opt/jupiter-stacks/stacks/memos-ng/docker-compose.yml`. Deploy
  with `docker compose -f … up -d --force-recreate` (never `restart` — reuses the old image).
  Verify the running image Id == the built `memos-ng:local` Id + a live `/health` before calling
  it deployed. Traefik takes ~20–40s to re-sync a new container — a brief external 404 right after
  deploy is normal; wait and retry before assuming a routing break.
- **Containers writing a bind-mounted `/volume1/docker/...` Saturn share must run as the share
  owner** (`user: "1027:100"`) — the Synology share ACL masks the unix mode, so a 0777 dir still
  denies a non-owner container UID. Verify with a real file write (heartbeat / derived asset), not
  just `mkdir -p` (which no-ops on existing dirs and hides the denial).

## G1 compat fixtures — `fixtures/live-0.29.1/`

Live memos 0.29.1 API responses captured 2026-06-20 — ground truth for the T4.1 compat contract
tests. **Read `fixtures/live-0.29.1/NOTES.md`** — two findings deviate from the plan: (F1) no-auth
returns 200/PUBLIC-only, **not 401**; (F2) original-file fetch over `/file/` 404s today, only
derived tiers are HTTP-served. Do not regenerate after old memos is stopped.

## Work discipline (ticket-scoped)

Work is ticket-scoped (windshift workspace **MEMNG**, id 16 — see the tracker for status, which
META never owns). Each ticket ends at its own objective verification gate: tests exist and pass,
the LOCAL gate is green (below), evidence shown. Finish one before starting the next; no
half-finished branches left open. For a substantial or concurrency/data-critical change, add an
independent review before merge (the local gate is the floor, not the ceiling). A prod deploy or
prod DB migration additionally goes through the plan gate first (see CARDINAL RULE).

## Tests first (front, back, and center)

Real automated tests lead every task — not manual checks. A task's verification gate is not
met until its tests exist and pass.
- **Real, not fake:** a test must fail when the feature is broken (remove the implementation →
  test goes red). No test that builds its own expected value and asserts against it. Never mock
  the unit under test.
- **Mutation controls are run, not trusted.** A repo-carried mutation under `tests/mutations/` is
  run by `scripts/check-mutations.sh`, which applies each patch in place with a sha256-proven
  revert, runs the module under its `MEMNG267_MUTATION` value, and requires exit 1 with exactly
  the FAILED set named in `tests/mutations/expected.tsv`; a mutation run that exits 0 is a defect.
- **Coverage:** every new/changed backend function gets a pytest unit test; every frontend
  view/composable gets a vitest test. Classification/routing heuristics need a case per input
  category, not just the happy path.
- **Manual verification supplements, never replaces.** Live `curl`/browser checks are evidence on
  top of tests.
- **The LOCAL gate is the gate — there is NO CI** (GitHub Actions abandoned, MEMNG-1 cancelled).
  A task is not done until, run **bare** (never piped — a pipe hides the exit code)
  and each exit code cited:
  `uv run pyright app/ && uv run ruff check app/ tests/ scripts/ &&
  uv run ruff format --check app/ tests/ scripts/ && uv run pytest -q && uv run alembic check`
  are all green. Never commit while any is RED. The test DB the gate needs: bring it up with
  `docker compose -f compose.dev.yaml up -d` and export
  `DATABASE_URL_SYNC=postgresql+psycopg2://memosng:memosng@localhost:5433/memosng` (pytest and
  the app fall back to that DSN; `alembic check` reads it explicitly).
- **Parallel runs get their own database** — the session fixture drops `public`, so two suites on
  the shared `memosng` tear each other's tables down mid-run. A concurrent run (parallel agents, or
  an agent alongside you) uses `memosng_test_<suffix>`, which `tests/db_guard.py` allowlists on the
  local hosts: create it with
  `docker exec memos-ng-db-1 psql -U memosng -d memosng -c "CREATE DATABASE memosng_test_a"`
  (the dev container's `memosng` role is its bootstrap superuser, so it may CREATE DATABASE; the
  same-named prod role on postgres17 has neither SUPERUSER nor CREATEDB), export **both** `DATABASE_URL_SYNC=...` and
  `DATABASE_URL=postgresql+asyncpg://memosng:memosng@localhost:5433/memosng_test_a` at it (the
  app's async engine reads the latter; a mismatch is refused at collection), and
  `DROP DATABASE memosng_test_a` when the run is done.
- **Local gates can't see the slim image.** When a change touches `pyproject.toml` deps or the
  `Dockerfile`, run a local image build BEFORE deploy — `python:3.13-slim` has no compiler, so
  C-extension wheels (e.g. blurhash-python) fail there but not on your dev machine. Every image
  build goes through `scripts/build-image.sh <version>+<short-sha>`, never a bare `docker build`:
  it shares a lock with `scripts/check-mutations.sh` and refuses a tree that differs from `HEAD`.

## Conventions

- Branch discipline: each task on `fix/<slug>` or `feature/<slug>`; never commit to main
  (hook-enforced). Merge via PR (squash). NOTE: the `/opt/jupiter-stacks` deploy repo is the one
  exception — it is pins-to-main (`ALLOW_MAIN_COMMIT=1` for compose bumps).
- **Off-loop blocking I/O uses `from anyio import to_thread` then `to_thread.run_sync(...)`** —
  never `import anyio` + `anyio.to_thread.run_sync`, which pyright misresolves to
  `BrokenWorkerInterpreter` and fails the gate.
- **Frontend: extract security/logic-critical behaviour into a pure function or middleware**
  (e.g. `renderMemoMarkdown`, `applyMediaEvent`, `toggleTask`, `tileKind`,
  `authMiddleware.onResponse`) and unit-test that directly — don't rely on driving it through a
  component in happy-dom, which lacks EventSource/canvas/clipboard/PushManager/localStorage.
- Every live Authelia/Traefik change is mirrored into the sanitized in-repo template the
  same session.
- Session review at the end of every implementation session (see `~/CLAUDE.md`).
- ARCHIMEDES: documented in BookStack book **1648 "memos-ng — Features"** (Session Reviews
  chapter 2689; page 1649 is the historical build roadmap; page 1646 is the superseded
  portainer-stacks draft). Read accepted ADRs before re-litigating settled decisions. Update
  BookStack pages via `node ~/projects/archimedes/lib/bookstack.mjs put-safe <id>` ONLY.

### BookStack Page Registry (resolve write targets BY ID — name-search is discovery-only)

- Book **1648** "memos-ng — Features": Session Reviews chapter **2689**, Decisions chapter **2690**,
  Lessons Learned page **1664**.
- Plan pages: **1649** (historical build roadmap), **1884** (feature-parity roadmap), **3151**
  (native iOS client + reference iOS architecture, 2026-08-23), **3819** (Places map, 2026-09-17),
  **3844** (two class-routed worker consumers, 2026-09-18).
- Feature pages (book level): **1663** (Media serving), **3836** (Places map).
- Release records (book level): **3846** (v1.7.1, 2026-09-18), **3837** (v1.7.0, 2026-09-18); earlier ones are 3043, 3049 and 3051.
- ADRs (chapter 2690): ADR-0009 **1661**, ADR-0010 **1692**, ADR-0011 **1881**, ADR-0012 **1882**,
  ADR-0013 **2316**, ADR-0014 **2584**, ADR-0015 **3152** (native client; PAT bearer; HTTPS
  callback brokered by the BFF), ADR-0016 **3209** (universal links skipped — the custom scheme is
  the permanent deep-link lane), ADR-0017 **3307** (daily digest absorbed into memos-ng, the
  satellite retired), ADR-0018 **3828** (Places map: coordinates in Postgres, one whole-set
  markers endpoint, client-side clustering, self-hosted PMTiles basemap).
- Sibling project **memos-ng-ios** (the reference iOS repo): book **3153** (Decisions **3154**,
  Session Reviews **3155**), Project Overview **3158**, doctrine "Reference iOS Architecture"
  **3156**, ADR-0001 **3157**.
- Estate ADR Decision Index **2697** (cross-project allow-list; edited only at ADR acceptance).
