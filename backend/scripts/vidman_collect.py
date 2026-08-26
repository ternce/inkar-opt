from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker


def _normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _database_description(database_url: str) -> str:
    url = make_url(database_url)
    return f"engine={url.get_backend_name()} host={url.host or ''} database={url.database or ''}"


async def _run(args: argparse.Namespace) -> int:
    backend_dir = Path(__file__).resolve().parents[1]
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    from app.db import Base
    from app import models  # noqa: F401
    from app.services.vidman_raw_collector import VidmanCollectorConfig, VidmanRawCollector

    database_url = _normalize_database_url(args.database_url or os.getenv("DATABASE_URL", ""))
    if not database_url:
        print("ERROR: DATABASE_URL is required. Refusing to fall back to SQLite.")
        return 2

    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        print(f"ERROR: Vidman raw collection requires PostgreSQL, got {url.get_backend_name()}.")
        print("STOP: no SQLite/test DB fallback was used.")
        return 2

    print(f"Database: {_database_description(database_url)}")

    engine = create_engine(database_url, pool_pre_ping=True, pool_recycle=300)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    config = VidmanCollectorConfig(
        delay_seconds=args.delay_seconds,
        retry_count=args.retry_count,
        timeout=args.timeout,
        auth_mode=args.auth_mode,
        only_main_id=args.only_main_id,
        start_page=args.start_page,
        max_pages=args.max_pages,
    )
    with Session() as db:
        collector = VidmanRawCollector(
            db=db,
            login=args.login,
            password=args.password,
            account_name=args.account_name or "",
            config=config,
        )
        await collector.collect(resume_run_id=args.resume_run_id)
    return 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Collect raw Vidman PLK rows into PostgreSQL.")
    parser.add_argument("--login", required=True, help="Vidman login")
    parser.add_argument("--password", required=True, help="Vidman password")
    parser.add_argument("--account-name", default="", help="Optional display name stored for the account")
    parser.add_argument("--resume-run-id", type=int, help="Resume an existing vidman_import_runs id")
    parser.add_argument("--only-main-id", type=int, help="Collect only one resolved main_id")
    parser.add_argument("--start-page", type=int, help="Start from this page number")
    parser.add_argument("--max-pages", type=int, help="Stop after this many detected pages")
    parser.add_argument("--delay-seconds", type=float, default=0.25, help="Delay between page requests")
    parser.add_argument("--retry-count", type=int, default=3, help="Bounded HTTP retry count")
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP read timeout")
    parser.add_argument("--auth-mode", default="auto", choices=["auto", "httpx", "playwright"], help="Vidman auth mode")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="PostgreSQL URL; defaults to DATABASE_URL")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
