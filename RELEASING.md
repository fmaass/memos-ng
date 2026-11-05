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
in another never wait for each other. An flock lives until the last descriptor on it is closed,
and a process's descriptors all close when it dies — the mutation script starts its `uv` children
with that descriptor closed for the same reason — so **the lock itself needs no cleanup after any
exit**.

The tree is a separate question. A run killed with `SIGKILL` runs no trap: it cannot revert its
patch, and its lock is freed all the same, so waiting for the lock proves nothing about what is
in the files. That is why the build goes through `scripts/build-image.sh`, which takes the lock
and then refuses, naming the files, if any tracked file differs from `HEAD`. Restore such a file
by hand (`git checkout -- <file>`) — `scripts/check-mutations.sh` names it when a revert could not
be proved — and run the build again. Images are never pulled from
a registry — the deploy image is built locally (below), so there is no `ghcr.io` push and no
Trivy gate. To catch slim-image/wheel breakage before a deploy without touching the deploy tag,
build under another name: `IMAGE_TAG=memos-ng:compile-check-<short-sha> scripts/build-image.sh
<version>+<short-sha>` — same lock, same `HEAD` comparison, and `memos-ng:local` stays what is
running. Remove that tag afterwards. Tag agreement is enforced *before* tagging via the checker's
`--tag`.

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
any console error, any failed request, any first-view pin that falls outside the map pane, any
timeline bar on the page at all, and any request that leaves the smoke's own origin. The font route
is checked by a fetch from the page at the versioned address the style builds — the template's
own shape is pinned by the unit tests in `style.spec.ts`, and with no basemap archive in the
fixtures MapLibre issues no glyph request of its own. It exists because happy-dom cannot run
MapLibre: 1.7.0 shipped a
basemap with no icons and pins hidden under the bar past 913 green unit tests and two audit
rounds. The browser is found via `PLACES_SMOKE_CHROME`, else the newest Playwright build under
`PLAYWRIGHT_BROWSERS_PATH`/`~/.cache/ms-playwright`, else a system Chromium; with none it fails
with a message saying so. `playwright-core` is a devDependency and downloads no browser on
install.

The smoke serves on `127.0.0.1` at a port the OS picks: with `PLACES_SMOKE_PORT` unset (or empty)
it listens on port 0 and reads the bound port back, and every origin it checks derives from that
port, so two smokes started at the same time (two lanes on one host) do not collide. Before, the
default was a fixed 8741 and the second run died with `EADDRINUSE`. Set `PLACES_SMOKE_PORT` to a
decimal port 0-65535 to pin one; any other value fails the run with an error naming the variable.

## Migrations

Alembic. Applied automatically at app-container boot (`docker-entrypoint.sh` runs
`alembic upgrade head` before uvicorn). Release review: inspect
`git diff <prev-tag>..HEAD -- alembic/`; any migration in the delta requires a rehearsal against
populated production-like Postgres **and** a fresh `pg_dump` of `memosng` (shared postgres17
cluster) before deploy. The dump is the step below.

### The production backup step (before any release that carries a migration)

Run from jupiter, before the deploy, and cite the artifact in the release record. The dump is a
read of production; nothing here writes to it.

```bash
ver=<version>                                   # e.g. 1.8.0
d=~/backups/memosng-pre-$ver-$(date +%Y%m%d)
mkdir -p "$d" && chmod 700 "$d"
f=$d/memosng-pre-$ver.dump

# 1. the dump: custom format, streamed over ssh (-n: ssh must not read this shell's stdin)
ssh -n saturn.local 'sudo /usr/local/bin/docker exec postgres17 pg_dump -U memosng -d memosng -Fc' > "$f"
echo "pg_dump exit: $?"                       # must be 0: a failed dump leaves a file behind too
chmod 600 "$f"
sha256sum "$f" > "$f.sha256"

# 2. the table of contents, read with the dev container's pg_restore (same major version)
docker exec -i memos-ng-db-1 pg_restore -l < "$f" > "$f.toc"
chmod 600 "$f.sha256" "$f.toc"
grep -c 'TABLE DATA' "$f.toc"                   # one per table; 40 on 2026-11-05
```

A dump that was never restored is not a backup. Restore it into a throwaway database in the DEV
container (never into postgres17), compare it with production, and drop it:

```bash
docker exec memos-ng-db-1 psql -U memosng -d memosng -c "CREATE DATABASE memosng_restore_probe"
docker exec -i memos-ng-db-1 pg_restore --no-owner --no-privileges -U memosng -d memosng_restore_probe < "$f"
docker exec memos-ng-db-1 psql -U memosng -d memosng_restore_probe -Atc \
  "SELECT (SELECT count(*) FROM information_schema.tables
            WHERE table_schema='public' AND table_type='BASE TABLE'),
          (SELECT count(*) FROM memos), (SELECT count(*) FROM attachments),
          (SELECT version_num FROM alembic_version)"
docker exec memos-ng-db-1 psql -U memosng -d memosng -c "DROP DATABASE memosng_restore_probe"
```

The same four numbers are read from production, read-only, through the estate's
`~/projects/archimedes/bin/prod-sql.sh`; they must be equal, apart from rows written between the
dump and the read. The step is done when the dump, its `.sha256` and its `.toc` exist with mode
600, the `TABLE DATA` count equals the table count, and the restore probe matched.

**Retention.** A dump was about 0.9 MB on 2026-11-05. Every `~/backups/memosng-pre-<version>-<date>/`
directory is kept and nothing prunes them; revisit this when a dump passes 1 GB.

**Rehearsed 2026-09-18** against production, read-only: 611,456 bytes, 24 `TABLE DATA` entries, and
the restore probe matched production exactly (24 tables, 291 memos, 1730 attachments, alembic
`d3f81b06c9a4`).

