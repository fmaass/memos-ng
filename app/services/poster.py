"""The video poster tier: the one derived image a video tile shows, and where it lives.

A video attachment gets no thumb and no mobile tier — its grid tile, its lightbox slide and the
`<video poster>` attribute are all this single file. The worker normally produces it by seeking
~12% into the clip and taking a hero frame (`app/services/media.poster_ffmpeg_cmd`), which is right
for a captured video and exactly wrong for one that OPENS on a title card: the seek exists to skip
an intro, so it skips the frame that names the video. A client that already knows which image
belongs on the tile therefore sends it with the memo (`MemoCreate.video_posters`) and it lands
here, in place of the frame the worker would have picked.

This module is the half of the tier the APP process may import: the tier's identity, its path and
its presence check. The generation itself lives in `app/services/media.py`, which only the worker
imports (see the blocking-I/O note at the top of that module) — nothing here pulls in an image
library, and its one filesystem call is offloaded. Same split, for the same reason, as
`app/services/printready.py`.
"""

from pathlib import Path

from anyio import to_thread

from app.config import settings

# `web` is the derived-asset directory a video's own artifacts share: the transcoded mp4 is
# `<uid>.mp4` and its poster frame `<uid>.poster.jpg`, both resolved from the uid alone by the
# signed (DB-free) tier route in app/routers/assets.py.
SUBDIR = "web"
SUFFIX = ".poster.jpg"
# What the poster tier is SERVED as. The route resolves the file from the uid with no database row
# behind it, so the content type is a constant of the tier rather than a stored per-file value —
# which is why an override has to be a JPEG rather than any image (app/services/uploads.py).
MIME = "image/jpeg"


def poster_path(uid: str) -> Path:
    """Where this attachment's poster frame lives. Resolves from the uid alone, like every other
    derived tier, and reads DATA_ROOT at call time so it stays overridable in tests."""
    return Path(settings.DATA_ROOT) / "assets" / SUBDIR / f"{uid}{SUFFIX}"


def poster_tmp_path(uid: str) -> Path:
    """The in-flight name a poster is written to before it is renamed into place.

    ONE definition, shared by both writers — the worker's ffmpeg extraction and the app's
    placement of a client-supplied override — because it is a real file in a real directory and
    each of them has to reap it. The `.tmp` sits BEFORE the extension rather than after it:
    ffmpeg infers its muxer from the extension, and a `.jpg.tmp` output fails outright.

    The two writers cannot collide over one uid. The app copies an override before the memo
    commits, so neither the attachment nor the media job that would derive a frame for it is
    visible to the worker until the file is already published.
    """
    path = poster_path(uid)
    return path.with_name(f"{path.stem}.tmp{path.suffix}")


def _exists_sync(path: Path) -> bool:
    return path.exists()


async def has_poster(uid: str) -> bool:
    """Whether this attachment's poster tier is on disk.

    A stat, not a database read: the poster is a FILE, and both things that put one there — the
    worker's hero frame and a client-supplied override — publish by renaming into place, so the
    file's existence is the only record either of them keeps. It is also exactly what the worker
    consults before deciding to derive one (`media.process_video_poster`), so this answer and that
    decision cannot disagree.
    """
    return await to_thread.run_sync(_exists_sync, poster_path(uid))
