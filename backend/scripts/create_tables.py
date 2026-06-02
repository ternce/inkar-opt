from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from dotenv import load_dotenv


def _normalize_database_url(url: str) -> str:
    # Railway and some providers may use postgres://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    # Force psycopg v3 driver if not explicitly selected
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://") :]

    return url


def _mask_url(url: str) -> str:
    # Best-effort masking: keep scheme/host/db, hide password.
    # Works for urls like postgresql://user:pass@host:port/db
    try:
        scheme_sep = url.find("://")
        if scheme_sep == -1:
            return "***"
        scheme = url[: scheme_sep + 3]
        rest = url[scheme_sep + 3 :]

        at = rest.rfind("@")
        if at == -1:
            return scheme + rest

        creds = rest[:at]
        hostpart = rest[at:]

        colon = creds.find(":")
        if colon == -1:
            return scheme + creds + hostpart

        user = creds[:colon]
        return scheme + f"{user}:***" + hostpart
    except Exception:
        return "***"


def main() -> int:
    # Allow using a local .env (same behavior as the FastAPI app)
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Create all database tables from SQLAlchemy models (PostgreSQL/Railway friendly)."
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL"),
        help="Database URL. If omitted, uses env DATABASE_URL.",
    )
    args = parser.parse_args()

    database_url = args.database_url
    if not database_url:
        print("ERROR: DATABASE_URL is not set and --database-url was not provided.")
        print("")
        print("Ways to run:")
        print("  1) Pass explicitly:")
        print('     python scripts/create_tables.py --database-url "postgresql://user:pass@host:port/db"')
        print("  2) Or set env var (PowerShell):")
        print('     $env:DATABASE_URL = "postgresql://user:pass@host:port/db"')
        print("     python scripts/create_tables.py")
        print("  3) Or create a .env file and set DATABASE_URL there.")
        return 2

    database_url = _normalize_database_url(database_url)

    # Ensure we can import backend/app as a top-level package (app.*)
    backend_dir = Path(__file__).resolve().parents[1]
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    # Import Base and models to register tables
    from app.db import Base  # noqa: E402
    from app import models  # noqa: F401,E402

    print(f"Connecting to: {_mask_url(database_url)}")

    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_recycle=300,
    )

    Base.metadata.create_all(bind=engine)
    print("OK: tables ensured (create_all complete)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
