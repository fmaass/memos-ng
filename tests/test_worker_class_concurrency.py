"""The two class-routed consumers against a REAL queue: contention inside one class, and the two
classes together draining a mixed one.

`worker.run()` spawns exactly one `consume_media` task per class, so the supervisor can never put
two claimers of the same class on the queue by itself. The claim statement is nevertheless written
to survive that (`FOR UPDATE ... SKIP LOCKED`), because a second worker process — the deployment
this project has already run, one container per lane — is the same contention. These tests drive
the drain directly for that reason: the question here is what the CLAIM does when two callers of
one class reach it together, not what `run()` spawns.

Nothing is mocked but the two seams the plan names:

* `media.slow_ffmpeg_cmd` — the collaborator whose own docstring names it the substitutable seam —
  is replaced by a REAL argv that appends its destination's basename to a ledger file before it
  writes the output. The ledger is therefore written by the transcode subprocess itself, once per
  run that actually happened: a job claimed twice leaves two lines, which is the only way
  "exactly once" is observable at all (the final `done`/`attempts == 0` row looks identical either
  way, which is what made the first draft of this proof worthless).
* `media.read_temp_millideg` — the thermal guard, so the verdict does not depend on how warm this
  host happens to be while the suite runs.

Everything else is the production path: real sessions, real row locks, real subprocesses, real
Pillow derivations.
"""

import asyncio
import contextlib
import secrets
from pathlib import Path

from app.config import settings
from app.services import media
from app.worker import run_media_drain
from PIL import Image
from sqlalchemy import create_engine, text

from tests.conftest import SYNC_URL

# Long enough that two claimers of one class are demonstrably inside their transcodes at the same
# time, short enough to keep the suite quick. Nothing is ASSERTED on this duration — it is the
# stub's work, not a synchronisation device; the claimers are aligned by a barrier below.
STUB_HOLD_SECONDS = 0.2
# A ceiling on drain iterations, so a queue this test did not seed (a row another module left
# behind) can slow the loop down but can never hang the suite.
DRAIN_BUDGET = 200


def _ledger_cmd(ledger: Path):
    """A real `slow_ffmpeg_cmd` replacement: appends the destination basename, then encodes.

    `>>` from a single small `printf` is an O_APPEND write, so two concurrent stubs interleave
    lines but never a line — which is what lets the ledger be counted rather than parsed.
    """

    def cmd(src: str, dest: str, threads: int) -> list[str]:
        return [
            "sh",
            "-c",
            'printf "%s\\n" "$(basename "$2")" >> "$1"; sleep "$3"; printf transcoded > "$2"',
            "sh",
            str(ledger),
            dest,
            str(STUB_HOLD_SECONDS),
        ]

    return cmd


def _seed_job(
    *,
    mime: str,
    lane: str,
    kind: str,
    filename: str,
    storage_path: Path,
    status: str = "processing",
    duration: float | None = None,
) -> tuple[str, int, int]:
    """A COMMITTED attachment + one queued job on it — `(uid, attachment_id, job_id)`.

    Committed on purpose: the claimers run on their own sessions and have to see, and lock, rows a
    fixture-scoped transaction would keep to itself.
    """
    uid = f"cls-{secrets.token_urlsafe(6)}"
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            aid = c.execute(
                text(
                    "INSERT INTO attachments "
                    "(uid, filename, mime, size, status, storage_path, duration) VALUES "
                    "(:u, :f, :m, 1, CAST(:s AS attachment_status), :sp, :d) RETURNING id"
                ),
                {
                    "u": uid,
                    "f": filename,
                    "m": mime,
                    "s": status,
                    "sp": str(storage_path),
                    "d": duration,
                },
            ).scalar_one()
            jid = c.execute(
                text(
                    "INSERT INTO media_jobs "
                    "(attachment_id, lane, kind, state, attempts, progress_pct) VALUES "
                    "(:a, CAST(:l AS job_lane), :k, CAST('queued' AS job_state), 0, 0) RETURNING id"
                ),
                {"a": aid, "l": lane, "k": kind},
            ).scalar_one()
    finally:
        eng.dispose()
    return uid, aid, jid


