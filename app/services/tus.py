"""The tus 1.0 header grammar the `/api/tus` routes speak, as pure functions over the store's row.

Acceptance of `Upload-Metadata` is RESTRICTED to the keys an `Upload` row can hold, so that what
HEAD echoes is exactly what creation accepted: a key the row could not keep, or a value it would
store as something else, is refused at creation instead of being silently changed or dropped.
"""

import base64
import binascii
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from app.models import Upload
from app.services.uploads import EPOCH

TUS_VERSION = "1.0.0"
OFFSET_STREAM = "application/offset+octet-stream"

# The last millisecond `datetime` can represent (9999-12-31T23:59:59.999Z): a larger value has no
# `client_mtime` to land in.
MAX_LAST_MODIFIED_MS = 253402300799999
# At most 15 digits (the length of MAX_LAST_MODIFIED_MS): `int()` refuses strings past its digit
# limit with a bare ValueError, which would escape the 400 path; the grammar stops them first.
_CANONICAL_MS = re.compile(r"[1-9][0-9]{0,14}")
_NON_NEGATIVE = re.compile(r"[0-9]+")
_ACCEPTED_KEYS = frozenset({"filename", "filetype", "lastModified"})


class MetadataRefused(ValueError):
    """Creation metadata the store cannot hold faithfully. The message names the key."""


@dataclass(frozen=True)
class CreationMetadata:
    filename: str
    mime: str
    last_modified: int | None


def parse_upload_metadata(header: str) -> dict[str, bytes]:
    """`key base64(value)` pairs, comma-separated; a key may carry no value; keys are unique.
    Anything else raises ValueError. Values are bytes because the protocol allows binary ones."""
    pairs: dict[str, bytes] = {}
    for element in header.split(","):
        parts = element.strip().split(" ")
        if len(parts) > 2 or not parts[0]:
            raise ValueError(f"malformed: the pair {element.strip()!r} is not `key [base64]`")
        key = parts[0]
        if key in pairs:
            raise ValueError(f"malformed: duplicate key {key!r}")
        try:
            pairs[key] = base64.b64decode(parts[1], validate=True) if len(parts) == 2 else b""
        except binascii.Error:
            raise ValueError(f"malformed: the value of {key!r} is not base64") from None
    return pairs


def encode_upload_metadata(metadata: Mapping[str, bytes]) -> str:
    """The inverse of `parse_upload_metadata`: an empty value is written as the bare key."""
    return ",".join(
        f"{key} {base64.b64encode(value).decode()}" if value else key
        for key, value in metadata.items()
    )


def non_negative_header(value: str | None, name: str) -> int:
    """A header the protocol defines as a non-negative integer, or ValueError naming it."""
    if value is None or not _NON_NEGATIVE.fullmatch(value):
        raise ValueError(f"{name} must be a non-negative integer")
    return int(value)


def _column_length(column: str) -> int:
    length = getattr(Upload.__table__.c[column].type, "length", None)
    assert isinstance(length, int), column
    return length


# `create_upload` creates the part file BEFORE the row is inserted, so a value the row cannot hold
# would fail the insert after the file exists and orphan it where `reap_expired` (which walks rows)
# never looks. Everything the column would refuse is therefore refused here, before the store.
_TEXT_COLUMNS = {"filename": "filename", "filetype": "mime"}


def _required_text(pairs: Mapping[str, bytes], key: str) -> str:
    raw = pairs.get(key)
    if not raw:
        raise MetadataRefused(f"Upload-Metadata must carry {key!r} with a value")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise MetadataRefused(f"Upload-Metadata {key!r} is not UTF-8") from None
    if "\x00" in text:
        raise MetadataRefused(f"Upload-Metadata {key!r} contains a NUL character")
    limit = _column_length(_TEXT_COLUMNS[key])
    if len(text) > limit:
        raise MetadataRefused(f"Upload-Metadata {key!r} is longer than {limit} characters")
    return text


def _last_modified(raw: bytes) -> int:
    # Canonical digits only: `0` is what the store reads as "absent", and `0001` or `+5` could
    # not be echoed back as they were sent.
    text = raw.decode("ascii", errors="replace")
    if not _CANONICAL_MS.fullmatch(text) or int(text) > MAX_LAST_MODIFIED_MS:
        raise MetadataRefused(
            "Upload-Metadata 'lastModified' must be milliseconds since the Unix epoch, written "
            f"as digits without a leading zero, from 1 to {MAX_LAST_MODIFIED_MS}"
        )
    return int(text)


def creation_metadata(header: str | None) -> CreationMetadata:
    """What a creation request's `Upload-Metadata` says, or MetadataRefused naming the key."""
    try:
        pairs = parse_upload_metadata(header) if header is not None else {}
    except ValueError as exc:
        raise MetadataRefused(f"Upload-Metadata is {exc}") from None
    unknown = sorted(set(pairs) - _ACCEPTED_KEYS)
    if unknown:
        raise MetadataRefused(
            f"Upload-Metadata key {unknown[0]!r} is not accepted; the keys are filename, "
            "filetype and lastModified"
        )
    raw_mtime = pairs.get("lastModified")
    return CreationMetadata(
        filename=_required_text(pairs, "filename"),
        mime=_required_text(pairs, "filetype"),
        last_modified=None if raw_mtime is None else _last_modified(raw_mtime),
    )


def echoed_metadata(upload: Upload) -> str:
    """`Upload-Metadata` for HEAD: exactly the keys creation accepts, read back from the row."""
    pairs = {"filename": upload.filename.encode(), "filetype": upload.mime.encode()}
    if upload.client_mtime is not None:
        # Integer arithmetic on the stored microseconds; `int(timestamp() * 1000)` truncates a
        # float and answers one millisecond short for some sent values (1001 is one).
        millis = (upload.client_mtime - EPOCH) // timedelta(milliseconds=1)
        pairs["lastModified"] = str(millis).encode()
    return encode_upload_metadata(pairs)


def expires_header(expires_at: datetime | None) -> dict[str, str]:
    """`Upload-Expires` as an RFC 9110 IMF-fixdate — or nothing for a row that never expires (a
    NULL `expires_at`, which the reaper keeps), because the header promises a deadline."""
    if expires_at is None:
        return {}
    return {"Upload-Expires": format_datetime(expires_at.astimezone(UTC), usegmt=True)}
