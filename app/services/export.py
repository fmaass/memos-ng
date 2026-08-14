"""The export archive, generated as it is sent.

Nothing in here scales with the size of the export. The zip is written into a write-only sink
that is drained into the response after every write, media payloads are read a chunk at a time,
and the manifest accumulates in a spooled temp file — so peak memory is one chunk plus one
entry's compression window whether the export is one memo or a hundred thousand. That is what
replaced the old fixed memo cap (`EXPORT_MAX` + 413): the reason for a cap was that the whole
archive used to be built in a BytesIO first.

stdlib `zipfile` does the archive format itself (headers, CRCs, data descriptors, zip64, the
central directory) — it writes valid zips to an unseekable target since 3.7. It is used instead
of a streaming-zip package because every entry here needs an explicit mtime (a memo's
created_at, an asset's captured_at-stamped file time), which the streaming-zip packages'
add-data APIs do not expose.
"""

import contextlib
import json
import os
import time
import zipfile
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from io import BufferedReader
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import IO, Any, cast

from anyio import to_thread

MANIFEST_NAME = "manifest.json"
CHUNK_SIZE = 1 << 16  # media read/emit granularity
_SPOOL_MAX = 1 << 20  # manifest bytes held in memory before the spool rolls to disk
_FLUSH_AT = 1 << 16  # buffered manifest bytes before a spool write

_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)  # DOS timestamps: zipfile raises below this ...
_ZIP_MAX = (2107, 12, 31, 23, 59, 58)  # ... and the field wraps above this


def _zip_date_time(value: tuple[int, ...]) -> tuple[int, int, int, int, int, int]:
    """Clamp into the ZIP format's representable range.

    Out-of-range inputs are legitimate here: a scanned pre-1980 photo carries its captured_at
    both in the DB and (via media.py's mtime stamping) on its tier files, and ZipInfo would
    raise mid-stream — after the 200 is sent — leaving a truncated archive.
    """
    t = cast(tuple[int, int, int, int, int, int], tuple(value[:6]))
    if t < _ZIP_EPOCH:
        return _ZIP_EPOCH
    if t > _ZIP_MAX:
        return _ZIP_MAX
    return t


@dataclass(frozen=True, slots=True)
class MediaFile:
    """One media payload to carry beside a memo.

    `path` is None when the attachment has no file for the requested tier at all (a `print`
    tier on a photo with no print master) — recorded in the manifest like a missing file.
    `mtime` is the attachment's captured_at when the row has one; the file's own mtime is only
    the fallback (media.py stamps captured_at onto derived-tier files, but never onto originals,
    and not at all when captured_at is NULL — so the DB value is the deterministic source).
    """

    uid: str
    arcname: str
    path: Path | None
    mtime: datetime | None = None


@dataclass(frozen=True, slots=True)
class ExportItem:
    """One memo: its text entry, the manifest record describing it, and its media payloads."""

    arcname: str
    text: str
    mtime: datetime
    manifest: dict[str, Any]
    media: Sequence[MediaFile] = ()


class _Sink:
    """The archive's output: writes land here and are drained straight into the response.

    Deliberately has neither `tell` nor `seek` — that is what makes zipfile treat the target as
    unseekable and emit a data descriptor after each entry instead of seeking back to patch a
    header it can no longer reach.
    """

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._parts.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def drain(self) -> list[bytes]:
        parts, self._parts = self._parts, []
        return parts


class _ManifestSpool:
    """The manifest, accumulated out of memory.

    One record per memo is O(rows) rather than O(archive bytes), but a big enough export would
    still hold megabytes of it, so records go into a spooled temp file — memory up to
    `_SPOOL_MAX`, disk beyond it — in batched writes.
    """

    def __init__(self) -> None:
        self._file: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(max_size=_SPOOL_MAX)
        self._buf: list[bytes] = []
        self._buffered = 0
        self._count = 0

    async def add(self, record: dict[str, Any]) -> None:
        chunk = (b"[\n" if self._count == 0 else b",\n") + json.dumps(record, indent=2).encode()
        self._buf.append(chunk)
        self._buffered += len(chunk)
        self._count += 1
        if self._buffered >= _FLUSH_AT:
            await self._flush()

    async def _flush(self) -> None:
        if not self._buf:
            return
        data, self._buf, self._buffered = b"".join(self._buf), [], 0
        await to_thread.run_sync(self._file.write, data)

    async def finish(self) -> tuple[SpooledTemporaryFile[bytes], int]:
        """Close the JSON array and rewind, returning the spool and its byte length."""
        tail = b"\n]\n" if self._count else b"[]\n"
        self._buf.append(tail)
        self._buffered += len(tail)
        await self._flush()
        size = self._file.tell()
        await to_thread.run_sync(self._file.seek, 0)
        return self._file, size

    def close(self) -> None:
        self._file.close()


