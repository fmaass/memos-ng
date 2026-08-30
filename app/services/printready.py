"""The print-ready rendition tier: what it is, where it lives, and how work for it is enqueued.

A photo book is laid out from 40-60 MP masters that no layout program can hold — 18.2 GB of PNG
across the curated set, ~180 MB decoded per image, and single files past the 50 MB upload cap of
the book service. The `printready` tier is the answer: ONE rung at 4096 px long edge, always JPEG,
colour-managed to sRGB at 300 dpi (MEMNG-49 / docs/plans/PLAN_2026-07-30_photobook-print-tier.md).
The geometry is not a round number pulled from the air: a 21x21 cm layflat page at 300 dpi is
2480 px, a 3:2 photo across 1.5 pages is 3720 px, plus bleed → 4096 (which is also exactly 2x the
`mobile` tier).

This module is the half of the feature the APP process may import: the tier's identity, its path,
which file it derives from, and the enqueue path. The generation itself (Pillow/ImageCms) lives in
app/services/media.py, which only the worker imports — nothing here pulls in an image library or
performs I/O outside an off-loop hop.

Generation is ON DEMAND and never part of an upload: re-encoding a 60 MP master costs seconds of
CPU on a thermally-bound shared host, so it happens when a book actually needs the photo, on the
slow lane, one at a time — never 238 times over because someone opened the library.
"""

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from anyio import to_thread
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Attachment, AttachmentRendition, JobKind, JobLane, JobState, MediaJob

# The tier key, deliberately NOT `print`: `print` already means "the separately-uploaded print
# master, streamed verbatim" and satellite clients depend on that meaning (app/routers/assets.py).
TIER = "printready"
SUBDIR = "printready"  # assets/<SUBDIR>/<uid>.jpg, the layout thumb/mobile use
IMAGE_PREFIX = "image/"

# The default rung: 4096 px on the long edge, never upscaled. It lives here rather than beside the
# encoder in app/services/media.py because it is tier IDENTITY, not an encoder setting — the
# photo-book manifest runs in the APP process (which must not import an image library to learn how
# big a rendition will be) and projects a not-yet-generated size from it. Settings can name a
# different rung per profile (MEMNG-238); this one is what the shipped tier on disk was built at,
# and what `DEFAULT_PROFILE` below therefore has to keep saying.
MAX_EDGE = 4096

# The encode settings the shipped tier was built with (MEMNG-49). They are the DEFAULT profile
# below, and the reason they are named here rather than only in media.py is that a profile is
# tier identity: the app process has to know which cache a request resolves to without importing
# an image library.
DEFAULT_QUALITY = 92
METADATA_STRIP = "strip"  # no EXIF at all — what the shipped encoder writes
METADATA_KEEP_DATES = "keep_dates"  # a REBUILT whitelist of date tags, never a pass-through
METADATA_POLICIES = (METADATA_STRIP, METADATA_KEEP_DATES)
DEFAULT_METADATA_POLICY = METADATA_STRIP
DEFAULT_GRAYSCALE_SINGLE_CHANNEL = True
ORIGINAL_LONG_EDGE = 0  # "no cap": the source's own size, never upscaled anyway
LONG_EDGES = (ORIGINAL_LONG_EDGE, 4096, 5000)

# The key the shipped tier already answers to, and the ONLY key whose files live at the legacy
# path. Every other profile derives into `assets/printready/<key>/<uid>.jpg`, which is what keeps
# a settings change from ever invalidating — or silently rewriting — the tier that is on disk.
DEFAULT_KEY = "print-4096"
_ORIGINAL_TOKEN = "orig"
_METADATA_TOKENS = {METADATA_STRIP: "strip", METADATA_KEEP_DATES: "dates"}
_TOKEN_METADATA = {token: policy for policy, token in _METADATA_TOKENS.items()}
_KEY_RE = re.compile(r"^print-(orig|[1-9]\d{1,4})-q(\d{2})-(strip|dates)-(gray|rgb)$")


