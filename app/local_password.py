"""Set a user's local password: `python -m app.local_password <username> [--create [--admin]]`.

The operator's side of LOCAL_LOGIN (MEMNG-1097). A one-household server resets a password by an
admin running this, not by an email flow. The password is read from the terminal without echo, or
as one line on stdin when stdin is not a terminal, so it never appears in argv or shell history.
"""

import argparse
import getpass
import sys

from sqlalchemy import create_engine, insert, update

from app.auth import hash_password
from app.config import settings
from app.models import User


def set_local_password(
    sync_url: str, subject: str, password: str, *, create: bool = False, role: str = "user"
) -> bool:
    """Store an argon2id hash of `password` for the user `subject`. True when a user now holds it;
    False when no such user exists and `create` is off. An empty password is refused."""
    if not password:
        raise ValueError("an empty password is refused")
    hashed = hash_password(password)
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            updated = conn.execute(
                update(User).where(User.subject == subject).values(password_hash=hashed)
            )
            if updated.rowcount == 1:
                return True
            if not create:
                return False
            conn.execute(insert(User).values(subject=subject, role=role, password_hash=hashed))
            return True
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.local_password")
    parser.add_argument("username")
    parser.add_argument("--create", action="store_true", help="create the user if it is missing")
    parser.add_argument("--admin", action="store_true", help="with --create: make it an admin")
    args = parser.parse_args(argv)
    if sys.stdin.isatty():
        password = getpass.getpass("New password: ")
        if getpass.getpass("Repeat: ") != password:
            print("passwords differ; nothing changed", file=sys.stderr)
            return 1
    else:
        password = sys.stdin.readline().rstrip("\n")
    role = "admin" if args.admin else "user"
    try:
        ok = set_local_password(
            settings.DATABASE_URL_SYNC, args.username, password, create=args.create, role=role
        )
    except ValueError as refused:
        print(str(refused), file=sys.stderr)
        return 1
    if not ok:
        print(f"no user {args.username!r}; pass --create to make one", file=sys.stderr)
        return 1
    print(f"password set for {args.username!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
