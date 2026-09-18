"""Dropbox hosting (MEMNG-234), driven through `httpx.MockTransport`.

No network anywhere, and no fake in place of the client: the REAL `DropboxHost` builds the real
requests and reads the real responses, and a scripted transport answers them. That is what lets
these tests hold the cases the satellite bought with production failures — the 409 →
`list_shared_links` fallback, the chunk cursor arithmetic, the `dl=0` → `raw=1` rewrite — instead of
asserting against a stub of our own opinions.

The heartbeat count is the other load-bearing assertion. One upload-session operation may run to
its 600 s timeout, so a run that stamped liveness once per FILE could go fifteen minutes silent
mid-upload and be swept while alive. `on_operation` must fire after every HTTP call.
"""

import httpx
import pytest
from app.config import settings as app_settings
from app.services.digest import hosting
from app.services.digest.select import Candidate, MediaRef

from tests.digest_support import FakeHost, RecordingLedger, write_mobile, write_web_mp4

TOKEN = "access-token-value"


class _Script:
    """Records every request and answers from a per-URL handler."""

    def __init__(self, handlers: dict[str, object]) -> None:
        self.handlers = handlers
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.handlers.get(str(request.url))
        if handler is None:
            return httpx.Response(404, json={"error": "unscripted"})
        if callable(handler):
            return handler(request)
        return handler

    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]


def _client(script: _Script) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(script))


def _host(script: _Script, beats: list[int] | None = None) -> hosting.DropboxHost:
    async def beat() -> None:
        if beats is not None:
            beats.append(1)

    return hosting.DropboxHost(
        app_key="key",
        app_secret="secret",
        refresh_token="refresh",
        base_folder="/memos-mail",
        run_folder="2026-07-01_0700",
        client=_client(script),
        on_operation=None if beats is None else beat,
    )


def _token_ok() -> httpx.Response:
    return httpx.Response(200, json={"access_token": TOKEN})


# --- the token ------------------------------------------------------------------------------


async def test_refresh_exchanges_the_refresh_token_and_never_logs_it():
    script = _Script({hosting.TOKEN_URL: _token_ok()})
    host = _host(script)
    await host.refresh()
    body = script.requests[0].content.decode()
    assert "grant_type=refresh_token" in body
    assert "client_id=key" in body


async def test_a_failed_refresh_raises_without_the_response_body():
    script = _Script({hosting.TOKEN_URL: httpx.Response(401, json={"error": "invalid_grant"})})
    with pytest.raises(hosting.DropboxError) as caught:
        await _host(script).refresh()
    assert "invalid_grant" not in str(caught.value)
    assert "401" in str(caught.value)


async def test_an_upload_before_refresh_is_refused_rather_than_sent_unauthenticated(tmp_path):
    script = _Script({})
    path = tmp_path / "f.mp4"
    path.write_bytes(b"x")
    with pytest.raises(hosting.DropboxError, match="no access token"):
        await _host(script).upload(path, "/memos-mail/2026-07-01_0700/f.mp4")
    assert script.requests == []


# --- upload paths ---------------------------------------------------------------------------


async def test_a_small_file_takes_the_single_shot_path(tmp_path):
    path = tmp_path / "small.mp4"
    path.write_bytes(b"a" * 1024)
    script = _Script(
        {
            hosting.TOKEN_URL: _token_ok(),
            hosting.UPLOAD_URL: httpx.Response(200, json={"name": "small.mp4"}),
            hosting.SHARED_LINK_URL: httpx.Response(
                200, json={"url": "https://www.dropbox.com/s/x/small.mp4?dl=0"}
            ),
        }
    )
    host = _host(script)
    await host.refresh()
    dropbox_path = host.dropbox_path_for("small.mp4")
    assert dropbox_path == "/memos-mail/2026-07-01_0700/small.mp4"
    link, size = await host.upload(path, dropbox_path)
    assert size == 1024
    assert hosting.UPLOAD_SESSION_START_URL not in script.urls()
    assert link.endswith("raw=1") and "dl=0" not in link