@dataclass(frozen=True, slots=True)
class PrintProfile:
    """Every byte-affecting decision the print encoder makes, as one value.

    It is a VALUE and not a settings row on purpose: a rendition is derived once and then read for
    as long as it survives, so what produced its bytes has to be carryable — into the job row that
    will run minutes later, into the path the file lands at, and into the manifest that names it.
    Reading the settings again at any of those points would let a change between the request and
    the encode decide the bytes, and nothing would say which profile the file on disk actually is.

    `long_edge = ORIGINAL_LONG_EDGE` means "no cap"; the downscale never upscales either way.
    """

    long_edge: int
    jpeg_quality: int
    metadata_policy: str
    grayscale_single_channel: bool

    @property
    def key(self) -> str:
        """The canonical, path-safe name of this profile — one spelling per profile, both ways.

        The default answers to `print-4096` and nothing else, because that string is already the
        name of the shipped tier and its files already sit at the legacy path. Every other profile
        spells out each field that changes the bytes — `grayscale_single_channel` included, since
        toggling it is the difference between a 3-channel and a 1-channel JPEG and a key that
        omitted it would go on serving the wrong one from cache.
        """
        if self == DEFAULT_PROFILE:
            return DEFAULT_KEY
        edge = _ORIGINAL_TOKEN if self.long_edge == ORIGINAL_LONG_EDGE else str(self.long_edge)
        gray = "gray" if self.grayscale_single_channel else "rgb"
        return f"print-{edge}-q{self.jpeg_quality}-{_METADATA_TOKENS[self.metadata_policy]}-{gray}"

    @property
    def subdir(self) -> str | None:
        """The directory under `assets/printready/` this profile derives into, or None for the
        default — whose files stay exactly where they have always been, byte for byte."""
        return None if self == DEFAULT_PROFILE else self.key


DEFAULT_PROFILE = PrintProfile(
    long_edge=MAX_EDGE,
    jpeg_quality=DEFAULT_QUALITY,
    metadata_policy=DEFAULT_METADATA_POLICY,
    grayscale_single_channel=DEFAULT_GRAYSCALE_SINGLE_CHANNEL,
)


def profile_from_key(key: str) -> PrintProfile | None:
    """Parse a profile key back into the profile that produced it, or None if it is not one.

    The WORKER is why this exists rather than a lookup table of the profiles settings currently
    name: a job enqueued under one settings row may not run until after that row has changed, and
    it still has to write the bytes that were asked for. So the key is a lossless encoding and the
    job carries it — never a pointer back into settings.

    Canonicality is enforced by re-deriving: a key that parses but is not what `PrintProfile.key`
    would produce for the result (`print-4096-q92-strip-gray`, a leading zero, a second spelling of
    the default) is refused. One profile, one name, in both directions.
    """
    if key == DEFAULT_KEY:
        return DEFAULT_PROFILE
    match = _KEY_RE.match(key)
    if match is None:
        return None
    edge_token, quality, metadata_token, gray_token = match.groups()
    profile = PrintProfile(
        long_edge=ORIGINAL_LONG_EDGE if edge_token == _ORIGINAL_TOKEN else int(edge_token),
        jpeg_quality=int(quality),
        metadata_policy=_TOKEN_METADATA[metadata_token],
        grayscale_single_channel=gray_token == "gray",
    )
    return profile if profile.key == key else None


def printready_path(uid: str, profile: PrintProfile | None = None) -> Path:
    """Where this attachment's print rendition lives, for the DEFAULT profile unless told
    otherwise. Resolves from the uid alone, like every other derived tier — read at call time so
    DATA_ROOT stays overridable in tests.

    `profile=None` is the default profile and the legacy path, which is what keeps every caller
    that serves or sweeps "the printready tier" (the asset routes, the export's tier map) pointed
    at exactly the file they have always been pointed at.
    """
    root = Path(settings.DATA_ROOT) / "assets" / SUBDIR
    subdir = (profile or DEFAULT_PROFILE).subdir
    return (root / subdir / f"{uid}.jpg") if subdir else (root / f"{uid}.jpg")


