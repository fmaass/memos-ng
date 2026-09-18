"""The MIME message and the SMTP transport, ported from the satellite's `app/mailer.py`.

Two things are deliberately NOT ported.

**The `refused` dict never raises.** The satellite raises `SMTPRecipientsRefused` on any non-empty
dict (`app/mailer.py:68-70`). Here a part accepted by at least one envelope recipient is a
DELIVERY: it commits, and the refused addresses go into the run row's error. Raising would make one
permanently refused address hold that part uncommitted every morning and re-send it to everyone
else forever — unbounded duplication, not the bounded window the plan accepts. Only a part accepted
by ZERO recipients is a failed part.

**`set_debuglevel` is never called** (LSN-0022): it prints the AUTH exchange, credentials included,
to stdout, and stdout is shipped to Loki.

`To:` is the sender and every recipient rides in the envelope BCC (`sendmail(sender, recipients,
…)`), which is what keeps six external addresses from seeing each other.
"""

import smtplib
from dataclasses import dataclass
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Protocol

import structlog
from anyio import to_thread

from app.services.digest.render import InlineImage

logger = structlog.get_logger(__name__)

SMTP_TIMEOUT_S = 30

# smtplib's own shape: address → (code, message) for every recipient the server refused.
Refusals = dict[str, tuple[int, bytes]]


def build_message(
    *,
    subject: str,
    from_email: str,
    from_name: str,
    html_body: str,
    plain_body: str,
    inline_images: list[InlineImage],
) -> MIMEMultipart:
    """The message, `multipart/mixed( multipart/alternative( text, multipart/related ) )`.

    No `video/*` part is ever attached, in any configuration: a video is a poster, a play badge
    and a link to its Dropbox copy (user decision 4). The only attachments this function can
    produce are the `image/*` inline parts the template references by `cid:`.
    """
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(plain_body, "plain", "utf-8"))

    related = MIMEMultipart("related")
    related.attach(MIMEText(html_body, "html", "utf-8"))
    for image in inline_images:
        maintype, _, subtype = image.content_type.partition("/")
        if maintype != "image":
            # Structural, not defensive: the ONLY thing this builder may attach is an image. A
            # non-image reaching here would be the mp4-attach path decision 4 forbids.
            raise ValueError(f"the digest attaches images only, not {image.content_type!r}")
        part = MIMEImage(image.data, _subtype=subtype)
        part.add_header("Content-ID", f"<{image.cid}>")
        part.add_header("Content-Disposition", "inline")
        related.attach(part)
    alternative.attach(related)

    message = MIMEMultipart("mixed")
    message["Subject"] = subject
    message["From"] = f"{from_name} <{from_email}>"
    message["To"] = f"{from_name} <{from_email}>"
    message.attach(alternative)
    return message


class MailTransport(Protocol):
    """What the runner needs from SMTP, and the whole seam a test replaces.

    A fake transport substitutes the DEPENDENCY (the mail server), never the unit under test: the
    message the runner builds, the recipient list it computes and the commit boundary it keeps are
    all still exercised.
    """

    async def send(
        self, message: MIMEMultipart, sender: str, recipients: list[str]
    ) -> Refusals: ...


@dataclass
class SmtpSettings:
    host: str
    port: int
    username: str
    password: str
    use_tls: bool = True


def _send_sync(
    settings: SmtpSettings, message: MIMEMultipart, sender: str, recipients: list[str]
) -> Refusals:
    with smtplib.SMTP(settings.host, settings.port, timeout=SMTP_TIMEOUT_S) as server:
        server.ehlo()
        if settings.use_tls:
            server.starttls()
            server.ehlo()
        server.login(settings.username, settings.password)
        return server.sendmail(sender, recipients, message.as_string())


class SmtplibTransport:
    """stdlib `smtplib` over STARTTLS, off the event loop.

    The whole session — connect, STARTTLS, AUTH, DATA — runs inside ONE `to_thread.run_sync` hop,
    so the loop keeps stamping the worker heartbeat for a send that can take minutes.
    """

    def __init__(self, settings: SmtpSettings) -> None:
        self._settings = settings

    async def send(self, message: MIMEMultipart, sender: str, recipients: list[str]) -> Refusals:
        return await to_thread.run_sync(_send_sync, self._settings, message, sender, recipients)


def accepted_count(recipients: list[str], refusals: Refusals) -> int:
    """How many envelope recipients accepted. ZERO is the only failed part."""
    refused = {address.lower() for address in refusals}
    return sum(1 for address in recipients if address.lower() not in refused)


def refusal_note(part_index: int, refusals: Refusals) -> str:
    """`part N: refused <n> recipient(s)` — the run row's error text.

    The COUNT, never the addresses: the run row is read by the panel and shipped to the logs, and
    a recipient address is personal data that has no business in either.
    """
    return f"part {part_index}: refused {len(refusals)} recipient(s)"