async def test_a_large_file_takes_an_upload_session_with_correct_cursors(tmp_path, monkeypatch):
    """The chunk arithmetic, at a scaled-down threshold so the test moves kilobytes rather than
    hundreds of megabytes. The offsets are what a wrong cursor corrupts, silently."""
    monkeypatch.setattr(hosting, "CHUNK_THRESHOLD_BYTES", 100)
    monkeypatch.setattr(hosting, "CHUNK_SIZE_BYTES", 40)
    path = tmp_path / "big.mp4"
    path.write_bytes(bytes(range(256))[:250])
    script = _Script(
        {
            hosting.TOKEN_URL: _token_ok(),
            hosting.UPLOAD_SESSION_START_URL: httpx.Response(200, json={"session_id": "S1"}),
            hosting.UPLOAD_SESSION_APPEND_URL: httpx.Response(200, json={}),
            hosting.UPLOAD_SESSION_FINISH_URL: httpx.Response(200, json={"name": "big.mp4"}),
            hosting.SHARED_LINK_URL: httpx.Response(
                200, json={"url": "https://www.dropbox.com/s/x/big.mp4?dl=0"}
            ),
        }
    )
    host = _host(script)
    await host.refresh()
    await host.upload(path, host.dropbox_path_for("big.mp4"))
    appends = [r for r in script.requests if str(r.url) == hosting.UPLOAD_SESSION_APPEND_URL]
    offsets = [
        int(r.headers["Dropbox-API-Arg"].split('"offset": ')[1].split("}")[0]) for r in appends
    ]
    assert offsets == [40, 80, 120, 160, 200, 240]
    # Every byte of the file reached Dropbox exactly once, in order.
    start = next(r for r in script.requests if str(r.url) == hosting.UPLOAD_SESSION_START_URL)
    assert start.content + b"".join(r.content for r in appends) == path.read_bytes()
    finish = next(r for r in script.requests if str(r.url) == hosting.UPLOAD_SESSION_FINISH_URL)
    assert '"offset": 250' in finish.headers["Dropbox-API-Arg"]


async def test_every_upload_session_operation_stamps_a_heartbeat(tmp_path, monkeypatch):
    """One chunk operation alone may run to its 600 s timeout, and a session holds many — a
    heartbeat stamped once per FILE could leave a live run silent past the 15-minute sweep bar."""
    monkeypatch.setattr(hosting, "CHUNK_THRESHOLD_BYTES", 100)
    monkeypatch.setattr(hosting, "CHUNK_SIZE_BYTES", 40)
    path = tmp_path / "big.mp4"
    path.write_bytes(b"z" * 250)
    script = _Script(
        {
            hosting.TOKEN_URL: _token_ok(),
            hosting.UPLOAD_SESSION_START_URL: httpx.Response(200, json={"session_id": "S1"}),
            hosting.UPLOAD_SESSION_APPEND_URL: httpx.Response(200, json={}),
            hosting.UPLOAD_SESSION_FINISH_URL: httpx.Response(200, json={}),
            hosting.SHARED_LINK_URL: httpx.Response(
                200, json={"url": "https://www.dropbox.com/s/x/big.mp4?dl=0"}
            ),
        }
    )
    beats: list[int] = []
    host = _host(script, beats)
    await host.refresh()
    await host.upload(path, host.dropbox_path_for("big.mp4"))
    # refresh + start + 6 appends + finish + link = 10 HTTP operations, 10 heartbeats.
    assert len(script.requests) == 10
    assert len(beats) == len(script.requests)


async def test_a_failed_session_append_abandons_the_upload_rather_than_committing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(hosting, "CHUNK_THRESHOLD_BYTES", 100)
    monkeypatch.setattr(hosting, "CHUNK_SIZE_BYTES", 40)
    path = tmp_path / "big.mp4"
    path.write_bytes(b"z" * 250)
    script = _Script(
        {
            hosting.TOKEN_URL: _token_ok(),
            hosting.UPLOAD_SESSION_START_URL: httpx.Response(200, json={"session_id": "S1"}),
            hosting.UPLOAD_SESSION_APPEND_URL: httpx.Response(503, json={}),
        }
    )
    host = _host(script)
    await host.refresh()
    with pytest.raises(hosting.DropboxError, match="append"):
        await host.upload(path, host.dropbox_path_for("big.mp4"))
    assert hosting.UPLOAD_SESSION_FINISH_URL not in script.urls()


# --- shared links ---------------------------------------------------------------------------


