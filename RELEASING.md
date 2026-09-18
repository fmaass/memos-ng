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
`uv run pyright app/ && uv run ruff check app/ tests/ scripts/ &&
uv run ruff format --check app/ tests/ scripts/ && uv run pytest -q &&
uv run alembic check && scripts/check-mutations.sh`, plus the frontend suite (`npx vitest run`)
and the version checker above. `scripts/check-mutations.sh` runs the repo-carried mutation
controls: each patch in `tests/mutations/` is applied in place with a sha256-proven revert and
must fail exactly the probes `tests/mutations/expected.tsv` names.

**The mutation run and the image build must not overlap**, because for a few seconds at a time
the checkout on disk *is* mutated source, and `docker build` copies whatever it finds. Both take
the same lock — `mutation-gate.lock` in this working tree's git directory — so run either one and
the other sorts itself out. They behave differently on purpose: the build **waits**, saying so
once on stderr, while the mutation script **refuses at once** with `another process holds the
mutation gate lock`, exit 2, having patched nothing — a gate that blocked silently for minutes
would read as a hang. The lock is per working tree, so a build in one worktree and a mutation run
in another never wait for each other, and the kernel drops it when the holder dies, so **the lock
itself needs no cleanup after any exit**.

The tree is a separate question. A run killed with `SIGKILL` runs no trap: it cannot revert its
patch, and its lock is freed all the same, so waiting for the lock proves nothing about what is
in the files. That is why the build goes through `scripts/build-image.sh`, which takes the lock
and then refuses, naming the files, if any tracked file differs from `HEAD`. Restore such a file
by hand (`git checkout -- <file>`) — `scripts/check-mutations.sh` names it when a revert could not
be proved — and run the build again. Images are never pulled from
a registry — the deploy image is built locally (below), so there is no `ghcr.io` push and no
Trivy gate; run the local `docker build` before deploy to catch slim-image/wheel breakage. Tag
agreement is enforced *before* tagging via the checker's `--tag`.

### The Places browser smoke — mandatory for every release that touches `frontend/`

Any release whose range touches anything under `frontend/` additionally runs, from `frontend/`,
bare. The rule is the whole directory on purpose. A list of map files cannot be kept true: the
view is assembled from modules the rest of the app shares (the API client, the dark-mode switch
that picks the map's style, the media helpers, the lightbox), and the likeliest way to break the
map is a `maplibre-gl` bump in `package.json`, which touches no map file at all. The 1.7.1
fit-padding fix touched `frontend/src/views/PlacesView.vue`, outside every `places/` directory.
The smoke costs little: it ran in 13 s here, on top of a build the release makes anyway.

The command:

```
npm ci && npm run build && npm run smoke:places
```

A real headless Chromium opens the PRODUCTION build at `/places` on a desktop viewport
(1440x900) and a phone one (390x844) against synthetic committed fixtures, and exits non-zero on
any console error, any failed request, and any first-view pin that falls outside the map pane or
behind the timeline bar, and on any request that leaves the smoke's own origin. The font route
is checked by a fetch from the page at the versioned address the style builds — the template's
own shape is pinned by the unit tests in `style.spec.ts`, and with no basemap archive in the
fixtures MapLibre issues no glyph request of its own. It exists because happy-dom cannot run
MapLibre: 1.7.0 shipped a
basemap with no icons and pins hidden under the bar past 913 green unit tests and two audit
rounds. The browser is found via `PLACES_SMOKE_CHROME`, else the newest Playwright build under
`PLAYWRIGHT_BROWSERS_PATH`/`~/.cache/ms-playwright`, else a system Chromium; with none it fails
with a message saying so. `playwright-core` is a devDependency and downloads no browser on
install.

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

    scripts/build-image.sh <version>+<short-sha>

Run it from the repository root. It takes the mutation gate lock, refuses if any tracked file
differs from `HEAD`, and then `exec`s the `docker build` — so the lock is held for exactly as long
as the build is reading the context (see the gate section above). Both guards matter: the lock
covers a mutation run that is active right now, the `HEAD` comparison covers one that was killed
and could not revert.

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

### Rollback across a migration

The retag above is not enough once the release carried a migration. Migrations here are
**expand-only** (new columns nullable, new constraints additive), so the previous image's code runs
against the migrated schema unchanged — but it cannot START on it: `docker-entrypoint.sh` migrates
to its own bundled head, and the revision the newer image wrote is not in the older image's graph
("Can't locate revision"), while the worker's schema gate (`app/migrations.py`) waits for a head
equal to its own and therefore waits for ever. **Production never downgrades**; the rollback keeps
the new columns.

From 1.7.0 onwards that is one environment variable: set `ROLLBACK_MODE=1` on both services in the
stack compose, retag as above, and `docker compose up -d --force-recreate`. The entrypoint then
skips the migration and the gate accepts a head its graph does not know, with one warning line.
Clear the variable on the roll-forward.

Rolling back TO an image built before 1.7.0 has no such flag, so the old app is started through an
overlay instead. `compose.rollback.yml` in the stack repo, used as
`docker compose -f docker-compose.yml -f compose.rollback.yml up -d --force-recreate`:

```yaml
services:
  memos-ng-app:
    # The entrypoint's uvicorn line, reproduced WITHOUT the alembic call. The forwarded-IP default
    # has to be reproduced with it: uvicorn otherwise trusts 127.0.0.1 alone, every request keeps
    # Traefik's address, and the per-IP login limiter collapses into one household-wide bucket
    # (MEMNG-28). Dollar signs are doubled so Compose passes them to the shell, not its own
    # interpolation.
    entrypoint:
      - sh
      - -c
      - >-
        FORWARDED_ALLOW_IPS="$${FORWARDED_ALLOW_IPS:-127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7}";
        exec uvicorn app.main:app --host 0.0.0.0 --port 8000
        --proxy-headers --forwarded-allow-ips "$$FORWARDED_ALLOW_IPS"
  memos-ng-worker:
    # Stopped for the rollback window rather than started against a schema its gate refuses. Jobs
    # queue; nothing is lost. Derivations, frame sharing, digests and cleanup wait, while uploads
    # keep working and stay pending/processing until the roll-forward drains the queue.
    scale: 0
```

After a roll-forward, re-run any backfill the newer release depends on for the rows the OLD code
created in the meantime (for 1.7.0: `python -m app.scripts.backfill_location --execute`).

## Acceptance probes (real surface: https://memos.discomarder.live)

1. `GET /health` → `{"status":"ok","version":"<version>+<short-sha>","db":"ok"}` (version read
   from the RUNNING app, not the compose file).
2. Anonymous `GET /api/memos` → **401** (auth enforcement intact).
3. `GET /` → 200 SPA shell (text/html).
4. One real derived-media fetch → 200 image bytes (exercises DB + storage + serving path).
5. `docker ps` both containers healthy, `RestartCount` stable; running image Id == freshly built
   image Id (local image — Id equality is the digest identity here).
