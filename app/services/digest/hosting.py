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
    decides to upload, which links reach which card, and what `digest_hosted_media` records are all
    still exercised. `DropboxHost` satisfies this structurally — there is no base class to forget
    to inherit.
    """

    async def upload(self, local_path: Path, filename: str) -> tuple[str, str, int]: ...

    async def delete(self, dropbox_path: str) -> None: ...


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

    async def upload(self, local_path: Path, filename: str) -> tuple[str, str, int]:
        """Upload one file and return `(dropbox_path, shared_link, bytes)`."""
        size = await to_thread.run_sync(_size_sync, local_path)
        dropbox_path = self.dropbox_path_for(filename)
        if size > CHUNK_THRESHOLD_BYTES:
            await self._upload_session(local_path, dropbox_path, size)
        else:
            await self._upload_single(local_path, dropbox_path)
        return dropbox_path, await self.shared_link(dropbox_path), size

    async def delete(self, dropbox_path: str) -> None:
        response = await self._client.post(
            DELETE_URL,
            headers={**self._auth(), "Content-Type": "application/json"},
            json={"path": dropbox_path},
            timeout=LINK_TIMEOUT_S,
        )
        await self._beat()
        # 409 is `path_lookup/not_found`: the file is already gone, which is the state the sweep
        # wants. Treating it as an error would leave `swept_at` unstamped and re-attempt it every
        # hour forever.
        if response.status_code not in (200, 409):
            raise DropboxError(f"delete failed with HTTP {response.status_code}")


def video_source_path(uid: str) -> Path | None:
    resolved = derived_tier_path(uid, "web")
    return None if resolved is None else resolved[0]


def image_full_source_path(uid: str) -> Path | None:
    resolved = derived_tier_path(uid, "mobile")
    return None if resolved is None else resolved[0]


async def resolve_links(
    candidates: list[Candidate],
    *,
    host: MediaHost | None,
    existing: dict[int, str],
    max_videos_per_memo: int,
    link_images_full: bool,
) -> MediaLinks:
    """Which attachments this render can link to, uploading what is missing when `host` is set.

    `host is None` is the inline-only path and covers three cases that must behave identically:
    hosting switched off in the settings, the `DROPBOX_*` env keys unset, and a token refresh that
    failed. In all three the run continues with whatever `existing` already knows — never nothing,
    because a link recorded by an earlier run is still live and still points at the right file.

    A test-send and a preview reach this with `host=None` BY CONTRACT, not by configuration: a
    probe must never create a Dropbox file the registry does not know about, or one the retention
    sweep would later have to guess at (A6).
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
                path = image_full_source_path(ref.uid)
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

    for kind, group in ((VIDEO, videos), (IMAGE_FULL, images)):
        for attachment_id, filename, path in group:
            if path not in present or attachment_id in links.by_attachment:
                continue
            try:
                dropbox_path, link, size = await host.upload(path, filename)
            except (DropboxError, httpx.HTTPError):
                # One failed upload degrades ONE card, never the run: the card falls back to the
                # "not hosted" chip and the rest of the mail goes out. The exception carries no
                # token and the log line names only the attachment.
                logger.warning("digest.hosting_upload_failed", attachment_id=attachment_id)
                continue
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