## Deploy target

Host **jupiter** (the stack runs where the repo lives). Stack:
`/opt/jupiter-stacks/stacks/memos-ng/docker-compose.yml`, containers `memos-ng-app` +
`memos-ng-worker`, image **`memos-ng:local`** built locally:

    scripts/build-image.sh <version>+<short-sha>

Run it from the repository root. It takes the mutation gate lock, refuses if any tracked file
differs from `HEAD`, and then `exec`s the `docker build` — so the lock is held for at least as
long as the build is reading the context (see the gate section above). Both guards matter: the lock
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

Recreating the worker stops any in-flight media job, and a **deliberate** stop now puts that work
back in the queue instead of stranding it. `docker compose up -d --force-recreate` sends SIGTERM to
the worker process (the entrypoint `exec`s it, so the signal arrives at Python); the supervisor
tears both consumers down, and each cancelled job's row goes back to `queued` with its attempt
count untouched — provided the row is still `processing` under that worker's own claim, so a job
that had already finished (or failed, or been deleted) is left exactly as it is. The requeued job
is therefore re-runnable by the next worker start, and once it has run, the per-attachment order
guard stops withholding that attachment's later jobs.

That happens inside docker's stop grace, which is **10 s**: the stack compose sets no
`stop_grace_period`, so the default applies. The bounds the stop was designed against: the
transcode's own tree stop first (SIGTERM to the ffmpeg process group, `media.KILL_GRACE_SECONDS` =
5 s), then up to 3 s for the requeue write (`worker.CANCEL_REQUEUE_TIMEOUT_SECONDS`), then up to
1 s for the progress row (`worker.CANCEL_PROGRESS_CLEAR_SECONDS`) — 9 s of the 10, with both
consumers running their own in parallel. The light consumer adds up to 1 s of its own
(`media.CANCEL_SWEEP_TIMEOUT_SECONDS`) when the stop skipped `guard_publish`, and with it the sweep
that fences a file published for a photo deleted mid-job: a stop waited out a print encode or a
heavy image decode, or was delivered at the checkpoint's own first await. At most one of those
fires per stopped job, so it is 5 s on the light path against the transcode path's 9 and does not
move the total. The transcode path takes no bounded second of its own — its checkpoint fence is a
single synchronous unlink of this run's temp — because 9 + 1 would be the whole grace. These are **designed bounds, not hard caps**: an ffmpeg
that ignores SIGTERM also needs the SIGKILL step, which adds another 5 s, and nothing in the
process can bound the kernel.

The 9 s bounds the **consumers' stop path only**. What comes after it is not measured and is not
bounded by this code: the consumers' database sessions closing, asyncpg tearing down a connection
whose operation was cancelled, the supervisor unwinding, and `asyncio.run()`'s own shutdown of any
task still left (which re-cancels an abandoned requeue write and does not honour its 3 s bound).
Docker's SIGKILL at the end of the grace is the only deadline over the stop as a whole — and a
second SIGTERM sent meanwhile is deliberately a no-op, so it does not shorten anything either.

Two kinds of job the stop **waits out**: a print-ready encode and a heavy image decode (HEIC/AVIF).
Both run in a thread, which cannot be interrupted, so `process_printready_job` and the slow lane's
image branch let the running work finish before the cancellation goes on to the hand-back — an
abandoned thread would keep writing its rendition or tiers into their final paths after the
coroutine that guards the publish is gone. The light consumer runs one job at a time, so either
wait is one job's remaining thread work. It is **not** part of the bounds above: it happens in
front of them, so the 5 s quoted for the light path is the bounded part only. Nor is it new
waiting — before the decode moved into a thread it ran inline on the event loop, which held the
stop's delivery back for exactly the same time. If a wait outlasts the grace, this is the SIGKILL
case below.

**Past the grace nothing can be written**: docker sends SIGKILL. That strands a row only if a
claim was actually in flight — a worker sitting idle leaves nothing behind. A claim that was in
flight stays `processing` (exactly as after an OOM kill or a host crash) until a sweep requeues
it: `MEDIA_CLAIM_STALE_SECONDS` (7200 s) is the age at which a claim becomes eligible, and the
next sweep after that requeues it, so the delay is that threshold plus up to one sweep interval.
While such a row stands, the order guard withholds that attachment's later jobs.

A frame job interrupted mid-send is not lost either: the next worker start re-queues any claim
older than `FRAME_CLAIM_STALE_MINUTES` (15 by default) and retries it, at the price of a possible
re-upload of a photo the album already accepted — aura-frames dedups by sha256, so an unchanged
photo does not duplicate (MEMNG-35). Still deploy in a quiet window — worker heartbeat fresh and no
active job in the log tail: a quiet window is what keeps the SIGKILL case off the table.

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

With `--user` the tiles directory must already exist and be writable by that user: creating it is
itself a write, and in that mode the script performs none as the caller. (Without `--user` it is
created if missing, as before.)

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
archive that is not there yet, and a published name is never rewritten or repaired under its own
name. Names do leave, by one route only: `prune` removes generations older than the previous one,
together with their regions sidecars. It keeps the newest two of each kind by publication time and
does not check whether a client is still reading an older one.

## Acceptance probes (real surface: https://memos.discomarder.live)

1. `GET /health` → `{"status":"ok","version":"<version>+<short-sha>","db":"ok"}` (version read
   from the RUNNING app, not the compose file).
2. Anonymous `GET /api/memos` → **401** (auth enforcement intact).
3. `GET /` → 200 SPA shell (text/html).
4. One real derived-media fetch → 200 image bytes (exercises DB + storage + serving path).
5. `docker ps` both containers healthy, `RestartCount` stable; running image Id == freshly built
   image Id (local image — Id equality is the digest identity here).
