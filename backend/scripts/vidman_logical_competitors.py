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


def _session(args: argparse.Namespace):
    backend_dir = Path(__file__).resolve().parents[1]
    repo_dir = backend_dir.parent
    for path in (str(repo_dir), str(backend_dir)):
        if path not in sys.path:
            sys.path.insert(0, path)

    from app.db import Base
    from app import models  # noqa: F401

    database_url = _normalize_database_url(args.database_url or os.getenv("DATABASE_URL", ""))
    if not database_url:
        raise RuntimeError("DATABASE_URL is required. Refusing to fall back to SQLite.")
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError(f"Vidman logical mapping requires PostgreSQL, got {url.get_backend_name()}.")
    print(f"Database: {_database_description(database_url)}")
    engine = create_engine(database_url, pool_pre_ping=True, pool_recycle=300)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _proposals() -> list[dict[str, object]]:
    return [
        {
            "logical_name": "Инкар Актау",
            "region": "Aktau",
            "price_format_code": "004",
            "collision_status": "SAME_PHYSICAL_COMPETITOR",
            "collision_notes": "Stage 4.1 found Provisor Инкар Актау collision candidates; keep one logical contribution policy.",
            "sources": [
                {"account_id": 2, "main_id": 11870, "role": "PRIMARY", "priority": 0, "approval": "approved"},
                {"account_id": 1, "main_id": 9352, "role": "FALLBACK", "priority": 10, "approval": "proposed"},
            ],
            "evidence": "2202 overlapping publishable products, 100% overlap, 100% exact price agreement.",
        },
        {
            "logical_name": "Belasar Pharmacy",
            "region": "",
            "price_format_code": "",
            "collision_status": "UNRESOLVED",
            "sources": [
                {"account_id": 2, "main_id": 10298, "role": "PRIMARY", "priority": 0, "approval": "proposed"},
                {"account_id": 1, "main_id": 10298, "role": "FALLBACK", "priority": 10, "approval": "proposed"},
            ],
            "evidence": "401 overlapping publishable products, 100% exact price agreement; region/format still missing.",
        },
        {
            "logical_name": "Эмити Интернешнл Актау",
            "region": "Aktau",
            "price_format_code": "",
            "collision_status": "POSSIBLE_COLLISION",
            "sources": [
                {"account_id": 2, "main_id": 9167, "role": "PRIMARY", "priority": 0, "approval": "needs_manual_approval"},
                {"account_id": 1, "main_id": 9373, "role": "FALLBACK", "priority": 10, "approval": "needs_manual_approval"},
            ],
            "evidence": "31 overlapping publishable products, median price difference about 0.36%, p90 about 0.76%.",
        },
        {"logical_name": "РАУЗА-АДЕ ФИЛЛИАЛ В Г АКТОБЕ", "sources": [{"account_id": 2, "main_id": 4237}, {"account_id": 1, "main_id": 4237}], "approval": "hold"},
        {"logical_name": "Медсервис Плюс Актау", "sources": [{"account_id": 2, "main_id": 175}, {"account_id": 1, "main_id": 175}], "approval": "hold"},
        {"logical_name": "ТОО Эльвива", "sources": [{"account_id": 2, "main_id": 12537}, {"account_id": 1, "main_id": 12537}], "approval": "hold"},
    ]


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Manage Vidman logical competitor mappings.")
    parser.add_argument("command", choices=["list", "propose", "assign"])
    parser.add_argument("--logical-name", default="")
    parser.add_argument("--account-id", type=int)
    parser.add_argument("--main-id", type=int)
    parser.add_argument("--role", default="PRIMARY", choices=["PRIMARY", "FALLBACK"])
    parser.add_argument("--priority", type=int)
    parser.add_argument("--region", default="")
    parser.add_argument("--price-format-id", type=int)
    parser.add_argument("--price-format-code", default="")
    parser.add_argument("--collision-status", default="UNRESOLVED")
    parser.add_argument("--collision-notes", default="")
    parser.add_argument("--inactive", action="store_true")
    parser.add_argument("--not-approved", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    args = parser.parse_args()

    if args.command == "propose":
        print(json.dumps({"proposals": _proposals()}, ensure_ascii=False, indent=2, default=str))
        return 0

    db = _session(args)
    try:
        from app.services.vidman_logical_competitors import assign_source_to_logical_competitor, list_logical_competitors

        if args.command == "list":
            print(json.dumps({"logical_competitors": list_logical_competitors(db=db)}, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.account_id is None or args.main_id is None:
            raise RuntimeError("--account-id and --main-id are required for assign")
        result = assign_source_to_logical_competitor(
            db=db,
            logical_name=args.logical_name,
            account_id=args.account_id,
            main_id=args.main_id,
            role=args.role,
            region=args.region,
            price_format_id=args.price_format_id,
            price_format_code=args.price_format_code,
            priority=args.priority,
            active=not args.inactive,
            approved_manually=not args.not_approved,
            collision_status=args.collision_status,
            collision_notes=args.collision_notes,
            apply=args.apply,
        )
        if args.apply:
            db.commit()
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
