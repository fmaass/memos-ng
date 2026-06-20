# memos-ng — Memos rebuilt on the household default web stack

## What this is / current state

A from-scratch replacement for the production `memos` (`neosmemo/memos:0.29.1`) plus its
three support containers (`memos-nginx`, `memos-imgproxy`, `memos-resizer`) and the Merkur
`memos-transcoder`. **All four collapse into memos-ng**: FastAPI serves the SPA + API +
asset tiers (no nginx), and an in-stack `memos-ng-worker` (pyvips + ffmpeg, Postgres
`media_jobs` queue) replaces imgproxy + resizer + the 5-minute NFS transcoder.

**State: greenfield.** Repo is not yet scaffolded (that's plan task T0.1). The only contents
today are `docs/plans/PLAN_2026-06-20_memos-ng.md` (the roadmap of record) and
`fixtures/live-0.29.1/` (captured compat ground truth — see below).

## CARDINAL RULE — build in parallel, never touch running memos until cutover

The whole point of this project: memos-ng is built, tested, and deployed **alongside** the
running memos at a **separate** host/DB/volume/Authelia-client. Production memos must keep
working untouched. The switch happens only at Phase 5 cutover, on explicit go-ahead, with a
≤5-min rollback and a 7-day soak. Until then:
- Staging host `memos-ng.discomarder.live`; never bind `memos.discomarder.live`.
- Separate Postgres DB `memosng`, separate volume `/volume1/docker/memos-ng`, separate
  Authelia client `memos-ng`. Never edit the live `memos` client/stack/volume.
- Old data is read **read-only** and **copied** (`cp -p`), never moved.

### The only shared-infra touchpoints (handle with care)
1. **Authelia restart** (registering the `memos-ng` OIDC client) interrupts *all* household
   SSO, not just memos. The change is purely additive (append a new `- client_id: 'memos-ng'`
   item; do not touch the `memos` block). **Run `authelia validate-config` before restart**,
   in a quiet window.
2. **Shared Postgres17 + shared Saturn host.** memos-ng's DB and worker share hardware with
   prod. The real coupling is **thermal** — a transcode storm can trip a host shutdown that
   takes prod memos down too. Reuse existing derived mp4s on migration (no mass re-transcode);
   1-thread throttle; monitor `/sys/class/thermal`, stay <70 °C.
3. **Traefik** routes per-service (label-based / own rule file) — additive and isolated; give
   memos-ng its own router so a bad config can't cascade.

## Architecture (target)

- **Backend:** FastAPI, SQLAlchemy 2 async, Alembic, pydantic-settings, structlog;
  **uv + ruff + pyright**, `python:3.13-slim`. Serves SPA + `/api/*` (clean REST) +
  `/api/v1/*` + `/file/*` (read-only compat for unmigrated satellites) + `/api/events` (SSE).
- **Frontend:** Vue 3.5 + PrimeVue 4 (Aura, `.app-dark`) + Tailwind v4, Vite SPA,
  **TanStack Query** for server-state (no Pinia), types **generated** from the OpenAPI schema.
- **Auth:** Authelia OIDC via a confidential **BFF** — tokens server-side, httpOnly rolling
  session cookie. Token/JWKS via **public** `https://auth.discomarder.live/...` URLs only
  (never `http://authelia:9091`). `client_secret_post` + `one_factor`.
- **Media:** two containers, one image — `memos-ng-app` + `memos-ng-worker`
  (`command: worker`, `cpu_shares: 512`, `mem_limit: 4g`). Queue = Postgres `media_jobs`
  with `FOR UPDATE SKIP LOCKED`. HEIC/HEVC originals kept as archive master (ADR-0008).

Full detail: `docs/plans/PLAN_2026-06-20_memos-ng.md`.

## Saturn (Synology NAS) conventions

- SSH alias: **`ssh saturn.local`** (Host in `~/.ssh/config`, port 27022, user `fabian`).
  Merkur IS the local machine — never ssh to it.
- Non-interactive SSH needs **full paths + sudo** for docker/git:
  `sudo /usr/local/bin/docker …`, `/usr/local/bin/git …`. docker/git are in the sudo
  exclusion list (no password). Other commands (mkdir, tee) have **no** sudo — write via
  `docker exec` into mounted volumes, or write to `/tmp` then `sudo cp`.
- **No scp/SFTP.** Move files with `cat local | ssh saturn.local 'cat > /path'` or git.
- **Protected-path writes:** write to `/tmp` on Saturn, then `sudo cp` — do **not**
  heredoc-through-`sudo tee` (produces 0-byte files). sudo password (when needed) is in
  `/volume1/docker/.sudo_pass` on Saturn.
- Stack git repo: `/volume1/docker-compose/stacks/`; app data: `/volume1/docker/`.
- Deploy with `up -d` (never `restart` — reuses the old image). Verify the running image
  digest + a live request before calling it deployed.

## G1 compat fixtures — `fixtures/live-0.29.1/`

Live memos 0.29.1 API responses captured 2026-06-20 while prod still runs — ground truth for
the T4.1 compat contract tests. **Read `fixtures/live-0.29.1/NOTES.md`** — two findings
deviate from the plan: (F1) no-auth returns 200/PUBLIC-only, **not 401**; (F2) original-file
fetch over `/file/` 404s today, only derived tiers are HTTP-served. Do not regenerate after
old memos is stopped.

## Phase discipline (hard gate)

Implement strictly in phases. **Each phase is a distinct, self-contained unit that must be
tested and verified against its acceptance criteria before the next phase starts.** Do not
begin Phase N+1 until Phase N is demonstrably done (its AC met, evidence shown). Same within a
phase: each task ends at its objective Verification gate. No racing ahead, no half-finished
phases left open. Current position: **Phase 0 → T0.1**.

**End-of-phase gate (mandatory):** close every phase with `/converge` for independent
validation before advancing. Sequence: finish the phase's tasks → `/session-review` +
`/implementation-report` (the packet `/converge` consumes) → **`/converge`** (L1–L4 validator
lenses + Codex cross-model review). Only a converged, adversarially-verified phase unlocks the
next one.

## Tests first (front, back, and center)

Real automated tests lead every task — not manual checks. A task's verification gate is not
met until its tests exist and pass.
- **Real, not fake:** a test must fail when the feature is broken (remove the implementation →
  test goes red). No test that builds its own expected value and asserts against it. Never mock
  the unit under test.
- **Coverage:** every new/changed backend function gets a pytest unit test; every frontend
  view/composable gets a vitest test. Classification/routing heuristics need a case per input
  category, not just the happy path.
- **Manual verification supplements, never replaces.** Live `curl`/browser checks are evidence
  on top of tests, not instead of them.
- **CI green is the per-task gate.** A task/phase is not done until the CI run on its commit is
  green across ALL jobs (backend, frontend, api-drift, release). Never advance past a red or
  pending CI. Never commit while a local gate is RED — run
  `uv run pyright app/ && uv run ruff check app/ tests/ scripts/ && uv run pytest -q && uv run alembic check`
  and observe all green first.
- **Local gates can't see the slim image.** When a change touches `pyproject.toml` deps or the
  `Dockerfile`, run a local/Saturn `docker build` BEFORE pushing — `python:3.13-slim` has no
  compiler, so C-extension wheels (e.g. blurhash-python) fail there but not on your dev machine.

## Conventions

- Branch discipline: each task on `feature/<task-slug>`; never commit to main.
- Every live Authelia/Traefik change is mirrored into the sanitized in-repo template the
  same session.
- Session review at the end of every implementation session (see `~/CLAUDE.md`).
- ARCHIMEDES: documented under the `memos-ng` BookStack shelf (to be created, plan T6.1);
  roadmap of record is BookStack page 1649 (book 1648 "memos-ng — Features"; page 1646 is the
  superseded portainer-stacks draft) / local plan above. Read accepted ADRs before
  re-litigating settled decisions.