def profile_variant_paths_sync(uid: str) -> list[Path]:
    """Every NON-default profile's rendition of this uid that is on disk right now, sorted.

    Enumerated rather than derived, and that is the whole difficulty: the set of profiles that
    ever existed is unbounded (any settings the admin has passed through), and nothing records
    which ones were used, so a deletion path that only unlinked the profiles settings name TODAY
    would leave a file behind for every profile the admin has since moved away from — bytes that
    no row names any more, which is the one outcome app/services/reclaim.py exists to prevent.

    EVERY file the uid owns in each profile directory, matched by the `<uid>.` prefix rather than
    by the final name: an encode interrupted between `img.save` and `os.replace` leaves a
    `<uid>.jpg.tmp` behind (media._atomic_save), and a list that named only `<uid>.jpg` would walk
    straight past it — the legacy path's temp is enumerated explicitly by media.derived_outputs,
    but a profile directory has no such hand-written list. The trailing dot is what keeps the
    prefix honest: uid `abc` matches `abc.jpg` and `abc.jpg.tmp`, never `abcd.jpg`.

    The legacy path is deliberately NOT included: it is `printready_path(uid)` and every caller
    already has it. Blocking I/O — callers on the request loop hop off it first.

    **An empty list means "this uid owns nothing in any profile directory", and nothing else.**
    Only a genuinely ABSENT tier directory (`FileNotFoundError` — the tier has never been written)
    is answered that way. Every other OSError PROPAGATES, because the alternative is the worst
    outcome this module has: a transiently unreadable mount would report "no assets", the caller
    would delete the row, and the renditions would survive with nothing naming them. A caller that
    cannot establish the set must fail closed and retry later, not proceed on a guess.
    """
    root = Path(settings.DATA_ROOT) / "assets" / SUBDIR
    prefix = f"{uid}."
    try:
        subdirs = sorted(entry for entry in root.iterdir() if entry.is_dir())
    except FileNotFoundError:  # the tier has never been written — genuinely nothing to enumerate
        return []
    found: list[Path] = []
    for sub in subdirs:
        try:
            found += sorted(
                entry
                for entry in sub.iterdir()
                if entry.name.startswith(prefix) and entry.is_file()
            )
        except FileNotFoundError:
            # The directory was removed between the listing above and this read: it holds nothing
            # now, which is the same answer as never having existed. Any OTHER error still
            # propagates — see the contract above.
            continue
    return found


def is_print_variant(variant_key: str) -> bool:
    """A rendition holding a print master: the bare `print` or a labelled `print:<slug>` (live data
    carries `print:glossy`). Matched exactly, not by prefix — a future `printable`/`print-proof`
    key must not be mistaken for one."""
    return variant_key == "print" or variant_key.startswith("print:")


def print_rendition(attachment: Attachment) -> AttachmentRendition | None:
    """The print master to derive from, or None. `renditions` is selectin-loaded, so this reads
    already-fetched rows and issues no query.

    Lowest id wins when a photo carries several (glossy AND matte): the earliest-attached master.
    Choosing between them per book is a selection question, and selection is Phase B's — this only
    has to be DETERMINISTIC, so that re-running an export cannot silently swap the source.
    """
    prints = [r for r in attachment.renditions if is_print_variant(r.variant_key)]
    return min(prints, key=lambda r: r.id) if prints else None


def source_path(attachment: Attachment) -> Path | None:
    """The file the print rendition is made FROM: the print master when the photo has one (it is
    the user's own edited crop for print), else the original.

    Never a screen tier — thumb/mobile are 800/2048 px and upscaling one to 4096 would print a
    blurred image at full page size. None means there is nothing to derive from at all.
    """
    rendition = print_rendition(attachment)
    if rendition is not None:
        return Path(rendition.storage_path)
    return Path(attachment.storage_path) if attachment.storage_path else None


def _effective_mtime(st: os.stat_result) -> float:
    """The moment the file itself last changed — st_ctime, deliberately not st_mtime.

    mtime is untrustworthy in BOTH directions here: media._stamp_mtime rewrites a derived tier's
    mtime to the photo's capture time, and capture time descends from the client-supplied upload
    last_modified, which nothing bounds — a future-dated stamp would make a rendition read as
    "fresh" forever, silently shipping stale print bytes after its source is replaced.

    st_ctime is the inode's change time: every write, replace, and the utime stamp itself bump it
    to NOW, and userland cannot set it. Comparing ctime on both sides therefore reads "rendition
    generated after the source last changed", errs toward regenerating (a source restored from
    backup or re-copied gets a fresh ctime and its rendition is rebuilt — the safe direction),
    and is immune to whatever value the mtime was stamped with.
    """
    return st.st_ctime


