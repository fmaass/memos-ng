"""Browser-brokered native device login: the handoff code between the web login and the app.

The device never speaks OIDC. It opens the ordinary web login in an authentication session; when
Authelia returns, the BFF mints a short-lived one-shot code and redirects the session to an HTTPS
callback only the entitled app receives. The app then exchanges that code — together with the PKCE
verifier it generated before the login started — for a PAT.

Two independent things have to be true for a code to become a token: the caller must hold the code
(which travelled through a URL) AND the verifier (which never left the device). That is why the
challenge is stored per code: an intercepted code alone is inert.
"""

import base64
import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import NativeAuthCode, User

# RFC 7636 §4.1: a code_verifier is 43-128 unreserved characters, so its base64url-encoded S256
# challenge is 43 characters. The range is kept wide (rather than pinned at 43) because that is the
# spec's own bound for the field, and the alphabet is the un-padded base64url set.
_CODE_CHALLENGE_RE = re.compile(r"\A[A-Za-z0-9_-]{43,128}\Z")

DEVICE_NAME_MAX = 64

# What a handoff code may be redeemed for. The login lane mints a device PAT; the management lane
# mints a step-up grant for `/api/pats/*` (MEMNG-138 option 2). One code is redeemable for exactly
# one of the two — see `consume_code`, where the purpose rides in the atomic consume's WHERE.
PURPOSE_LOGIN = "login"
PURPOSE_MANAGE = "manage"
PURPOSES = (PURPOSE_LOGIN, PURPOSE_MANAGE)

# Rows are kept for an hour past expiry so an app that comes back late still gets `code_expired`
# rather than the indistinguishable `code_invalid`. Both are terminal for the client — the window
# only buys a better diagnostic.
_SWEEP_GRACE = timedelta(hours=1)


