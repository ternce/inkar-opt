from __future__ import annotations

import argparse
import getpass
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal  # noqa: E402
from app.models import AppUser  # noqa: E402
from app.services.auth import hash_password  # noqa: E402
from app.timezone import now_kz_naive  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Set an INKAR OPT app user password.")
    parser.add_argument("username", help="Existing app_users.username")
    args = parser.parse_args()

    password = getpass.getpass("New password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        return 1

    with SessionLocal() as db:
        user = db.query(AppUser).filter(AppUser.username == args.username).one_or_none()
        if user is None:
            print(f"User not found: {args.username}", file=sys.stderr)
            return 1
        user.password_hash = hash_password(password)
        user.password_changed_at = now_kz_naive()
        user.updated_at = now_kz_naive()
        db.commit()
    print(f"Password updated for {args.username}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
