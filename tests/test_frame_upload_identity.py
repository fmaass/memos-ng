"""upload_to_album returns aura-frames' album photo id and posts to the album it is given (plan
PLAN_2026-11-06_favorites-to-aura-frame.md, check R2-3). aura-frames re-renders every upload, so
the id it answers with — for a new photo and for a duplicate alike — is the only identity memos can
later join to `album_photos.id`."""

import json

import httpx
import pytest
from app.config import settings
from app.services import frame_share as fs


def _album(monkeypatch, *, status: int, body: object | None, captured: list):
    def handler(request):
        captured.append(request)
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, content=json.dumps(body).encode())

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(fs.httpx, "AsyncClient", factory)
    monkeypatch.setattr(settings, "FRAME_API_URL", "http://aura-frames.test")
    monkeypatch.setattr(settings, "FRAME_API_TOKEN", "tok")
    monkeypatch.setattr(settings, "FRAME_ALBUM_ID", 5)


@pytest.mark.parametrize(
    ("status", "body"),
    [(201, {"status": "created", "id": 4242}), (200, {"status": "duplicate", "id": 4242})],
)
async def test_the_album_photo_id_is_returned_for_created_and_duplicate(monkeypatch, status, body):
    captured: list = []
    _album(monkeypatch, status=status, body=body, captured=captured)
    assert await fs.upload_to_album("a.jpg", b"\xff\xd8\xff\xd9") == 4242
    assert captured[0].url.path == "/api/albums/5/photos"  # default: the curated album


async def test_an_explicit_album_id_is_the_destination(monkeypatch):
    captured: list = []
    _album(monkeypatch, status=201, body={"status": "created", "id": 7}, captured=captured)
    assert await fs.upload_to_album("a.jpg", b"\xff\xd8\xff\xd9", album_id=13) == 7
    assert captured[0].url.path == "/api/albums/13/photos"


@pytest.mark.parametrize(
    "body",
    [
        None,
        {"status": "created"},
        {"status": "created", "id": "7"},
        {"status": "created", "id": True},
    ],
)
async def test_a_body_without_an_integer_id_is_still_success_without_identity(monkeypatch, body):
    # The upload succeeded (2xx); a body memos cannot read must not turn it into a failure.
    captured: list = []
    _album(monkeypatch, status=201, body=body, captured=captured)
    assert await fs.upload_to_album("a.jpg", b"\xff\xd8\xff\xd9") is None


async def test_a_non_json_body_is_success_without_identity(monkeypatch):
    captured: list = []

    def handler(request):
        captured.append(request)
        return httpx.Response(201, content=b"<html>ok</html>")

    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient
    monkeypatch.setattr(
        fs.httpx, "AsyncClient", lambda *a, **kw: orig(*a, **{**kw, "transport": transport})
    )
    monkeypatch.setattr(settings, "FRAME_API_URL", "http://aura-frames.test")
    monkeypatch.setattr(settings, "FRAME_API_TOKEN", "tok")
    monkeypatch.setattr(settings, "FRAME_ALBUM_ID", 5)
    assert await fs.upload_to_album("a.jpg", b"\xff\xd8\xff\xd9") is None


# --- D1: the source identity header (plan Phase D) -----------------------------------------------

SOURCE_REF_SHAPE = r"^[\x20-\x7E]{1,200}$"  # what aura-frames accepts; anything else is a 422


async def test_a_source_ref_is_sent_as_the_x_source_ref_header(monkeypatch):
    captured: list = []
    _album(monkeypatch, status=201, body={"status": "created", "id": 1}, captured=captured)
    await fs.upload_to_album("a.jpg", b"\xff\xd8\xff\xd9", source_ref="memos-attachment:42")
    assert captured[0].headers["X-Source-Ref"] == "memos-attachment:42"


async def test_no_source_ref_sends_no_header(monkeypatch):
    captured: list = []
    _album(monkeypatch, status=201, body={"status": "created", "id": 1}, captured=captured)
    await fs.upload_to_album("a.jpg", b"\xff\xd8\xff\xd9")
    assert "X-Source-Ref" not in captured[0].headers


@pytest.mark.parametrize(
    ("att_id", "uid"),
    [(1, "plain"), (2**63 - 1, "é" * 64), (7, "x" * 64), (8, "tab\there"), (9, "")],
    ids=["ascii-uid", "max-id-non-ascii-uid", "long-uid", "control-char-uid", "empty-uid"],
)
def test_the_source_ref_is_printable_ascii_and_short_whatever_the_uid(att_id, uid):
    # Built from the numeric id, ASCII by construction: an imported uid is not guaranteed ASCII.
    import re

    from app.models import Attachment

    ref = fs.frame_source_ref(Attachment(id=att_id, uid=uid))
    assert ref == f"memos-attachment:{att_id}"
    assert re.fullmatch(SOURCE_REF_SHAPE, ref)