def _open_sync(path: Path) -> tuple[BufferedReader, os.stat_result] | None:
    """Open a media payload and size it in ONE off-loop hop, or None if it isn't there.

    The size is read off the open descriptor, not from a separate stat, so the entry header can
    never disagree with the bytes that follow it.
    """
    try:
        handle = path.open("rb")
    except OSError:
        return None
    return handle, os.fstat(handle.fileno())


async def _media_entry(
    zf: zipfile.ZipFile,
    sink: _Sink,
    arcname: str,
    handle: BufferedReader,
    stat: os.stat_result,
    mtime: datetime | None,
) -> AsyncIterator[bytes]:
    """Stream one already-opened media payload into the archive, a chunk at a time."""
    # captured_at from the row when it has one; the file's mtime otherwise (see MediaFile).
    stamp = mtime.timetuple()[:6] if mtime else time.localtime(stat.st_mtime)[:6]
    info = zipfile.ZipInfo(arcname, date_time=_zip_date_time(stamp))
    # Stored, not deflated: renditions are JPEG/MP4, already compressed, so deflating them would
    # spend CPU per byte for nothing. file_size lets zipfile decide about zip64 on its own.
    info.compress_type = zipfile.ZIP_STORED
    info.file_size = stat.st_size
    try:
        # blocking-ok: ZipFile.open writes this entry's header into the in-memory sink — it is a
        # zip-entry writer, not a filesystem open (the guard matches on the attribute name). The
        # only file here is `handle`, opened and read off-loop.
        with zf.open(info, "w") as dest:
            while data := await to_thread.run_sync(handle.read, CHUNK_SIZE):
                dest.write(data)
                for chunk in sink.drain():
                    yield chunk
    finally:
        await to_thread.run_sync(handle.close)
    for chunk in sink.drain():  # the entry's data descriptor
        yield chunk


async def _manifest_entry(
    zf: zipfile.ZipFile, sink: _Sink, manifest: _ManifestSpool
) -> AsyncIterator[bytes]:
    """Stream the manifest out of its spool as the archive's last entry."""
    spool, size = await manifest.finish()
    info = zipfile.ZipInfo(MANIFEST_NAME, date_time=time.localtime()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.file_size = size
    # blocking-ok: a zip-entry writer over the in-memory sink, as in _media_entry above; the
    # spool it copies from is read off-loop.
    with zf.open(info, "w") as dest:
        while data := await to_thread.run_sync(spool.read, CHUNK_SIZE):
            dest.write(data)
            for chunk in sink.drain():
                yield chunk
    for chunk in sink.drain():
        yield chunk


async def stream_zip(items: AsyncIterator[ExportItem]) -> AsyncIterator[bytes]:
    """Yield the export archive as its rows arrive: `<memo-uid>.md` per memo, each requested
    media payload beside it, and the manifest last.

    A media file that is missing on disk is SKIPPED and noted in the manifest rather than
    failing the export. Two reasons, both decisive: derived tiers are produced asynchronously,
    so at any moment some attachments legitimately have no file for a given tier yet (and image
    attachments never have a `web`/`poster` one at all) — and the response has already been sent
    as a 200 by the time the first payload is read, so there is no clean way to fail: the client
    would get a truncated archive with no explanation. The manifest is the explanation, and the
    rest of the export still arrives intact.
    """
    sink = _Sink()
    zf = zipfile.ZipFile(cast(IO[bytes], sink), "w", zipfile.ZIP_DEFLATED)
    manifest = _ManifestSpool()
    try:
        async for item in items:
            info = zipfile.ZipInfo(item.arcname, date_time=_zip_date_time(item.mtime.timetuple()))
            info.compress_type = zipfile.ZIP_DEFLATED  # memo text, unlike media, compresses
            zf.writestr(info, item.text)
            for chunk in sink.drain():
                yield chunk
            record = dict(item.manifest)
            media: list[dict[str, Any]] = []
            for entry in item.media:
                opened = await to_thread.run_sync(_open_sync, entry.path) if entry.path else None
                if opened is None:
                    media.append({"attachment": entry.uid, "file": entry.arcname, "missing": True})
                    continue
                handle, stat = opened
                async for chunk in _media_entry(
                    zf, sink, entry.arcname, handle, stat, entry.mtime
                ):
                    yield chunk
                media.append({"attachment": entry.uid, "file": entry.arcname, "size": stat.st_size})
            if item.media:
                record["media"] = media
            await manifest.add(record)
        async for chunk in _manifest_entry(zf, sink, manifest):
            yield chunk
        zf.close()
        for chunk in sink.drain():  # the central directory
            yield chunk
    finally:
        # `zf.fp is None` once the close above ran, so this only fires on a failed/abandoned
        # stream: it keeps the ZipFile from re-raising out of __del__ later. Its footer bytes go
        # nowhere (the sink dies with it) and the client's archive is unfinished either way — so
        # an error closing it must not replace the exception that got us here.
        if zf.fp is not None:
            with contextlib.suppress(Exception):
                zf.close()
        manifest.close()
