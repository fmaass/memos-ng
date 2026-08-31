"""Dropbox media hosting for the digest, ported from the satellite's `app/dropbox.py`.

The recipients are outside the household and hold no memos-ng account, and the owner's decision
on design 3285 is that the mail must never point them at the memos-ng host — so a video rides as
a poster, a play badge and a link to a copy on Dropbox. Two things change in the port:

* `httpx` instead of `requests`, because httpx is already in the graph and is async-native;
* every file read is offloaded (`to_thread.run_sync`) rather than blocking the loop that has to
  keep the worker's heartbeat beating for the whole run.

The per-operation timeouts are the satellite's, and they differ by operation for a reason: a token
refresh or a link call is a small JSON round trip (15 s), a single-shot upload carries up to 140 MB
(120 s), and one upload-session operation carries a 48 MB chunk over a household uplink (600 s).
That last number is why the run's liveness signal is a heartbeat and not elapsed time, and why
`on_operation` is stamped after EVERY session call rather than once per file.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx
import structlog
from anyio import to_thread

from app.services.asset_paths import derived_tier_path
from app.services.digest.select import Candidate

logger = structlog.get_logger(__name__)

TOKEN_URL = "https://api.dropbox.com/oauth2/token"
UPLOAD_URL = "https://content.dropboxapi.com/2/files/upload"
UPLOAD_SESSION_START_URL = "https://content.dropboxapi.com/2/files/upload_session/start"
UPLOAD_SESSION_APPEND_URL = "https://content.dropboxapi.com/2/files/upload_session/append_v2"
UPLOAD_SESSION_FINISH_URL = "https://content.dropboxapi.com/2/files/upload_session/finish"
SHARED_LINK_URL = "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings"
LIST_SHARED_LINKS_URL = "https://api.dropboxapi.com/2/sharing/list_shared_links"
DELETE_URL = "https://api.dropboxapi.com/2/files/delete_v2"
GET_METADATA_URL = "https://api.dropboxapi.com/2/files/get_metadata"
ACCOUNT_URL = "https://api.dropboxapi.com/2/users/get_current_account"

# Dropbox refuses a single-shot /2/files/upload above 150 MB. Strictly above this goes through an
# upload session; at or below it the one-request path is used unchanged (satellite parity).
CHUNK_THRESHOLD_BYTES = 140 * 1024 * 1024
CHUNK_SIZE_BYTES = 48 * 1024 * 1024

REFRESH_TIMEOUT_S = 15.0
UPLOAD_TIMEOUT_S = 120.0
SESSION_TIMEOUT_S = 600.0
LINK_TIMEOUT_S = 15.0

VIDEO = "video"
IMAGE_FULL = "image_full"

# Awaited after every Dropbox HTTP operation. A run's liveness stamp rides on it — see the module
# docstring and `runner.heartbeat`.
Operation = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class HostedUpload:
    """One file a REAL run put on Dropbox, and what `digest_hosted_media` must record for it."""

    attachment_id: int
    kind: str
    dropbox_path: str
    shared_link: str
    bytes: int


@dataclass
class MediaLinks:
    """Which attachments have a shared link for this render, and which ones this run uploaded.

    The two are not the same set: a test-send resolves links from `digest_hosted_media` rows that
    already exist and uploads nothing, so `uploaded` is empty while `by_attachment` may be full.
    """

    by_attachment: dict[int, str] = field(default_factory=dict)
    uploaded: list[HostedUpload] = field(default_factory=list)
    # Videos whose transcoded mp4 is not on disk yet: the card shows "video still processing"
    # rather than a link, exactly as the satellite does.
    processing: set[int] = field(default_factory=set)


class DropboxError(RuntimeError):
    """Any Dropbox call that did not succeed. Carries no token and no response body."""


class MediaHost(Protocol):
    """What the pipeline needs from a media host, and the whole seam a fake replaces.

    A fake host substitutes the DEPENDENCY (Dropbox), never the unit under test: which files a run
    decides to upload, which links reach which card, and what the hosted-media ledger records are
    all still exercised. `DropboxHost` satisfies this structurally — there is no base class to
    forget to inherit.

    `dropbox_path_for` is part of the protocol because the PATH must be decidable — and therefore
    committable to the ledger — before any byte is sent. `upload` is handed that path rather than
    computing one of its own, so the row that is committed first and the object that is written
    second can never name two different places.
    """

    def dropbox_path_for(self, filename: str) -> str: ...

    async def upload(self, local_path: Path, dropbox_path: str) -> tuple[str, int]: ...

    async def delete(self, dropbox_path: str) -> None: ...

    # The reconciliation probe: a `pending` ledger row is judged by asking the remote whether its
    # object is actually there, which is the only authority on a crash that happened mid-upload.
    async def remote_exists(self, dropbox_path: str) -> bool: ...

    async def shared_link(self, dropbox_path: str) -> str: ...

    # Part of the protocol so that CLOSING is not optional. A host that owns a connection pool
    # (`DropboxHost` owns an `httpx.AsyncClient`) leaks it otherwise, once per run, in a process
    # that runs for weeks — and a caller cannot close what the type does not promise.
    async def aclose(self) -> None: ...


class HostedLedger(Protocol):
    """The two ledger writes an upload is wrapped in, as the hosting layer needs them.

    A protocol rather than a session: every call here COMMITS in its own short-lived transaction,
    because the whole point is that no transaction is open while bytes are on the wire. The
    implementation lives in `runner`, which is the only module that writes to the database.
    """

    async def claim(self, *, attachment_id: int, kind: str, dropbox_path: str) -> int:
        """Commit the INTENTION to write `dropbox_path`, returning the ledger row id."""
        ...

    async def mark_uploaded(self, ledger_id: int, *, shared_link: str, size: int) -> None:
        """Record that the upload this row claimed has succeeded."""
        ...


def _size_sync(path: Path) -> int:
    return path.stat().st_size


def _read_all_sync(path: Path) -> bytes:
    return path.read_bytes()


def _read_chunk_sync(path: Path, offset: int, size: int) -> bytes:
    """One chunk, by re-opening and seeking.

    A file handle held open across `await`s would have to survive every hop of an upload session;
    re-opening costs one extra syscall per 48 MB chunk (five for the largest file the satellite
    ever sent) and keeps the whole read inside one offloaded call with no shared mutable state.
    """
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(size)


def _existing_sync(paths: list[Path]) -> set[Path]:
    return {p for p in paths if p.exists()}


# The 409 error families that mean "there is no object at that path", as Dropbox spells them in
# `error_summary`: `path/not_found/…` from `/2/files/get_metadata`, `path_lookup/not_found/…` from
# `/2/files/delete_v2`. Every OTHER 409 — `path/restricted_content`, `too_many_write_operations`,
# a lock, an unexpected new tag — is a different condition entirely and must not be read as
# absence: see `_is_not_found`.
NOT_FOUND_FAMILIES = ("path", "path_lookup")
NOT_FOUND_TAG = "not_found"


def _is_not_found(response: httpx.Response) -> bool:
    """Whether a 409 says "no object at that path" — and nothing looser.

    Dropbox answers 409 for EVERY route-level error, not only for a missing path: restricted
    content, a write-lock conflict, rate limiting and any tag it adds in future all arrive with the
    same status. Reading the status alone as "absent" therefore turns an unrelated refusal into a
    claim that a file is gone — which stamps a ledger row `swept` while its object is still on
    Dropbox, unfindable for ever after. The TAG is the answer, so it is what is read; an
    unparseable or unrecognised body is "unknown", never "absent".
    """
    try:
        summary = response.json().get("error_summary") or ""
    except ValueError:  # a 409 whose body is not JSON tells us nothing
        return False
    parts = [part for part in str(summary).split("/") if part]
    return bool(parts) and parts[0] in NOT_FOUND_FAMILIES and NOT_FOUND_TAG in parts


def raw_link(url: str) -> str:
    """Dropbox's share URL rewritten to serve the FILE rather than its preview page.

    `?dl=0` is the preview; `?raw=1` is the bytes, which is what an `<img>`/`<a>` in a mail needs.
    """
    return url.replace("dl=0", "raw=1")


class DropboxHost:
    """The Dropbox side of a run: refresh a token once, upload, share, delete.

    The HTTP client is injected so tests drive the real request-building and response-handling
    code through `httpx.MockTransport` — the 409 → `list_shared_links` fallback, the chunk cursor
    arithmetic and the `dl=0` → `raw=1` rewrite are all exercised, with no network anywhere.
    """

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        refresh_token: str,
        base_folder: str,
        run_folder: str,
        client: httpx.AsyncClient,
        on_operation: Operation | None = None,
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._refresh_token = refresh_token
        self._base_folder = base_folder.rstrip("/")
        self._run_folder = run_folder
        self._client = client
        self._on_operation = on_operation
        self._token: str | None = None

    async def aclose(self) -> None:
        """Release the HTTP client. Idempotent, and safe on a host whose refresh never succeeded."""
        await self._client.aclose()

    async def _beat(self) -> None:
        if self._on_operation is not None:
            await self._on_operation()

    async def refresh(self) -> None:
        """Exchange the refresh token for an access token. Called once, before any upload.

        Its failure is what "hosting unconfigured or token refresh failing" means: the caller
        catches it, logs, and the run continues inline-only rather than failing.
        """
        response = await self._client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._app_key,
                "client_secret": self._app_secret,
            },
            timeout=REFRESH_TIMEOUT_S,
        )
        await self._beat()
        if response.status_code != 200:
            raise DropboxError(f"token refresh failed with HTTP {response.status_code}")
        self._token = response.json()["access_token"]

    def _auth(self) -> dict[str, str]:
        if self._token is None:
            raise DropboxError("no access token — refresh() was not called or it failed")
        return {"Authorization": f"Bearer {self._token}"}

    def _octet_headers(self, api_arg: dict) -> dict[str, str]:
        return {
            **self._auth(),
            "Content-Type": "application/octet-stream",
            "Dropbox-API-Arg": json.dumps(api_arg),
        }

    def dropbox_path_for(self, filename: str) -> str:
        return f"{self._base_folder}/{self._run_folder}/{filename}"

    async def _upload_single(self, local_path: Path, dropbox_path: str) -> None:
        data = await to_thread.run_sync(_read_all_sync, local_path)
        response = await self._client.post(
            UPLOAD_URL,
            headers=self._octet_headers(
                {"path": dropbox_path, "mode": "overwrite", "autorename": False}
            ),
            content=data,
            timeout=UPLOAD_TIMEOUT_S,
        )
        await self._beat()
        if response.status_code != 200:
            raise DropboxError(f"upload failed with HTTP {response.status_code}")

    async def _upload_session(self, local_path: Path, dropbox_path: str, size: int) -> None:
        """Chunked upload, one heartbeat per HTTP operation.

        Every step raises on a non-2xx, so a failed start or append leaves the later calls
        unreachable — a half-written session is abandoned rather than committed, and Dropbox
        garbage-collects sessions that are never finished (satellite parity).
        """
        chunk = await to_thread.run_sync(_read_chunk_sync, local_path, 0, CHUNK_SIZE_BYTES)
        response = await self._client.post(
            UPLOAD_SESSION_START_URL,
            headers=self._octet_headers({"close": False}),
            content=chunk,
            timeout=SESSION_TIMEOUT_S,
        )
        await self._beat()
        if response.status_code != 200:
            raise DropboxError(f"upload session start failed with HTTP {response.status_code}")
        session_id = response.json()["session_id"]
        offset = len(chunk)
        while offset < size:
            chunk = await to_thread.run_sync(
                _read_chunk_sync, local_path, offset, CHUNK_SIZE_BYTES
            )
            if not chunk:
                break
            response = await self._client.post(
                UPLOAD_SESSION_APPEND_URL,
                headers=self._octet_headers(
                    {
                        "cursor": {"session_id": session_id, "offset": offset},
                        "close": False,
                    }
                ),
                content=chunk,
                timeout=SESSION_TIMEOUT_S,
            )
            await self._beat()
            if response.status_code != 200:
                raise DropboxError(
                    f"upload session append failed with HTTP {response.status_code}"
                )
            offset += len(chunk)
        response = await self._client.post(
            UPLOAD_SESSION_FINISH_URL,
            headers=self._octet_headers(
                {
                    "cursor": {"session_id": session_id, "offset": offset},
                    "commit": {
                        "path": dropbox_path,
                        "mode": "overwrite",
                        "autorename": False,
                    },
                }
            ),
            timeout=SESSION_TIMEOUT_S,
        )
        await self._beat()
        if response.status_code != 200:
            raise DropboxError(f"upload session finish failed with HTTP {response.status_code}")

    async def shared_link(self, dropbox_path: str) -> str:
        """The file's shared link, creating it if it has none.

        409 is Dropbox's answer for "a shared link for this path already exists", which is the
        ordinary case for a re-upload; the fallback reads the existing one rather than treating a
        successful state as an error (satellite parity, bought by a real failure).
        """
        response = await self._client.post(
            SHARED_LINK_URL,
            headers={**self._auth(), "Content-Type": "application/json"},
            json={"path": dropbox_path},
            timeout=LINK_TIMEOUT_S,
        )
        await self._beat()
        if response.status_code == 200:
            return raw_link(response.json()["url"])
        if response.status_code == 409:
            listed = await self._client.post(
                LIST_SHARED_LINKS_URL,
                headers={**self._auth(), "Content-Type": "application/json"},
                json={"path": dropbox_path, "direct_only": True},
                timeout=LINK_TIMEOUT_S,
            )
            await self._beat()
            if listed.status_code == 200:
                links = listed.json().get("links", [])
                if links:
                    return raw_link(links[0]["url"])
        raise DropboxError(f"shared link failed with HTTP {response.status_code}")

    async def upload(self, local_path: Path, dropbox_path: str) -> tuple[str, int]:
        """Upload one file to an ALREADY-DECIDED path and return `(shared_link, bytes)`.

        The path is an argument rather than something derived here: the caller commits it to the
        ledger before this runs, and a path computed twice is a path that can differ once.
        """
        size = await to_thread.run_sync(_size_sync, local_path)
        if size > CHUNK_THRESHOLD_BYTES:
            await self._upload_session(local_path, dropbox_path, size)
        else:
            await self._upload_single(local_path, dropbox_path)
        return await self.shared_link(dropbox_path), size

    async def remote_exists(self, dropbox_path: str) -> bool:
        """Whether the remote object is there — the reconciliation probe for a `pending` row.

        A 409 tagged `path/not_found` is a real ANSWER ("it never landed") and not an error. Any
        OTHER 409 is a different condition — restricted content, a lock, a tag Dropbox adds later —
        and raises, because it says nothing about whether the object exists and a ledger row must
        never be resolved on a guess: the caller leaves the row exactly as it is and the next pass
        asks again.
        """
        response = await self._client.post(
            GET_METADATA_URL,
            headers={**self._auth(), "Content-Type": "application/json"},
            json={"path": dropbox_path},
            timeout=LINK_TIMEOUT_S,
        )
        await self._beat()
        if response.status_code == 200:
            return True
        if response.status_code == 409 and _is_not_found(response):
            return False
        raise DropboxError(f"metadata lookup failed with HTTP {response.status_code}")

    async def account(self) -> str:
        """The account these keys authenticate as, for the admin panel's hosting status line.

        Read-only and identifying only — an address or a display name, never a token and never a
        quota or a file listing. `/2/users/get_current_account` takes no arguments, so the request
        carries no body and no `Content-Type` (Dropbox refuses one on an argument-less RPC).
        """
        response = await self._client.post(
            ACCOUNT_URL, headers=self._auth(), timeout=LINK_TIMEOUT_S
        )
        await self._beat()
        if response.status_code != 200:
            raise DropboxError(f"account lookup failed with HTTP {response.status_code}")
        payload = response.json()
        name = payload.get("name") or {}
        return payload.get("email") or name.get("display_name") or ""

    async def delete(self, dropbox_path: str) -> None:
        response = await self._client.post(
            DELETE_URL,
            headers={**self._auth(), "Content-Type": "application/json"},
            json={"path": dropbox_path},
            timeout=LINK_TIMEOUT_S,
        )
        await self._beat()
        # A 409 tagged `path_lookup/not_found` means the file is already gone, which is the state
        # the sweep wants; treating that as an error would leave the row unstamped and re-attempt
        # it every hour for ever. Any other 409 is NOT that — a restricted or locked path answers
        # 409 too — and stamping `swept` on one would claim a live file had been deleted, leaving
        # it on Dropbox with nothing naming it. Those raise, and the sweep leaves the row for the
        # next pass.
        if response.status_code == 200:
            return
        if response.status_code == 409 and _is_not_found(response):
            return
        raise DropboxError(f"delete failed with HTTP {response.status_code}")


def video_source_path(uid: str) -> Path | None:
    resolved = derived_tier_path(uid, "web")
    return None if resolved is None else resolved[0]


def image_full_source_path(uid: str) -> Path | None:
    """The screen tier hosted as an image's full-size copy when nothing better is offered.

    The FALLBACK, since MEMNG-240: a run that can derive them hands in the print profile's
    renditions (`image_full_sources`), and this is what an image without one still gets — the same
    2048 px copy the digest hosted before, which is why a spent encode budget or a hot host costs
    quality and never a link.
    """
    resolved = derived_tier_path(uid, "mobile")
    return None if resolved is None else resolved[0]


async def current_account(
    *, app_key: str, app_secret: str, refresh_token: str
) -> str | None:
    """The Dropbox account the configured keys authenticate as, or None when it cannot be read.

    None on EVERY failure — unset keys, a refused refresh, a malformed payload, a timeout: this
    answers one line of an admin status panel, and a panel that 500s because Dropbox is slow is
    worse than a panel that says it does not know. The client is closed on every path, this one
    included, because the failure paths are exactly where a leaked connection pool accumulates.
    """
    if not (app_key and app_secret and refresh_token):
        return None
    client = httpx.AsyncClient()
    host = DropboxHost(
        app_key=app_key,
        app_secret=app_secret,
        refresh_token=refresh_token,
        base_folder="",
        run_folder="",
        client=client,
    )
    try:
        await host.refresh()
        label = await host.account()
    except Exception:
        logger.warning("digest.hosting_account_lookup_failed")
        return None
    finally:
        await client.aclose()
    return label or None


async def resolve_links(
    candidates: list[Candidate],
    *,
    host: MediaHost | None,
    ledger: HostedLedger | None = None,
    existing: dict[int, str],
    max_videos_per_memo: int,
    link_images_full: bool,
    image_full_sources: dict[int, Path] | None = None,
) -> MediaLinks:
    """Which attachments this render can link to, uploading what is missing when `host` is set.

    `host is None` is the inline-only path and covers three cases that must behave identically:
    hosting switched off in the settings, the `DROPBOX_*` env keys unset, and a token refresh that
    failed. In all three the run continues with whatever `existing` already knows — never nothing,
    because a link recorded by an earlier run is still live and still points at the right file.

    A test-send and a preview reach this with `host=None` BY CONTRACT, not by configuration: a
    probe must never create a Dropbox file the ledger does not know about, or one the retention
    sweep would later have to guess at.

    `image_full_sources` names the file to host as an image's full-size copy, per attachment: the
    print profile's rendition, prepared by the caller (`runner.print_grade_sources`) because only
    the caller has a database session and this module must not open one across a Dropbox call. An
    attachment with no entry falls back to `image_full_source_path` — the screen tier — which is
    what makes a spent encode budget or a hot host a quality difference and not a missing link.

    **Every upload is bracketed by the ledger, one file at a time.** The intention is committed
    first, the bytes go second, the success is committed third — so the only state a crash can
    leave is a `pending` row naming a path that either holds an object or does not, which is a
    question the reconciliation sweep can ask Dropbox. Accumulating the successes in memory and
    writing them once at the end (the shape this replaces) leaves an object no row names, and
    nothing can ever find it again.
    """
    links = MediaLinks(by_attachment=dict(existing))
    videos: list[tuple[int, str, Path]] = []
    images: list[tuple[int, str, Path]] = []
    for candidate in candidates:
        for ref in candidate.videos[:max_videos_per_memo]:
            path = video_source_path(ref.uid)
            if path is not None:
                videos.append((ref.attachment_id, f"{ref.uid}.mp4", path))
        if link_images_full:
            for ref in candidate.images:
                path = (image_full_sources or {}).get(
                    ref.attachment_id
                ) or image_full_source_path(ref.uid)
                if path is not None:
                    images.append((ref.attachment_id, f"{ref.uid}.jpg", path))

    present = await to_thread.run_sync(
        _existing_sync, [path for _, _, path in videos] + [path for _, _, path in images]
    )
    for attachment_id, _, path in videos:
        if path not in present:
            links.processing.add(attachment_id)

    if host is None:
        return links

    if ledger is None:
        raise ValueError("a host without a ledger would upload an object nothing owns")

    for kind, group in ((VIDEO, videos), (IMAGE_FULL, images)):
        for attachment_id, filename, path in group:
            if path not in present or attachment_id in links.by_attachment:
                continue
            dropbox_path = host.dropbox_path_for(filename)
            ledger_id = await ledger.claim(
                attachment_id=attachment_id, kind=kind, dropbox_path=dropbox_path
            )
            try:
                link, size = await host.upload(path, dropbox_path)
            except (DropboxError, httpx.HTTPError):
                # One failed upload degrades ONE card, never the run: the card falls back to the
                # "not hosted" chip and the rest of the mail goes out. The exception carries no
                # token and the log line names only the attachment. The ledger row stays `pending`
                # — the reconciliation sweep, not this code path, decides whether anything landed.
                logger.warning("digest.hosting_upload_failed", attachment_id=attachment_id)
                continue
            await ledger.mark_uploaded(ledger_id, shared_link=link, size=size)
            links.by_attachment[attachment_id] = link
            links.uploaded.append(
                HostedUpload(
                    attachment_id=attachment_id,
                    kind=kind,
                    dropbox_path=dropbox_path,
                    shared_link=link,
                    bytes=size,
                )
            )
    return links
