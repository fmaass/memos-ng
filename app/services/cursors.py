"""Opaque keyset cursors, minted and read back in exactly one place.

Three lists page by cursor — the memo feed (`app/services/memos.py`), the media library
(`app/routers/assets.py`) and the 0.29.1 compat feed's `pageToken` (`app/routers/compat.py`) —
and each used to decode its own token its own way. They share this module now so that a token
this server did not mint is refused identically wherever it arrives.
"""

import base64


class InvalidCursor(ValueError):
    """A cursor this server did not mint. Translated to 422 by the handler in `app.main`.

    A ValueError subclass so that a caller which already guards its own parse (`int(...)`,
    `datetime.fromisoformat(...)`) with `except ValueError` keeps working unchanged.
    """


def encode_cursor_payload(raw: str) -> str:
    """The one place a cursor is minted: base64url of the keyset payload, padding included."""
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor_payload(cursor: str) -> str:
    """The payload of a token we minted — `InvalidCursor` for anything else.

    `base64.urlsafe_b64decode` is permissive by design: characters outside the alphabet are
    DISCARDED rather than rejected, so junk like `M!Q==` decodes to `1`, i.e. a perfectly valid
    cursor pointing at row 1. Re-encoding the decoded bytes and demanding the original token back
    is what makes this strict — a token that does not round-trip could not have come from
    `encode_cursor_payload`, whatever base64 it superficially resembles. That also settles
    padding: our own tokens are always padded, so a stripped or doubled `=` is refused too.
    """
    if not cursor:
        raise InvalidCursor("cursor is empty")  # never minted; "" is a bad token, not page 1
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
    except ValueError:  # binascii.Error and UnicodeEncodeError are both ValueError subclasses
        raise InvalidCursor("cursor is not base64url") from None
    if base64.urlsafe_b64encode(raw).decode("ascii") != cursor:
        raise InvalidCursor("cursor is not canonical base64url")
    try:
        return raw.decode()
    except UnicodeDecodeError:
        raise InvalidCursor("cursor payload is not text") from None