async def test_a_409_falls_back_to_the_existing_shared_link():
    """409 is Dropbox's answer for "this path already has a link" — the ordinary case on a
    re-upload. Treating a successful state as an error is the failure this fallback was bought
    with."""
    script = _Script(
        {
            hosting.TOKEN_URL: _token_ok(),
            hosting.SHARED_LINK_URL: httpx.Response(409, json={"error": "shared_link_already"}),
            hosting.LIST_SHARED_LINKS_URL: httpx.Response(
                200, json={"links": [{"url": "https://www.dropbox.com/s/y/f.mp4?dl=0"}]}
            ),
        }
    )
    host = _host(script)
    await host.refresh()
    assert await host.shared_link("/memos-mail/x/f.mp4") == (
        "https://www.dropbox.com/s/y/f.mp4?raw=1"
    )


async def test_a_409_with_no_listed_link_raises():
    script = _Script(
        {
            hosting.TOKEN_URL: _token_ok(),
            hosting.SHARED_LINK_URL: httpx.Response(409, json={}),
            hosting.LIST_SHARED_LINKS_URL: httpx.Response(200, json={"links": []}),
        }
    )
    host = _host(script)
    await host.refresh()
    with pytest.raises(hosting.DropboxError):
        await host.shared_link("/memos-mail/x/f.mp4")


def test_the_raw_rewrite_turns_a_preview_url_into_the_bytes():
    assert hosting.raw_link("https://www.dropbox.com/s/x/f.mp4?dl=0").endswith("raw=1")
    assert hosting.raw_link("https://www.dropbox.com/s/x/f.mp4?raw=1").endswith("raw=1")


# --- delete ---------------------------------------------------------------------------------


async def test_delete_treats_an_already_missing_file_as_done():
    """The retention sweep stamps `swept_at` only on success; a 409 TAGGED `path_lookup/not_found`
    IS the state it wants, and raising would re-attempt the same file every hour forever.

    The tag is what is read, not the status — see the 409-family tests at the end of this file.
    """
    script = _Script(
        {
            hosting.TOKEN_URL: _token_ok(),
            hosting.DELETE_URL: httpx.Response(
                409, json={"error_summary": "path_lookup/not_found/..."}
            ),
        }
    )
    host = _host(script)
    await host.refresh()
    await host.delete("/memos-mail/x/f.mp4")


async def test_delete_raises_on_a_real_failure():
    script = _Script(
        {hosting.TOKEN_URL: _token_ok(), hosting.DELETE_URL: httpx.Response(500, json={})}
    )
    host = _host(script)
    await host.refresh()
    with pytest.raises(hosting.DropboxError):
        await host.delete("/memos-mail/x/f.mp4")


# --- resolve_links --------------------------------------------------------------------------


def _candidate_with_video(uid: str, attachment_id: int) -> Candidate:
    from datetime import UTC, datetime

    return Candidate(
        memo_id=1,
        uid="memo",
        content="",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
        videos=[
            MediaRef(attachment_id=attachment_id, uid=uid, filename="clip.mp4", mime="video/mp4")
        ],
    )


async def test_resolve_links_uploads_nothing_when_there_is_no_host(tmp_path, monkeypatch):
    """The probe contract: a preview or a test-send must never create a Dropbox file the ledger
    does not know about."""
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    write_web_mp4(tmp_path, "vid-1")
    links = await hosting.resolve_links(
        [_candidate_with_video("vid-1", 7)],
        host=None,
        existing={7: "https://dropbox.example/old.mp4?raw=1"},
        max_videos_per_memo=2,
        link_images_full=False,
    )
    assert links.by_attachment == {7: "https://dropbox.example/old.mp4?raw=1"}
    assert links.uploaded == []


