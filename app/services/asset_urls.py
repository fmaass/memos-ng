"""HMAC-signed media URLs — the DB-free byte path.

The (already-authorized) feed mints short-lived signed URLs for the derived image/video tiers; the
serve route verifies the signature with no DB query and no auth dependency, then streams the file.
This keeps the high-fan-out media path off the bounded DB pool (the QueuePool-exhaustion bug). It's
the S3-presigned-URL model adapted with zero new infrastructure.

Pure + unit-tested: `now` is injected so signing/verification are deterministic.
"""

import hashlib
import hmac

from app.config import settings

# Tiers that resolve from the uid alone (no DB row) — only these are signed/served DB-free.
DERIVED_TIERS = frozenset({"thumb", "mobile", "web", "poster"})


def _secret() -> bytes:
    # Default off SESSION_SECRET so prod needs no new env; a dedicated ASSET_URL_SECRET overrides.
    return (settings.ASSET_URL_SECRET or settings.SESSION_SECRET).encode()


def _prev_secret() -> bytes | None:
    return settings.ASSET_URL_SECRET_PREV.encode() if settings.ASSET_URL_SECRET_PREV else None


def _sig(uid: str, tier: str, exp: int, scope: str, secret: bytes) -> str:
    msg = f"{uid}\n{tier}\n{exp}\n{scope}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def bucketed_exp(now: float) -> int:
    """Round mint time DOWN to a bucket then add the TTL, so every URL minted for the same
    (uid,tier,scope) within one bucket window is byte-identical — the browser's immutable cache
    still hits. (A per-request fresh exp would change the URL every render and defeat caching.)"""
    bucket = settings.ASSET_URL_BUCKET
    return (int(now) // bucket) * bucket + settings.ASSET_URL_TTL


def sign_asset_url(uid: str, tier: str, scope: int | str, *, now: float) -> str:
    """A signed URL for a derived tier: /api/attachments/{uid}/{tier}?exp=&scope=&sig=."""
    exp = bucketed_exp(now)
    sig = _sig(uid, tier, exp, str(scope), _secret())
    return f"/api/attachments/{uid}/{tier}?exp={exp}&scope={scope}&sig={sig}"


def signed_tier_urls(uid: str, scope: int | str, *, now: float) -> dict[str, str]:
    """The four derived-tier URL fields, all signed for the viewer — shared by every serializer."""
    return {
        "thumbnailUrl": sign_asset_url(uid, "thumb", scope, now=now),
        "previewUrl": sign_asset_url(uid, "mobile", scope, now=now),
        "webUrl": sign_asset_url(uid, "web", scope, now=now),
        "posterUrl": sign_asset_url(uid, "poster", scope, now=now),
    }


def signed_cover_url(uid: str, mime: str, scope: int | str, *, now: float) -> str:
    """The signed picture that stands for one item on a cover: its poster when it is a video, else
    its thumb. A video is given a poster and never a thumb (app/services/poster.py), so a thumb URL
    for it names a file the worker never writes."""
    tier = "poster" if mime.startswith("video/") else "thumb"
    return sign_asset_url(uid, tier, scope, now=now)


def verify_asset_sig(
    uid: str, tier: str, exp: str | int | None, scope: str | None, sig: str | None, *, now: float
) -> bool:
    """True iff `sig` is a valid, unexpired signature for (uid, tier, exp, scope). Constant-time;
    accepts the current key or the previous key (rotation ring)."""
    if not sig or exp is None or scope is None:
        return False
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_i <= int(now):
        return False
    if hmac.compare_digest(_sig(uid, tier, exp_i, scope, _secret()), sig):
        return True
    prev = _prev_secret()
    return prev is not None and hmac.compare_digest(_sig(uid, tier, exp_i, scope, prev), sig)
