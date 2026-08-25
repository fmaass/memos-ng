# Releasing memos-ng

The release contract this repo's release procedure reads. Every fact here was verified against the
live system when written (2026-08-13); correct it in the same change that alters the mechanism.

## Version-literal manifest

Authority: `pyproject.toml` `[project].version`. Mirrors that must agree:
`frontend/package.json` `.version` · `frontend/package-lock.json` `.version` **and**
`.packages[""].version` · `uv.lock` `[[package]]` entry for `memos-ng` · a `### v<version>`
changelog section in `README.md`.

Check: `python3 scripts/check_release_version.py [--tag vX.Y.Z]` —
**bare `python3`, never `uv run`**: `uv run` re-locks `uv.lock` before executing and silently
repairs exactly the staleness the checker exists to catch.

## No CI — the local gate is the gate

There is no GitHub Actions workflow (retired 2026-08-25 after every run had been refused at
the billing layer for 40+ runs while the local gates did all the catching). The release gate
is the LOCAL gate, run bare with each exit code cited:
`uv run pyright app/ && uv run ruff check app/ tests/ scripts/ && uv run pytest -q &&
uv run alembic check`, plus the frontend suite (`npx vitest run`) and the version checker
above. Images are never pulled from a registry — the deploy image is built locally (below),
so there is no `ghcr.io` push and no Trivy gate; run the local `docker build` before deploy
to catch slim-image/wheel breakage. Tag agreement is enforced *before* tagging via the
checker's `--tag`.

## Migrations

Alembic. Applied automatically at app-container boot (`docker-entrypoint.sh` runs
`alembic upgrade head` before uvicorn). Release review: inspect
`git diff <prev-tag>..HEAD -- alembic/`; any migration in the delta requires a rehearsal against
populated production-like Postgres **and** a fresh `pg_dump` of `memosng` (shared postgres17
cluster) before deploy.

## Deploy target

Host **jupiter** (the stack runs where the repo lives). Stack:
`/opt/jupiter-stacks/stacks/memos-ng/docker-compose.yml`, containers `memos-ng-app` +
`memos-ng-worker`, image **`memos-ng:local`** built locally:

    docker build --build-arg APP_VERSION=<version>+<short-sha> -t memos-ng:local .

`APP_VERSION` is **also hardcoded in the stack compose `environment:` for both containers** and
must be updated to the same string in the same deploy — the compose env is the runtime authority
for the backend (`app/version.py` reads the environment), while the build-arg bakes the frontend
bundle's displayed version. Deploy with `docker compose up -d --force-recreate` (never `restart`).

Rollback: retag the previous image `memos-ng:rollback-<short-sha>` *before* overwriting
`memos-ng:local`. To roll back, point the fixed tag back at it —
`docker tag memos-ng:rollback-<short-sha> memos-ng:local` — then revert the compose
`APP_VERSION` env lines and `docker compose up -d --force-recreate` (compose always deploys the
`memos-ng:local` tag; reverting only the env var would restart the BAD image under the old name).

Recreating the worker kills any in-flight transcode/frame job. A frame job interrupted mid-send is
no longer lost: the next worker start re-queues any claim older than `FRAME_CLAIM_STALE_MINUTES`
(15 by default) and retries it, at the price of a possible re-upload of a photo the album already
accepted — aura-frames dedups by sha256, so an unchanged photo does not duplicate (MEMNG-35). Still
deploy in a quiet window — worker heartbeat fresh and no active job in the log tail.

## Acceptance probes (real surface: https://memos.discomarder.live)

1. `GET /health` → `{"status":"ok","version":"<version>+<short-sha>","db":"ok"}` (version read
   from the RUNNING app, not the compose file).
2. Anonymous `GET /api/memos` → **401** (auth enforcement intact).
3. `GET /` → 200 SPA shell (text/html).
4. One real derived-media fetch → 200 image bytes (exercises DB + storage + serving path).
5. `docker ps` both containers healthy, `RestartCount` stable; running image Id == freshly built
   image Id (local image — Id equality is the digest identity here).
