from __future__ import annotations

import argparse
import json
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


def _run(args: argparse.Namespace) -> int:
    backend_dir = Path(__file__).resolve().parents[1]
    repo_dir = backend_dir.parent
    if str(repo_dir) not in sys.path:
        sys.path.insert(0, str(repo_dir))
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    from app.db import Base
    from app import models  # noqa: F401
    from app.services.vidman_competitor_price_lists import (
        build_vidman_competitor_price_list,
        ensure_vidman_competitor_price_list_source,
    )

    database_url = _normalize_database_url(args.database_url or os.getenv("DATABASE_URL", ""))
    if not database_url:
        print("ERROR: DATABASE_URL is required. Refusing to fall back to SQLite.")
        return 2
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        print(f"ERROR: Vidman price-list publishing requires PostgreSQL, got {url.get_backend_name()}.")
        print("STOP: no SQLite/test DB fallback was used.")
        return 2

    print(f"Database: {_database_description(database_url)}")
    engine = create_engine(database_url, pool_pre_ping=True, pool_recycle=300)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with Session() as db:
        if args.configure_source:
            if not args.region or not args.branch_name or not args.competitor_name:
                print("ERROR: --configure-source requires --region, --branch-name, and --competitor-name.")
                return 2
            source = ensure_vidman_competitor_price_list_source(
                db=db,
                account_id=args.account_id,
                main_id=args.main_id,
                price_format_code=args.price_format_code,
                is_active=args.active,
                region=args.region,
                branch_id=args.branch_id or args.branch_name,
                branch_code=args.branch_code or args.branch_id or args.branch_name,
                branch_name=args.branch_name,
                competitor_name=args.competitor_name,
                price_coefficient=args.price_coefficient,
            )
            db.commit()
            print(
                json.dumps(
                    {
                        "configured": True,
                        "source_id": source.id,
                        "active": source.is_active,
                        "account_id": source.account_id,
                        "main_id": source.main_id,
                        "price_format_code": source.price_format_code,
                        "region": source.region,
                        "branch_name": source.branch_name,
                        "competitor_name": source.competitor_name,
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )

        summary = build_vidman_competitor_price_list(
            db=db,
            account_id=args.account_id,
            main_id=args.main_id,
            price_format_code=args.price_format_code,
            import_run_id=args.import_run_id,
            apply=bool(args.apply),
            preview_limit=args.preview_limit,
            require_active=not args.allow_inactive_dry_run,
        )
        print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2, default=str))
        if summary.skipped_reason:
            return 1
    return 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Build one Vidman PLK into existing competitor price-list tables.")
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--main-id", type=int, required=True)
    parser.add_argument("--price-format-code", required=True)
    parser.add_argument("--import-run-id", type=int)
    parser.add_argument("--preview-limit", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview only. This is the default.")
    parser.add_argument("--apply", action="store_true", help="Persist the built list. Default is dry-run.")
    parser.add_argument("--allow-inactive-dry-run", action="store_true", help="Inspect rows even before PLK activation.")
    parser.add_argument("--configure-source", action="store_true", help="Create/update explicit PLK source mapping before build.")
    parser.add_argument("--active", action="store_true", help="Mark configured PLK source active.")
    parser.add_argument("--region", default="")
    parser.add_argument("--branch-id", default="")
    parser.add_argument("--branch-code", default="")
    parser.add_argument("--branch-name", default="")
    parser.add_argument("--competitor-name", default="")
    parser.add_argument("--price-coefficient", default="1")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="PostgreSQL URL; defaults to DATABASE_URL")
    return _run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