def _seed_video(tmp_path: Path) -> tuple[str, int, int]:
    """A slow-lane derive job on a video: the TRANSCODE class by the claim's own predicate.

    `duration` is set so `process_slow` never shells out to ffprobe — the stub below stands in for
    the encode, and a real probe of a file that is not a video is not part of this proof.
    """
    src = tmp_path / "originals" / f"{secrets.token_urlsafe(4)}.mov"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"not really a video")
    return _seed_job(
        mime="video/mp4",
        lane="slow",
        kind="derive",
        filename="v.mp4",
        storage_path=src,
        duration=2.0,
    )


def _png(path: Path, size=(400, 300)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (120, 80, 200)).save(path, "PNG")
    return path


def _heic(path: Path) -> Path:
    """A REAL HEIF file (pillow-heif's opener is registered by app.services.media), so the
    slow-lane image branch decodes what the deployed worker decodes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 300), (30, 140, 90)).save(path, format="HEIF")
    return path


def _cleanup(aids: list[int], jids: list[int]) -> None:
    # media_jobs carries no FK to attachments (MEMNG-41), so the jobs go by id too.
    eng = create_engine(SYNC_URL)
    try:
        with eng.begin() as c:
            for jid in jids:
                c.execute(text("DELETE FROM media_jobs WHERE id=:j"), {"j": jid})
            for aid in aids:
                c.execute(text("DELETE FROM attachments WHERE id=:a"), {"a": aid})
    finally:
        eng.dispose()


def _job_rows(jids: list[int]) -> dict[int, tuple[str, int]]:
    eng = create_engine(SYNC_URL)
    try:
        with eng.connect() as c:
            rows = c.execute(
                text("SELECT id, state, attempts FROM media_jobs WHERE id = ANY(:j)"),
                {"j": jids},
            ).all()
    finally:
        eng.dispose()
    return {row[0]: (row[1], row[2]) for row in rows}


async def _claimer(job_class: str, barrier: asyncio.Barrier | None) -> list[str]:
    """One consumer of one class, drained to empty — `consume_media`'s body without its backoff.

    The barrier (when two claimers share a class) releases both into their claim on the SAME event
    loop pass, so the contention is produced by an observed rendezvous rather than by a sleep a
    loaded machine could stretch past it. Whoever empties the queue first aborts the barrier, which
    frees a partner already waiting on it instead of parking it there for ever.
    """
    statuses: list[str] = []
    for _ in range(DRAIN_BUDGET):
        if barrier is not None:
            with contextlib.suppress(asyncio.BrokenBarrierError):
                await barrier.wait()
        status = await run_media_drain(job_class)
        statuses.append(status)
        if status == "idle":
            if barrier is not None:
                await barrier.abort()
            return statuses
    raise AssertionError(f"{job_class} never drained to idle within {DRAIN_BUDGET} passes")


async def _run_claimers(specs: list[tuple[str, asyncio.Barrier | None]]) -> list[list[str]]:
    """Run the given claimers concurrently and NEVER leave one behind: a failure in any of them
    cancels the rest before the loop this runs on is torn down."""
    tasks = [
        asyncio.create_task(_claimer(job_class, barrier), name=f"claimer-{i}-{job_class}")
        for i, (job_class, barrier) in enumerate(specs)
    ]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _pin_worker_environment(monkeypatch, tmp_path: Path, ledger: Path) -> None:
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "WORKER_LANES", "fast,slow")  # one worker, both lanes
    monkeypatch.setattr(settings, "SLOW_TRANSCODE_TIMEOUT_S", 120)
    monkeypatch.setattr(settings, "FFMPEG_THREADS", 1)
    monkeypatch.setattr(media, "read_temp_millideg", lambda: None)  # thermal guard open
    monkeypatch.setattr(media, "slow_ffmpeg_cmd", _ledger_cmd(ledger))


def test_two_transcode_claimers_each_job_runs_exactly_once(monkeypatch, tmp_path, db_engine):
    """AC2a — the SKIP LOCKED proof, read off work that actually happened.

    Six video derive jobs on six DISTINCT attachments and two claimers of the `transcode` class,
    released into each claim together. The ledger is written by the transcode subprocess itself, so
    a row claimed by both claimers appears twice in it; a queue that ends `done`/`attempts == 0`
    proves nothing on its own, because a double-claimed job ends exactly that way too.
    """
    ledger = tmp_path / "transcodes.log"
    _pin_worker_environment(monkeypatch, tmp_path, ledger)
    seeded = [_seed_video(tmp_path) for _ in range(6)]
    uids = [uid for uid, _, _ in seeded]
    aids = [aid for _, aid, _ in seeded]
    jids = [jid for _, _, jid in seeded]
    try:

        async def _go():
            barrier = asyncio.Barrier(2)
            return await _run_claimers([("transcode", barrier), ("transcode", barrier)])

        statuses = asyncio.run(_go())

        # Both claimers really worked — a run where one sat idle throughout would still have a
        # clean ledger and would have proven nothing about contention.
        assert all("processed" in side for side in statuses), statuses
        lines = ledger.read_text().split()
        assert sorted(lines) == sorted(f"{uid}.tmp.mp4" for uid in uids), lines
        assert _job_rows(jids) == {jid: ("done", 0) for jid in jids}
        for uid in uids:  # the transcode was published, not just counted
            assert (tmp_path / "assets" / "web" / f"{uid}.mp4").exists()
    finally:
        _cleanup(aids, jids)


def test_the_two_classes_together_drain_a_mixed_queue(monkeypatch, tmp_path, db_engine):
    """AC2b — the two consumers together finish every job, whatever class it falls in.

    NOT the SKIP LOCKED proof (that is the test above): one consumer per class is exactly what the
    supervisor spawns, so no two claimers here ever contend for one row. What this pins is the
    partition — videos to `transcode`, HEIC, ordinary images and printready to `light` — leaving no
    job that neither consumer will claim. The ledger covers the transcodes (the only jobs that
    shell out); the image jobs are read back through the tiers they published.
    """
    ledger = tmp_path / "transcodes.log"
    _pin_worker_environment(monkeypatch, tmp_path, ledger)

    videos = [_seed_video(tmp_path) for _ in range(2)]
    heics = [
        _seed_job(
            mime="image/heic",
            lane="slow",
            kind="derive",
            filename="p.heic",
            storage_path=_heic(tmp_path / "originals" / f"{i}.heic"),
        )
        for i in range(2)
    ]
    images = [
        _seed_job(
            mime="image/png",
            lane="fast",
            kind="derive",
            filename="p.png",
            storage_path=_png(tmp_path / "originals" / f"{i}.png"),
        )
        for i in range(2)
    ]
    prints = [
        _seed_job(
            mime="image/jpeg",
            lane="slow",
            kind="printready",
            filename="p.jpg",
            storage_path=_png(tmp_path / "originals" / "print.jpg"),
            status="ready",
        )
    ]
    seeded = [*videos, *heics, *images, *prints]
    aids = [aid for _, aid, _ in seeded]
    jids = [jid for _, _, jid in seeded]
    try:

        async def _go():
            return await _run_claimers([("transcode", None), ("light", None)])

        asyncio.run(_go())

        assert _job_rows(jids) == {jid: ("done", 0) for jid in jids}
        assert sorted(ledger.read_text().split()) == sorted(
            f"{uid}.tmp.mp4" for uid, _, _ in videos
        )
        for uid, _, _ in videos:
            assert (tmp_path / "assets" / "web" / f"{uid}.mp4").exists()
        for uid, _, _ in [*heics, *images]:
            assert (tmp_path / "assets" / "thumbs" / f"{uid}.jpg").exists()
    finally:
        _cleanup(aids, jids)
