"""The MIME message and the acceptance rule (MEMNG-234).

Two assertions here are policy, not shape:

* **no `video/*` part, ever** (user decision 4, AC 2 as amended) — the builder refuses one
  structurally rather than trusting every caller to remember;
* **a part accepted by at least one envelope recipient is a DELIVERY.** The satellite raises on any
  non-empty `refused` dict, which would leave a part uncommitted every morning because of one
  permanently refused address and re-send it to everyone else forever.
"""

import smtplib
from email.mime.multipart import MIMEMultipart

import pytest
from app.services.digest import mailer
from app.services.digest.render import InlineImage

from tests.digest_support import content_ids, html_part, message_content_types, plain_part

RECIPIENTS = ["a@example.com", "b@example.com", "c@example.com"]


def _message(images: list[InlineImage] | None = None) -> MIMEMultipart:
    return mailer.build_message(
        subject="The Daily Dami Memos Mail — July 1, 2026",
        from_email="sender@example.com",
        from_name="The Daily Dami Memos Mail",
        html_body="<p>hello</p>",
        plain_body="hello",
        inline_images=images or [],
    )


# --- the message --------------------------------------------------------------------------------


def test_the_message_offers_both_alternatives():
    types = message_content_types(_message())
    assert "text/plain" in types and "text/html" in types
    assert "multipart/alternative" in types and "multipart/related" in types
    assert html_part(_message()) == "<p>hello</p>"
    assert plain_part(_message()) == "hello"


def test_inline_images_are_attached_with_their_content_ids():
    images = [InlineImage(cid="img-1", data=b"AAA"), InlineImage(cid="poster-1", data=b"BBB")]
    message = _message(images)
    assert content_ids(message) == ["img-1", "poster-1"]
    assert message_content_types(message).count("image/jpeg") == 2


def test_the_message_contains_no_video_part():
    """AC 2 as amended: a video is a poster, a play badge and a Dropbox link — never an
    attachment."""
    images = [InlineImage(cid="poster-1", data=b"BBB")]
    assert not any(t.startswith("video/") for t in message_content_types(_message(images)))


def test_attaching_a_non_image_is_refused_structurally():
    """The mp4-attach path decision 4 forbids cannot be reached by forgetting a check at a call
    site — the builder itself will not produce it."""
    with pytest.raises(ValueError, match="images only"):
        _message([InlineImage(cid="v", data=b"x", content_type="video/mp4")])


def test_to_is_the_sender_so_recipients_never_see_each_other():
    """Every recipient rides in the ENVELOPE (`sendmail(sender, recipients, …)`); six external
    addresses in a visible `To:` would leak the household's mailing list to all of them."""
    message = _message()
    assert message["To"] == "The Daily Dami Memos Mail <sender@example.com>"
    assert message["From"] == message["To"]
    for address in RECIPIENTS:
        assert address not in message.as_string()


# --- acceptance and refusal ---------------------------------------------------------------------


def test_a_part_nobody_refused_is_fully_accepted():
    assert mailer.accepted_count(RECIPIENTS, {}) == 3


def test_a_part_one_recipient_refused_is_still_a_delivery():
    refusals = {"b@example.com": (550, b"no such user")}
    assert mailer.accepted_count(RECIPIENTS, refusals) == 2


def test_refusal_matching_is_case_insensitive():
    assert mailer.accepted_count(RECIPIENTS, {"B@Example.COM": (550, b"x")}) == 2


def test_only_a_part_nobody_accepted_is_a_failed_part():
    refusals = {address: (550, b"x") for address in RECIPIENTS}
    assert mailer.accepted_count(RECIPIENTS, refusals) == 0


def test_the_refusal_note_counts_addresses_and_names_none():
    """The run row is read by the panel and shipped to the logs; a recipient address is personal
    data that has no business in either."""
    note = mailer.refusal_note(2, {"b@example.com": (550, b"x")})
    assert note == "part 2: refused 1 recipient(s)"
    assert "b@example.com" not in note


# --- the transport ------------------------------------------------------------------------------


async def test_the_transport_never_enables_smtp_debug(monkeypatch):
    """LSN-0022: `set_debuglevel` prints the AUTH exchange, credentials included, and this
    deployment ships stdout to Loki. Asserted against the real send path, not by reading the
    source."""
    calls: list[str] = []

    class _Recorder:
        def __init__(self, host, port, timeout=None):
            calls.append(f"connect {host}:{port}")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            calls.append("ehlo")

        def starttls(self):
            calls.append("starttls")

        def login(self, user, password):
            calls.append("login")

        def set_debuglevel(self, level):  # pragma: no cover — the assertion is that it is unused
            calls.append("set_debuglevel")

        def sendmail(self, sender, recipients, body):
            calls.append("sendmail")
            return {}

    monkeypatch.setattr(smtplib, "SMTP", _Recorder)
    transport = mailer.SmtplibTransport(
        mailer.SmtpSettings(host="smtp.test", port=587, username="u", password="p")
    )
    assert await transport.send(_message(), "sender@example.com", RECIPIENTS) == {}
    assert "set_debuglevel" not in calls
    assert calls == [
        "connect smtp.test:587",
        "ehlo",
        "starttls",
        "ehlo",
        "login",
        "sendmail",
    ]
