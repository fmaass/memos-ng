#!/usr/bin/env python3
"""Phase-0 empirical probe: prove the iCloud SMTP -> Aura-frame send path end to end.

Sends ONE small JPEG to an Aura frame address via smtp.mail.me.com:587 (STARTTLS),
so we can pin down which From identity iCloud + Aura actually accept before any app
code is parameterized on it.

The app-specific password is read from the FRAME_SMTP_PASSWORD env var ONLY — never a
CLI flag, never logged, never echoed. Source it silently, e.g.:

    set -a; source ~/.config/memos-ng/frame.env; set +a
    uv run python scripts/frame_smtp_probe.py --from "$FRAME_SMTP_FROM" \
        --to "$FRAME_SMTP_TO" --image /path/to/test.jpg

Env vars:
  FRAME_SMTP_PASSWORD  (required) app-specific password for the iCloud account
  FRAME_SMTP_USERNAME  (optional) SMTP AUTH identity; defaults to --from. Set this when
                       authenticating as the primary Apple ID but sending From an alias.
  FRAME_SMTP_HOST      (optional) default smtp.mail.me.com
  FRAME_SMTP_PORT      (optional) default 587

Remember: SMTP 250 is relay ACCEPTANCE, not delivery. The Phase-0 acceptance gate is the
photo VISIBLE ON THE PHYSICAL FRAME — look at the frame after a green send.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path


def build_message(sender: str, recipient: str, image: Path) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = "memos-ng frame probe"
    msg.set_content("Aura ignores the body; this is a Phase-0 SMTP probe.")

    ctype, _ = mimetypes.guess_type(image.name)
    maintype, _, subtype = (ctype or "image/jpeg").partition("/")
    if maintype != "image":
        raise SystemExit(f"refusing to send non-image attachment: {image.name} ({ctype})")
    msg.add_attachment(
        image.read_bytes(), maintype=maintype, subtype=subtype or "jpeg", filename=image.name
    )
    return msg


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase-0 iCloud->Aura SMTP probe")
    ap.add_argument("--from", dest="sender", required=True, help="From address")
    ap.add_argument("--to", dest="recipient", required=True, help="Aura frame address")
    ap.add_argument("--image", dest="image", required=True, type=Path, help="path to a JPEG")
    args = ap.parse_args()

    password = os.environ.get("FRAME_SMTP_PASSWORD")
    if not password:
        print("FRAME_SMTP_PASSWORD is unset — source your env file first.", file=sys.stderr)
        return 2
    username = os.environ.get("FRAME_SMTP_USERNAME") or args.sender
    host = os.environ.get("FRAME_SMTP_HOST", "smtp.mail.me.com")
    port = int(os.environ.get("FRAME_SMTP_PORT", "587"))

    if not args.image.is_file():
        print(f"image not found: {args.image}", file=sys.stderr)
        return 2

    msg = build_message(args.sender, args.recipient, args.image)

    print(f"connecting {host}:{port} as {username}, From={args.sender} To={args.recipient}")
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(username, password)
            smtp.send_message(msg)
    except smtplib.SMTPException as exc:
        # Print the SMTP exception (server code/text), which never contains the password.
        print(f"SEND FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("SMTP accepted (250-class). NOW LOOK AT THE FRAME — acceptance is not delivery.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
