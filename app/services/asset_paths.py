"""Where an attachment's files live — one map, so nothing has to keep a second copy in step.

Resolving a tier and enumerating everything an attachment owns are the same knowledge, and three
callers need it: the serving routes, the export's media payloads, and every path that DELETES an
attachment (which must unlink exactly this set, or leave bytes behind that no row names any more).
It lives in the service layer rather than in the assets router because `app.services.reclaim`
needs it, and a service importing a router is an import cycle waiting to happen.
"""

from pathlib import Path

from app.config import settings
from app.models import Attachment
from app.services import poster, printready


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
            paths.append(resolved[0].with_suffix(".webp"))  # format-negotiated variant
    return paths
