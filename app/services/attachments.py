"""The media library's date expression — ONE definition, read by every surface that dates a photo.

A second copy is how two surfaces come to disagree about when a photo was taken: the Places map
and the library would place the same item on different days.
"""

from sqlalchemy import func

from app.models import Attachment, Memo


def taken_at_expr():
    """When the photo was taken, as an EXPRESSION over `attachments` outer-joined to `memos` —
    no stored date is ever rewritten to fill it, and no index or migration backs it.

    The owner's rule (2026-09-22): the EXIF capture instant, else the parent memo's date; the
    attachment's own `created_at` exists only for an orphan with no memo, and is NOT NULL, so the
    chain always yields a value. `captured_at` is deliberately absent: it is `capture_instant or
    client_mtime or now` (app/services/uploads.py), so it would date a photo by the browser's file
    mtime or by the day a migration ran. `capture_civil` is a zone-less wall clock and is not
    converted here — the SPA owns that policy (plan 3929, terminology).
    """
    return func.coalesce(Attachment.capture_instant, Memo.created_at, Attachment.created_at)