@dataclass(frozen=True, slots=True)
class SourceProbe:
    source_present: bool  # the file to derive from exists (nothing to do if it doesn't)
    current: bool  # a non-empty rendition exists and is at least as new as that source
    # When the source file itself last changed (_effective_mtime, i.e. ctime), or None if it is
    # gone. Carried out of the stat that already happens here so a caller never has to re-stat:
    # the photo-book export compares it against the time a job failed, to tell a failure that
    # still applies from one whose source has since been replaced.
    source_changed_at: float | None = None


def _probe_sync(pairs: list[tuple[int, Path, Path]]) -> dict[int, SourceProbe]:
    """Stat one batch of (attachment id, source, rendition) triples in ONE off-loop hop.

    Batched on purpose: the data volume is a bind-mounted share where every stat is a network
    round-trip, and a photo-book export asks about a few hundred attachments at once.
    """
    probes: dict[int, SourceProbe] = {}
    for attachment_id, source, dest in pairs:
        try:
            source_stat = source.stat()
        except OSError:  # the master is gone — a job could only fail
            probes[attachment_id] = SourceProbe(source_present=False, current=False)
            continue
        source_changed_at = _effective_mtime(source_stat)
        try:
            dest_stat = dest.stat()
        except OSError:  # not generated yet
            probes[attachment_id] = SourceProbe(
                source_present=True, current=False, source_changed_at=source_changed_at
            )
            continue
        probes[attachment_id] = SourceProbe(
            source_present=True,
            # Size guards the one case mtimes cannot see: a 0-byte file left by a crash between
            # create and write would otherwise count as a finished rendition forever.
            current=dest_stat.st_size > 0 and _effective_mtime(dest_stat) >= source_changed_at,
            source_changed_at=source_changed_at,
        )
    return probes


async def probe_renditions(
    attachments: Iterable[Attachment], *, profile: PrintProfile | None = None
) -> dict[int, SourceProbe]:
    """Each attachment's print-rendition freshness IN ONE PROFILE, in ONE batched off-loop pass.

    Exactly the two facts `enqueue_printready` decides on, exposed as a READ-ONLY query. The
    photo-book manifest needs them for members it deliberately does not enqueue (one whose last
    job failed terminally is reported, not retried), and deriving the freshness rule a second time
    there is how the two would drift — so both callers go through `_probe_sync`.

    A non-image, or an attachment with no source file recorded at all, comes back
    `source_present=False`: there is nothing to derive from, so there is nothing to wait for.
    """
    absent: dict[int, SourceProbe] = {}
    pairs: list[tuple[int, Path, Path]] = []
    for attachment in attachments:
        source = source_path(attachment) if attachment.mime.startswith(IMAGE_PREFIX) else None
        if source is None:
            absent[attachment.id] = SourceProbe(source_present=False, current=False)
            continue
        pairs.append((attachment.id, source, printready_path(attachment.uid, profile)))
    probes = await to_thread.run_sync(_probe_sync, pairs) if pairs else {}
    return absent | probes


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    """Every requested attachment lands in exactly one bucket, each in request order.

    The caller (the photo-book export, Phase B) needs the split, not just a count: `fresh` is ready
    to serve NOW, `enqueued` + `in_flight` are what a progress view has to wait for, and
    `ineligible` is what will never arrive and must not be waited on.
    """

    enqueued: tuple[int, ...]  # a job was created by this call
    fresh: tuple[int, ...]  # a current rendition is already on disk
    in_flight: tuple[int, ...]  # a printready job for it is already queued/claimed/processing
    ineligible: tuple[int, ...]  # no such attachment, not an image, or no source file on disk


def job_profile_predicate(profile: PrintProfile | None = None):
    """The `media_jobs` predicate that selects the jobs belonging to ONE profile.

    A NULL `profile_key` is the DEFAULT profile, not "any profile": every printready job written
    before this column existed produced the shipped 4096/q92 tier at the legacy path, so reading
    NULL as anything else would either resurrect those rows against a profile they never ran, or
    lose them entirely and let a duplicate 60 MP encode be queued for work already in flight.
    """
    key = (profile or DEFAULT_PROFILE).key
    if key == DEFAULT_KEY:
        return or_(MediaJob.profile_key.is_(None), MediaJob.profile_key == key)
    return MediaJob.profile_key == key