class NativeCodeRejected(Exception):
    """A handoff code that cannot be exchanged, carrying the stable client-facing reason.

    An exception rather than a return value so no caller can forget to branch on it: every path
    out of `consume_code` that is not a usable claim leaves through here.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NativeCodeClaim:
    """What a successful consume hands back: everything the token mint needs, nothing else."""

    __slots__ = ("code_challenge", "device_name", "user_id")

    def __init__(self, user_id: int, code_challenge: str, device_name: str) -> None:
        self.user_id = user_id
        self.code_challenge = code_challenge
        self.device_name = device_name


def valid_code_challenge(value: str | None) -> bool:
    """True iff `value` is a well-formed S256 PKCE challenge. Pure."""
    return bool(value) and _CODE_CHALLENGE_RE.match(value or "") is not None


def valid_device_name(value: str | None) -> bool:
    """True iff `value` is a usable device display name — it becomes the PAT's name, and a PAT
    with no name is unattributable in the token list. Pure."""
    return bool(value) and len(value or "") <= DEVICE_NAME_MAX


def s256_challenge(verifier: str) -> str:
    """The S256 PKCE challenge for `verifier` (un-padded base64url of its sha256). Pure.

    S256 is the only method this flow supports: the server always computes it, and no request
    parameter can ask for `plain` — so a downgrade is not something a client can negotiate.
    """
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


async def mint_code(
    db: AsyncSession,
    user: User,
    code_challenge: str,
    device_name: str,
    purpose: str = PURPOSE_LOGIN,
) -> str:
    """Insert a fresh one-shot code for `user`; return the plaintext, which exists only here.

    `purpose` is stamped on the row because the transaction cookie that carried it through the
    Authelia round trip is gone by the time the code is redeemed — see `NativeAuthCode.purpose`.

    Sweeps rows an hour past expiry on the way through — the volume is a handful of rows per device
    login, which does not justify a worker job, and doing it at mint time means the table is
    cleaned by exactly the traffic that grows it.
    """
    now = datetime.now(UTC)
    await db.execute(
        delete(NativeAuthCode).where(NativeAuthCode.expires_at < now - _SWEEP_GRACE)
    )
    code = secrets.token_urlsafe(32)
    db.add(
        NativeAuthCode(
            code_hash=hash_code(code),
            user_id=user.id,
            code_challenge=code_challenge,
            device_name=device_name,
            purpose=purpose,
            expires_at=now + timedelta(seconds=settings.NATIVE_CODE_TTL_S),
        )
    )
    await db.flush()
    return code


async def consume_code(db: AsyncSession, code: str, *, purpose: str) -> NativeCodeClaim:
    """Burn `code` and return its claim, or raise `NativeCodeRejected`.

    The burn is ONE statement. Reading the row, deciding it is usable and then marking it consumed
    would leave a window in which two concurrent exchanges both read an unconsumed row and both
    mint a token; a conditional UPDATE ... RETURNING makes the database the arbiter — the second
    writer blocks on the row lock, re-evaluates the WHERE against the committed version, and
    matches nothing. The caller must COMMIT: the consumption has to outlive the request even when
    what follows it fails, or a wrong verifier would be retryable.

    `purpose` has no default and rides in that same WHERE: a caller has to say what it is redeeming
    for, and a code minted for the other lane matches nothing — so it is refused AND left unburned,
    which is what keeps a cross-redemption attempt from costing the owner their real code. The
    refusal reason deliberately does not distinguish it from an unknown code (see
    `_rejection_reason`): a client acts on neither differently, and saying so would confirm that a
    code exists for a lane the caller is not entitled to.
    """
    now = datetime.now(UTC)
    consumed = (
        await db.execute(
            update(NativeAuthCode)
            .where(
                NativeAuthCode.code_hash == hash_code(code),
                NativeAuthCode.purpose == purpose,
                NativeAuthCode.consumed_at.is_(None),
                NativeAuthCode.expires_at > now,
            )
            .values(consumed_at=now)
            .returning(
                NativeAuthCode.user_id,
                NativeAuthCode.code_challenge,
                NativeAuthCode.device_name,
            )
        )
    ).one_or_none()
    if consumed is not None:
        return NativeCodeClaim(consumed[0], consumed[1], consumed[2])
    raise NativeCodeRejected(await _rejection_reason(db, code))


async def _rejection_reason(db: AsyncSession, code: str) -> str:
    """Why the conditional consume matched nothing — read back, never inferred.

    A swept code is indistinguishable from one that never existed, and deliberately so: both are
    `code_invalid` and both are terminal, and the alternative (keeping rows forever to tell them
    apart) is an unbounded table for a distinction no client acts on.
    """
    row = (
        await db.execute(
            select(NativeAuthCode.consumed_at, NativeAuthCode.expires_at).where(
                NativeAuthCode.code_hash == hash_code(code)
            )
        )
    ).one_or_none()
    if row is None:
        return "code_invalid"
    consumed_at, expires_at = row
    if consumed_at is not None:
        return "code_consumed"
    if expires_at <= datetime.now(UTC):
        return "code_expired"
    # The row is live and unconsumed, so either it was minted for the OTHER lane (the purpose in
    # the consume's WHERE did not match) or the UPDATE lost a race that has since been rolled back.
    # Both are reported as `code_invalid`, indistinguishable from a code that never existed: a
    # client acts on none of them differently, and a wrong-lane code that announced itself would
    # confirm the existence of a credential the caller is not entitled to redeem.
    return "code_invalid"


def verifier_matches(verifier: str, code_challenge: str) -> bool:
    """Constant-time check that `verifier` is the one the code was minted for. Pure."""
    return secrets.compare_digest(s256_challenge(verifier), code_challenge)


def aasa_document(team_id: str, bundle_id: str, keys: list[str]) -> dict:
    """The apple-app-site-association body for this deployment. Pure.

    Only the requested sections are emitted: a domain that claims capabilities it does not use
    makes Apple's CDN cache a promise the app cannot keep.
    """
    app_id = f"{team_id}.{bundle_id}"
    document: dict = {}
    if "webcredentials" in keys:
        document["webcredentials"] = {"apps": [app_id]}
    if "applinks" in keys:
        document["applinks"] = {
            "details": [
                {
                    "appIDs": [app_id],
                    # Two URL families belong to the app: the login handoff callback, and the memo
                    # permalink — the canonical web URL for a memo, which the app opens directly
                    # rather than bouncing the reader through the browser. Both are the same app,
                    # so they are components of ONE details entry: Apple matches the components of
                    # an entry in order, and a second entry with identical appIDs would only
                    # restate the claim. A device without the app installed follows the same URL
                    # to the SPA route, which is why the permalink stays a plain HTTPS link.
                    "components": [
                        {"/": "/ios/auth/callback*"},
                        {"/": "/m/*"},
                    ],
                }
            ]
        }
    return document
