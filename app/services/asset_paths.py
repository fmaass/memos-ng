"""Where an attachment's files live — one map, so nothing has to keep a second copy in step.

Resolving a tier and enumerating everything an attachment owns are the same knowledge, and three
callers need it: the serving routes, the export's media payloads, and every path that DELETES an
attachment (which must unlink exactly this set, or leave bytes behind that no row names any more).
It lives in the service layer rather than in the assets router because `app.services.reclaim`
needs it, and a service importing a router is an import cycle waiting to happen.
"""

from pathlib import Path

from anyio import to_thread

from app.config import settings
from app.models import Attachment
from app.services import poster, printready


def tiles_dir() -> Path:
    """Where the basemap archives and style assets are published.

    Settings-driven with a derived default rather than a constant, for the same reason the tier
    paths above are computed per call: tests and the dev server move DATA_ROOT, and a value frozen
    at import time would keep pointing at the directory that was configured when the module loaded.
    """
    if settings.TILES_DIR:
        return Path(settings.TILES_DIR)
    return Path(settings.DATA_ROOT) / "assets" / "tiles"


def derived_tier_path(uid: str, tier: str) -> tuple[Path, str] | None:
    """Derived tiers resolve from the uid alone (no DB row) — used by the signed fast path."""
    assets = Path(settings.DATA_ROOT) / "assets"
    if tier == "thumb":
        return assets / "thumbs" / f"{uid}.jpg", "image/jpeg"
    if tier == "mobile":
        return assets / "mobile" / f"{uid}.jpg", "image/jpeg"
    if tier == "web":
        return assets / "web" / f"{uid}.mp4", "video/mp4"
    if tier == "poster":
        return poster.poster_path(uid), poster.MIME
    if tier == printready.TIER:
        return printready.printready_path(uid), "image/jpeg"
    return None


def variant_paths(path: Path) -> list[Path]:
    """The format-negotiated siblings of one derived tier file.

    One definition, because two callers have to agree about it and they fail in opposite
    directions: everything that DELETES an attachment unlinks these alongside the tier itself
    (`all_asset_paths`), and anything that REPLACES a tier in place has to remove them
    (`app.scripts.backfill_hdr_posters`) — otherwise the serving path keeps handing the stale
    variant to every client that accepts webp (`app/routers/assets.py:_negotiated_path_sync`),
    and a replaced JPEG goes on being served as the picture it used to be.
    """
    return [path.with_suffix(".webp")]


def tier_path(attachment: Attachment, tier: str) -> tuple[Path, str] | None:
    """Resolve an attachment's file for a tier — the export builds its media payloads from the
    same map (app/routers/export.py); a second copy would drift the moment a rendition rung is
    added."""
    if tier == "original":
        return (Path(attachment.storage_path), attachment.mime) if attachment.storage_path else None
    if tier == "print":  # separately-uploaded print master (download-only); None if absent
        return (
            (Path(attachment.print_path), attachment.print_mime or "application/octet-stream")
            if attachment.print_path
            else None
        )
    return derived_tier_path(attachment.uid, tier)


def all_asset_paths(att: Attachment) -> list[Path]:
    """Every on-disk file an attachment owns: the original + each derived tier (+ webp variant).

    The full DERIVABLE set, never filtered by what happens to exist right now. A deleting caller
    unlinks this list and tolerates the ones that are absent — filtering by `exists()` first would
    miss a tier a worker publishes between the check and the unlink, and that file would outlive
    the row that names it (see app/services/reclaim.py for why that is the one outcome forbidden).
    """
    paths: list[Path] = []
    if att.storage_path:
        paths.append(Path(att.storage_path))
    if att.print_path:
        paths.append(Path(att.print_path))
    for rendition in att.renditions:  # hidden print masters (unique, unshared files)
        paths.append(Path(rendition.storage_path))
    for tier in ("thumb", "mobile", "web", "poster", printready.TIER):
        resolved = derived_tier_path(att.uid, tier)
        if resolved is not None:
            paths.append(resolved[0])
            paths.extend(variant_paths(resolved[0]))  # format-negotiated variants
    return paths


def _non_regenerable_paths(att: Attachment) -> list[Path]:
    """The files that only exist because somebody uploaded them: the screen original, the legacy
    print master, every rendition master, and — for a video — the poster tier.

    The poster is in this set even though it is derived, because it is the one derived file a
    CLIENT may supply (`uploads.attach_video_poster` writes the worker's own destination and the
    row records nothing about it), so a missing one cannot be told apart from an override that was
    removed — but only for a video that HAS an original, since that is what a poster is derived
    from. Everything else under `all_asset_paths` is regenerable from the original and is
    deliberately absent: a thumb the worker has not written yet is not damage.

    A row that names no file of its own therefore yields an empty set and is adoptable. That is
    the intended reading: this asks whether what the row NAMES is still there, and a placeholder
    that names nothing has lost nothing.
    """
    paths: list[Path] = []
    if att.storage_path:
        paths.append(Path(att.storage_path))
    if att.print_path:
        paths.append(Path(att.print_path))
    paths.extend(Path(rendition.storage_path) for rendition in att.renditions)
    if att.storage_path and att.mime.startswith("video/"):
        resolved = derived_tier_path(att.uid, "poster")
        if resolved is not None:
            paths.append(resolved[0])
    return paths


def _all_present_sync(paths: list[Path]) -> bool:
    return all(path.exists() for path in paths)


async def adoptable(att: Attachment) -> bool:
    """May this existing row be handed to a memo instead of the bytes the caller is holding?

    Only if every non-regenerable file it names is still there. A row whose original was removed
    under it — what a reclaim leaves behind when the unlink fails after the first file and the
    transaction rolls back — still matches a dedup candidate by every column it has, and adopting
    it would hand the memo a picture that cannot be served AND destroy the upload that still had
    the bytes. Refusing is always the safe direction: a dedup caller still holds the bytes and mints
    its own row, and a caller that named the uid itself is told the picture is missing.

    Asked under the candidate's row lock, held to commit, so the answer cannot go stale between
    the stat and the adoption. Every stat is a network round-trip on the deployed volume, so they
    go off the request loop.
    """
    return await to_thread.run_sync(_all_present_sync, _non_regenerable_paths(att))
