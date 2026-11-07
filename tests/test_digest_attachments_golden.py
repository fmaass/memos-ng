"""`attachments` mode is byte-for-byte what it was before link mode existed (MEMNG-1246, D7).

The golden file `tests/fixtures/digest-attachments-golden.json` was written by THIS test against
the tree before `delivery_mode` was added (commit a2e0870), with `DIGEST_GOLDEN_WRITE=1`. Every
later run compares the mails a real run sends — subject, sender, envelope, MIME shape, both
alternatives and the sha256 of every inline image — against it, so any change link mode makes to
the attachments path is a red test rather than a surprise in six inboxes.

Two things are pinned so the bytes are reproducible: the memo and attachment uids, and the random
half of each `cid:` (`render.uuid.uuid4`), which is otherwise fresh per run. Nothing else is
faked: the database, the selection, the encoder, the splitter, the template and the MIME builder
are all the real ones; SMTP is the only system replaced.
"""

import hashlib
import json
import os
import secrets
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from app.config import settings as app_settings
from app.database import async_session
from app.models import User, Visibility
from app.services.digest import render
from app.services.digest import runner as digest_runner
from PIL import Image
from sqlalchemy import delete

from tests.digest_support import (
    FakeTransport,
    add_recipients,
    html_part,
    plain_part,
    reset_digest_state,
    seed_attachment,
    seed_memo,
    seed_user,
    set_digest_settings,
    write_mobile,
)

GOLDEN = Path(__file__).parent / "fixtures" / "digest-attachments-golden.json"
SENDER = "sender@example.com"
NOW = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)


class _CountingUuid:
    """`uuid.uuid4()` with a predictable `.hex`, so every `cid:` is the same on every run."""

    def __init__(self) -> None:
        self.calls = 0

    def uuid4(self):
        self.calls += 1
        value = f"{self.calls:08x}" + "0" * 24

        class _Hex:
            hex = value

        return _Hex()


@pytest.fixture
async def env(db_engine, tmp_path, monkeypatch):
    reset_digest_state()
    monkeypatch.setattr(app_settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(app_settings, "SMTP_USERNAME", SENDER)
    monkeypatch.setattr(app_settings, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(render, "uuid", _CountingUuid())
    # A low cap so each language group splits into two parts: the splitter is part of what must not
    # move. The API floors this at 1 MiB; the row itself takes any value.
    set_digest_settings(
        enabled=True, start_date="2026-06-01", grace_minutes=30, max_email_bytes=24000
    )
    subject = f"digest-golden-{secrets.token_hex(4)}"
    try:
        yield subject, tmp_path
    finally:
        reset_digest_state()
        async with async_session() as db:
            await db.execute(delete(User).where(User.subject.like(f"{subject}%")))
            await db.commit()


async def _seed(subject: str, data_root: Path) -> None:
    async with async_session() as db:
        user = await seed_user(db, subject=subject)
        specs = [
            (
                "goldenm1",
                "First day, with a [link home](https://memos.discomarder.live/m/x) and "
                "[a foreign one](https://example.org/a).\n\n#family",
                datetime(2026, 6, 28, 9, 0, tzinfo=UTC),
                ["goldena1", "goldena2"],
                [],
            ),
            (
                "goldenm2",
                "**Second** memo, same day",
                datetime(2026, 6, 28, 17, 30, tzinfo=UTC),
                ["goldena3"],
                ["goldenv1"],
            ),
            (
                "goldenm3",
                "Third memo, the next day\n\n- one\n- two",
                datetime(2026, 6, 29, 8, 15, tzinfo=UTC),
                ["goldena4"],
                [],
            ),
        ]
        for uid, content, created_at, photos, videos in specs:
            memo = await seed_memo(
                db,
                user,
                uid=uid,
                content=content,
                visibility=Visibility.PUBLIC,
                created_at=created_at,
            )
            for index, photo in enumerate(photos):
                await seed_attachment(
                    db, user, memo, uid=photo, mime="image/jpeg", filename=f"{photo}.jpg"
                )
                write_mobile(data_root, photo, size=(320 + 40 * index, 240))
            for video in videos:
                await seed_attachment(
                    db, user, memo, uid=video, mime="video/mp4", filename=f"{video}.mp4"
                )
        await db.commit()


def _stable_sha256(data: bytes) -> str:
    """The image part's sha256 with ONE field held still: the embedded ICC profile's creation
    date-time (profile bytes 24..36), which littleCMS stamps from the clock on every encode. Every
    other byte of the part — pixels, quantisation, Huffman tables, the rest of the profile — is
    hashed as sent."""
    with Image.open(BytesIO(data)) as image:
        icc = image.info.get("icc_profile")
    if icc:
        data = data.replace(icc, icc[:24] + b"\0" * 12 + icc[36:])
    return hashlib.sha256(data).hexdigest()


def _snapshot(transport: FakeTransport) -> list[dict]:
    mails = []
    for mail in transport.sent:
        message = mail.message
        mails.append(
            {
                "subject": message["Subject"],
                "from": message["From"],
                "to": message["To"],
                "envelope_sender": mail.sender,
                "recipients": mail.recipients,
                "content_types": [part.get_content_type() for part in message.walk()],
                "html": html_part(message),
                "plain": plain_part(message),
                # Sorted: the builder attaches a part's images in `set` order, which varies with the
                # interpreter's hash seed. The order is not something a reader can see — every
                # image is reached by its `cid:` — so the golden file pins the SET of parts.
                "images": sorted(
                    (
                        {
                            "cid": part["Content-ID"],
                            "disposition": part["Content-Disposition"],
                            "sha256": _stable_sha256(part.get_payload(decode=True)),
                        }
                        for part in message.walk()
                        if part.get("Content-ID") is not None
                    ),
                    key=lambda image: image["cid"],
                ),
            }
        )
    return mails


async def test_attachments_mode_sends_exactly_the_golden_mails(env):
    subject, data_root = env
    add_recipients("anna@example.com", "bernd@example.com", language="de")
    add_recipients("carol@example.com", language="en")
    await _seed(subject, data_root)
    transport = FakeTransport()

    result = await digest_runner.run_digest(transport=transport, now_utc=NOW)

    assert result is not None and result.status == digest_runner.SENT
    actual = _snapshot(transport)
    if os.environ.get("DIGEST_GOLDEN_WRITE") == "1":
        GOLDEN.write_text(json.dumps(actual, indent=2, ensure_ascii=False) + "\n", "utf-8")
    expected = json.loads(GOLDEN.read_text("utf-8"))
    assert len(actual) == len(expected)
    for index, (got, want) in enumerate(zip(actual, expected, strict=True)):
        for key in want:
            assert got[key] == want[key], f"mail {index + 1}: {key} differs from the golden file"
