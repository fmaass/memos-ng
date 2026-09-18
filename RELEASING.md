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
must fail exactly the probes `tests/mutations/expected.tsv` names. Images are never pulled from
a registry — the deploy image is built locally (below), so there is no `ghcr.io` push and no
Trivy gate; run the local `docker build` before deploy to catch slim-image/wheel breakage. Tag
agreement is enforced *before* tagging via the checker's `--tag`.

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

## Basemap archives (only when the map's tiles are being republished)

Not part of an ordinary release: the PMTiles archives live on the tiles share and outlive the
image. When a release does republish them, `scripts/tiles/extract.sh` writes them through
containers, and **the production tiles share accepts a write only from the share's owner** — from
inside a container and from the calling shell alike:

    docker pull busybox:stable      # once; the script refuses to start if it is missing
    scripts/tiles/extract.sh <tiles-dir> <regions.geojson> --user 1027:100

`--user` is not only the extractor's uid. With it, **nothing in the tiles directory is written by
the calling shell**: the extraction, the `.tmp` renames that publish each archive, the regions
sidecar, the pruning of superseded generations and `BUILD` all run in a container as that user.
Without it none of that changes — the writes are ordinary shell commands and no helper container
starts, which is what a local tiles directory wants.

The renames need a shell, and the go-pmtiles image is distroless (one binary, an empty
`/usr/bin`), so they run in a second, small image — `busybox:stable` by default, `--helper-image`
to name another. **It is never pulled at publish time**: the script checks it is present locally
before it writes anything, so a missing image or an unreachable registry is a refusal up front
rather than a failure between an extracted `.tmp` and a published name.

The share is an NFS-mounted Synology export whose ACL decides by numeric uid and masks the unix
mode, so the 0777 directory still refuses anyone but `1027:100` — the uid the app and worker
containers run as. `fetch-assets.sh` needs none of this: it writes as the operator into a
directory the operator may write. The publish order is the script's and stays as it is: planet,
then the detail archive's regions sidecar, then the detail archive, then `BUILD` last — each
written to a `.tmp` and renamed, so no half-written file is ever served, `BUILD` never names an
archive that is not there yet, and a published name is never rewritten or deleted.

## Acceptance probes (real surface: https://memos.discomarder.live)

1. `GET /health` → `{"status":"ok","version":"<version>+<short-sha>","db":"ok"}` (version read
   from the RUNNING app, not the compose file).
2. Anonymous `GET /api/memos` → **401** (auth enforcement intact).
3. `GET /` → 200 SPA shell (text/html).
4. One real derived-media fetch → 200 image bytes (exercises DB + storage + serving path).
5. `docker ps` both containers healthy, `RestartCount` stable; running image Id == freshly built
   image Id (local image — Id equality is the digest identity here).