async def enqueue_printready(
    db: AsyncSession, attachment_ids: Iterable[int], *, profile: PrintProfile | None = None
) -> EnqueueResult:
    """Queue print renditions for existing attachments, skipping every attachment that needs none.

    Idempotent by three independent rules, because the expensive thing (a 60 MP re-encode on a
    thermally-bound host) must not happen twice for one photo:

    Everything below is scoped to ONE profile: its own cache file, and its own jobs. Two profiles
    of one photo are two different renditions with two different sets of bytes, so a job for one
    must neither satisfy nor block the other.

    * a rendition that is already current is left alone (see `_effective_mtime`);
    * an attachment whose printready job FOR THIS PROFILE is still queued/claimed/processing gets
      no second job —
      `done`/`failed` are terminal, so a finished job never blocks a legitimate regeneration and a
      failed one can be retried by asking again;
    * non-images, unknown ids, and attachments whose source file is missing are refused outright
      rather than queued to fail (a failing job would otherwise burn MEDIA_MAX_ATTEMPTS).

    Adds the rows and FLUSHES; the caller owns the transaction and commits. Two callers racing on
    the same attachment can both pass the in-flight check and create a duplicate job — harmless
    (the second regenerates the same bytes) and not worth a partial unique index today; the
    photo-book export serialises its own requests.
    """
    ids = list(dict.fromkeys(attachment_ids))  # de-duplicated, request order preserved
    if not ids:
        return EnqueueResult((), (), (), ())
    attachments = {
        a.id: a
        for a in (await db.execute(select(Attachment).where(Attachment.id.in_(ids)))).scalars()
    }
    active = set(
        (
            await db.execute(
                select(MediaJob.attachment_id).where(
                    MediaJob.attachment_id.in_(ids),
                    MediaJob.kind == JobKind.printready,
                    job_profile_predicate(profile),
                    MediaJob.state.notin_((JobState.done, JobState.failed)),
                )
            )
        ).scalars()
    )

    disposition: dict[int, str] = {}
    pairs: list[tuple[int, Path, Path]] = []
    for attachment_id in ids:
        attachment = attachments.get(attachment_id)
        if attachment is None or not attachment.mime.startswith(IMAGE_PREFIX):
            disposition[attachment_id] = "ineligible"
            continue
        source = source_path(attachment)
        if source is None:
            disposition[attachment_id] = "ineligible"
            continue
        pairs.append((attachment_id, source, printready_path(attachment.uid, profile)))

    probes = await to_thread.run_sync(_probe_sync, pairs) if pairs else {}
    for attachment_id, _source, _dest in pairs:
        probe = probes[attachment_id]
        if not probe.source_present:
            disposition[attachment_id] = "ineligible"
        elif attachment_id in active:
            # Checked BEFORE freshness: a running job is about to rewrite the file, so what is on
            # disk right now says nothing about what this attachment still needs.
            disposition[attachment_id] = "in_flight"
        elif probe.current:
            disposition[attachment_id] = "fresh"
        else:
            db.add(
                MediaJob(
                    attachment_id=attachment_id,
                    lane=JobLane.slow,
                    kind=JobKind.printready,
                    # The profile is SNAPSHOT onto the job, never looked up again: the worker may
                    # not reach this row until after the settings that produced it have changed,
                    # and it still has to write the bytes that were asked for.
                    profile_key=(profile or DEFAULT_PROFILE).key,
                    state=JobState.queued,
                )
            )
            disposition[attachment_id] = "enqueued"
    if any(d == "enqueued" for d in disposition.values()):
        await db.flush()
    return EnqueueResult(
        enqueued=tuple(i for i in ids if disposition[i] == "enqueued"),
        fresh=tuple(i for i in ids if disposition[i] == "fresh"),
        in_flight=tuple(i for i in ids if disposition[i] == "in_flight"),
        ineligible=tuple(i for i in ids if disposition[i] == "ineligible"),
    )