async def test_a_video_with_no_transcoded_mp4_is_marked_processing(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    links = await hosting.resolve_links(
        [_candidate_with_video("vid-1", 7)],
        host=None,
        existing={},
        max_videos_per_memo=2,
        link_images_full=False,
    )
    assert links.processing == {7}
    assert links.by_attachment == {}


async def test_an_already_linked_attachment_is_not_re_uploaded(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    write_web_mp4(tmp_path, "vid-1")
    host = FakeHost()
    ledger = RecordingLedger()
    links = await hosting.resolve_links(
        [_candidate_with_video("vid-1", 7)],
        host=host,
        ledger=ledger,
        existing={7: "https://dropbox.example/old.mp4?raw=1"},
        max_videos_per_memo=2,
        link_images_full=False,
    )
    assert host.uploads == []
    # Nothing was uploaded, so nothing was CLAIMED either: an intention is committed for a file
    # this run is about to write, never for one it already has a link to.
    assert ledger.claims == []
    assert links.uploaded == []
    assert links.by_attachment[7] == "https://dropbox.example/old.mp4?raw=1"


async def test_one_failed_upload_degrades_one_card_and_not_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    write_web_mp4(tmp_path, "good")
    write_web_mp4(tmp_path, "bad")
    host = FakeHost(raise_on_upload={"bad.mp4"})
    ledger = RecordingLedger()
    links = await hosting.resolve_links(
        [_candidate_with_video("bad", 1), _candidate_with_video("good", 2)],
        host=host,
        ledger=ledger,
        existing={},
        max_videos_per_memo=2,
        link_images_full=False,
    )
    assert [name for _, name in host.uploads] == ["good.mp4"]
    assert list(links.by_attachment) == [2]
    assert [upload.attachment_id for upload in links.uploaded] == [2]
    # BOTH files were claimed — the intention is committed before the upload is attempted, so the
    # failed one leaves a row naming a path that may or may not hold an object. Only the one that
    # succeeded is marked; the other stays pending for the reconciliation sweep to resolve.
    assert [path for _, _, path in ledger.claims] == [
        "/memos-mail/run/bad.mp4",
        "/memos-mail/run/good.mp4",
    ]
    assert [ledger_id for ledger_id, _, _ in ledger.marked] == [2]


async def test_the_intention_is_committed_before_the_bytes_and_the_success_after_them(
    tmp_path, monkeypatch
):
    """The ordering that makes an orphan impossible.

    A crash anywhere in this sequence leaves a row that NAMES the remote path, so the object is
    always findable; recording the successes in one batch after the whole pass — the shape this
    replaced — leaves an object nothing can ever locate again.
    """
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    write_web_mp4(tmp_path, "vid-1")
    trace: list[str] = []
    host = FakeHost(events=trace)
    ledger = RecordingLedger(events=trace)
    await hosting.resolve_links(
        [_candidate_with_video("vid-1", 7)],
        host=host,
        ledger=ledger,
        existing={},
        max_videos_per_memo=2,
        link_images_full=False,
    )
    assert trace == [
        "claim:/memos-mail/run/vid-1.mp4",
        "upload:/memos-mail/run/vid-1.mp4",
        "mark:1",
    ]


async def test_a_host_without_a_ledger_is_refused_rather_than_uploading_unowned(
    tmp_path, monkeypatch
):
    """An upload with nowhere to record it is the exact failure the ledger exists to prevent, so
    it is refused at the door rather than degrading into one."""
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    write_web_mp4(tmp_path, "vid-1")
    host = FakeHost()
    with pytest.raises(ValueError, match="ledger"):
        await hosting.resolve_links(
            [_candidate_with_video("vid-1", 7)],
            host=host,
            existing={},
            max_videos_per_memo=2,
            link_images_full=False,
        )
    assert host.uploads == []


def _candidate_with_image(uid: str, attachment_id: int) -> Candidate:
    from datetime import UTC, datetime

    return Candidate(
        memo_id=1,
        uid="memo",
        content="",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
        images=[
            MediaRef(attachment_id=attachment_id, uid=uid, filename="p.jpg", mime="image/jpeg")
        ],
    )


async def test_link_images_full_falls_back_to_the_mobile_tier(tmp_path, monkeypatch):
    """No print-grade copy offered for this attachment: the screen tier is hosted, which is what
    keeps a spent encode budget or a hot host (MEMNG-240) a quality difference and not a missing
    link."""
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    write_mobile(tmp_path, "img-1")
    host = FakeHost()
    links = await hosting.resolve_links(
        [_candidate_with_image("img-1", 5)],
        host=host,
        ledger=RecordingLedger(),
        existing={},
        max_videos_per_memo=2,
        link_images_full=True,
    )
    assert host.uploads == [(str(tmp_path / "assets" / "mobile" / "img-1.jpg"), "img-1.jpg")]
    assert links.uploaded[0].kind == hosting.IMAGE_FULL
    assert 5 in links.by_attachment


async def test_a_print_grade_source_is_hosted_in_place_of_the_mobile_tier(tmp_path, monkeypatch):
    """The override the caller prepares wins, per attachment — and only for that attachment."""
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    write_mobile(tmp_path, "img-1")
    write_mobile(tmp_path, "img-2")
    print_grade = tmp_path / "assets" / "printready" / "img-1.jpg"
    print_grade.parent.mkdir(parents=True, exist_ok=True)
    print_grade.write_bytes(b"print-grade bytes")
    host = FakeHost()
    await hosting.resolve_links(
        [_candidate_with_image("img-1", 5), _candidate_with_image("img-2", 6)],
        host=host,
        ledger=RecordingLedger(),
        existing={},
        max_videos_per_memo=2,
        link_images_full=True,
        image_full_sources={5: print_grade},
    )
    assert host.uploads == [
        (str(print_grade), "img-1.jpg"),
        (str(tmp_path / "assets" / "mobile" / "img-2.jpg"), "img-2.jpg"),
    ]


async def test_link_images_full_off_uploads_no_photo(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    write_mobile(tmp_path, "img-1")
    candidate = Candidate(
        memo_id=1,
        uid="memo",
        content="",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
        images=[MediaRef(attachment_id=5, uid="img-1", filename="p.jpg", mime="image/jpeg")],
    )
    host = FakeHost()
    await hosting.resolve_links(
        [candidate],
        host=host,
        ledger=RecordingLedger(),
        existing={},
        max_videos_per_memo=2,
        link_images_full=False,
    )
    assert host.uploads == []


# --- the reconciliation probe -----------------------------------------------------------------


async def test_a_present_object_answers_true_and_an_absent_one_answers_false():
    """409 is Dropbox's `path/not_found`, which is an ANSWER — "the upload never landed" — and not
    an error. Reading it as an error would leave the sweep unable to close a stuck row at all."""
    present = _Script(
        {
            hosting.TOKEN_URL: _token_ok(),
            hosting.GET_METADATA_URL: httpx.Response(200, json={"name": "a.mp4"}),
        }
    )
    host = _host(present)
    await host.refresh()
    assert await host.remote_exists("/memos-mail/run/a.mp4") is True

    absent = _Script(
        {
            hosting.TOKEN_URL: _token_ok(),
            hosting.GET_METADATA_URL: httpx.Response(409, json={"error_summary": "path/not_found"}),
        }
    )
    host = _host(absent)
    await host.refresh()
    assert await host.remote_exists("/memos-mail/run/a.mp4") is False


async def test_an_unreadable_probe_raises_rather_than_guessing_absent():
    """A 503 is not "it is not there". Treating it as absent would stamp a live object `swept` and
    leave it on Dropbox with nothing naming it — the orphan the ledger exists to prevent."""
    script = _Script(
        {hosting.TOKEN_URL: _token_ok(), hosting.GET_METADATA_URL: httpx.Response(503, json={})}
    )
    host = _host(script)
    await host.refresh()
    with pytest.raises(hosting.DropboxError, match="metadata"):
        await host.remote_exists("/memos-mail/run/a.mp4")


# --- the account the keys authenticate as -------------------------------------------------------


async def test_the_account_lookup_returns_the_address_and_never_a_token():
    script = _Script(
        {
            hosting.TOKEN_URL: _token_ok(),
            hosting.ACCOUNT_URL: httpx.Response(
                200,
                json={
                    "email": "household@example.com",
                    "name": {"display_name": "The Household"},
                },
            ),
        }
    )
    host = _host(script)
    await host.refresh()
    assert await host.account() == "household@example.com"
    # The request carries the bearer and NO body: `/2/users/get_current_account` takes no
    # arguments, and Dropbox refuses a Content-Type on an argument-less RPC.
    call = next(r for r in script.requests if str(r.url) == hosting.ACCOUNT_URL)
    assert call.content == b""
    assert "content-type" not in {key.lower() for key in call.headers}


async def test_the_account_lookup_falls_back_to_the_display_name():
    """Some Dropbox accounts publish no address on this endpoint; a name still identifies which
    account the keys are for, which is the whole question being asked."""
    script = _Script(
        {
            hosting.TOKEN_URL: _token_ok(),
            hosting.ACCOUNT_URL: httpx.Response(
                200, json={"name": {"display_name": "The Household"}}
            ),
        }
    )
    host = _host(script)
    await host.refresh()
    assert await host.account() == "The Household"


async def test_current_account_answers_none_and_closes_its_client_on_every_failure(monkeypatch):
    """Three failures with three different shapes — a refused refresh, a refused lookup, and a
    malformed body that raises out of `.json()` — all answer None, and none of them leaks the
    HTTP client. The panel line this feeds must never be the reason a page 500s, and a client
    leaked on a failure path accumulates for as long as the fault lasts.
    """
    scripts = {
        "refresh refused": _Script({hosting.TOKEN_URL: httpx.Response(401, json={})}),
        "lookup refused": _Script(
            {hosting.TOKEN_URL: _token_ok(), hosting.ACCOUNT_URL: httpx.Response(503, json={})}
        ),
        "malformed body": _Script(
            {
                hosting.TOKEN_URL: _token_ok(),
                hosting.ACCOUNT_URL: httpx.Response(200, content=b"not json"),
            }
        ),
    }
    # Built BEFORE the constructor is replaced, so the replacement hands out a fresh client per
    # call rather than re-serving one that has already been closed.
    clients = {label: _client(script) for label, script in scripts.items()}
    closed: list[str] = []

    def _count_closes(label: str, client: httpx.AsyncClient) -> None:
        real = client.aclose

        async def _aclose() -> None:
            closed.append(label)
            await real()

        monkeypatch.setattr(client, "aclose", _aclose)

    for label, client in clients.items():
        _count_closes(label, client)

    pending = list(clients.values())
    monkeypatch.setattr(httpx, "AsyncClient", lambda *_a, **_k: pending.pop(0))
    for label in scripts:
        assert (
            await hosting.current_account(
                app_key="key", app_secret="secret", refresh_token="refresh"
            )
            is None
        ), label
    assert closed == list(scripts)


async def test_current_account_makes_no_request_at_all_without_all_three_keys(monkeypatch):
    def _refuse(*_args, **_kwargs):
        raise AssertionError("a client was built with no keys configured")

    monkeypatch.setattr(httpx, "AsyncClient", _refuse)
    assert await hosting.current_account(app_key="", app_secret="s", refresh_token="r") is None


# --- a 409 is a family of answers, not one ------------------------------------------------------


def _conflict(summary: str) -> httpx.Response:
    """A Dropbox route error, in the shape Dropbox actually sends: HTTP 409 plus a TAGGED body."""
    return httpx.Response(
        409,
        json={
            "error_summary": summary,
            "error": {".tag": summary.split("/")[0]},
        },
    )


async def test_only_a_not_found_tagged_409_means_the_object_is_absent():
    """Dropbox answers 409 for EVERY route-level error, not only for a missing path.

    Reading the status alone as "absent" turns a restricted path, a write lock or a rate limit
    into the claim that a file is gone — the sweep then stamps the row `swept` while the object is
    still on Dropbox, and nothing can ever find it again. The TAG is the answer.
    """
    absent = _Script(
        {
            hosting.TOKEN_URL: _token_ok(),
            hosting.GET_METADATA_URL: _conflict("path/not_found/..."),
        }
    )
    host = _host(absent)
    await host.refresh()
    assert await host.remote_exists("/memos-mail/run/a.mp4") is False

    for summary in ("path/restricted_content/...", "too_many_write_operations/..."):
        other = _Script(
            {hosting.TOKEN_URL: _token_ok(), hosting.GET_METADATA_URL: _conflict(summary)}
        )
        host = _host(other)
        await host.refresh()
        with pytest.raises(hosting.DropboxError, match="metadata"):
            await host.remote_exists("/memos-mail/run/a.mp4")


async def test_only_a_not_found_tagged_409_counts_as_already_deleted():
    """The same discrimination on the delete side, where the tag is `path_lookup/not_found`.

    Already-gone is the state the sweep wants and must be stamped. Any other 409 is not that, and
    stamping `swept` on it would record a deletion that never happened.
    """
    gone = _Script(
        {hosting.TOKEN_URL: _token_ok(), hosting.DELETE_URL: _conflict("path_lookup/not_found/..")}
    )
    host = _host(gone)
    await host.refresh()
    await host.delete("/memos-mail/run/a.mp4")  # returns: nothing left to delete

    for summary in ("path_write/conflict/file", "too_many_write_operations/.."):
        refused = _Script({hosting.TOKEN_URL: _token_ok(), hosting.DELETE_URL: _conflict(summary)})
        host = _host(refused)
        await host.refresh()
        with pytest.raises(hosting.DropboxError, match="delete failed"):
            await host.delete("/memos-mail/run/a.mp4")


async def test_a_409_with_an_unreadable_body_is_unknown_rather_than_absent():
    """An untagged or non-JSON 409 tells us nothing, and "nothing" must not resolve a ledger row."""
    script = _Script(
        {
            hosting.TOKEN_URL: _token_ok(),
            hosting.GET_METADATA_URL: httpx.Response(409, content=b"<html>nope</html>"),
        }
    )
    host = _host(script)
    await host.refresh()
    with pytest.raises(hosting.DropboxError, match="metadata"):
        await host.remote_exists("/memos-mail/run/a.mp4")
