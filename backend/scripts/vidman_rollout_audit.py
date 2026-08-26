from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


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
    for path in (str(repo_dir), str(backend_dir)):
        if path not in sys.path:
            sys.path.insert(0, path)

    from backend.app.services.vidman_rollout_audit import build_rollout_audit, report_to_dict

    database_url = _normalize_database_url(args.database_url or os.getenv("DATABASE_URL", ""))
    if not database_url:
        print("ERROR: DATABASE_URL is required. Refusing to fall back to SQLite.")
        return 2
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        print(f"ERROR: Vidman rollout audit requires PostgreSQL, got {url.get_backend_name()}.")
        print("STOP: no SQLite/test DB fallback was used.")
        return 2

    print(f"Database: {_database_description(database_url)}")
    engine = create_engine(database_url, pool_pre_ping=True, pool_recycle=300)
    with engine.connect() as conn:
        tx = conn.begin()
        conn.execute(text("SET TRANSACTION READ ONLY"))
        report = build_rollout_audit(conn, sample_limit=args.sample_limit)
        data = report_to_dict(report)
        if args.account_id is not None:
            data["sources"] = [row for row in data["sources"] if int(row["account_id"]) == args.account_id]
        if args.main_id is not None:
            data["sources"] = [row for row in data["sources"] if int(row["main_id"]) == args.main_id]
        if args.duplicates:
            data = {"duplicates": data["duplicates"]}
        if args.rollout_plan:
            data = {
                "wave1": [row for row in report_to_dict(report)["sources"] if row["readiness"] == "READY_FOR_APPLY" and row["coverage_band"] == "HIGH_COVERAGE"],
                "wave2": [row for row in report_to_dict(report)["sources"] if row["readiness"] == "READY_FOR_APPLY" and row["coverage_band"] != "HIGH_COVERAGE"],
                "hold": [row for row in report_to_dict(report)["sources"] if row["readiness"] != "READY_FOR_APPLY"],
            }
        text_out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        print(text_out)
        if args.output:
            Path(args.output).write_text(text_out, encoding="utf-8")
        tx.rollback()
    return 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Read-only Vidman multi-PLK rollout audit.")
    parser.add_argument("--all", action="store_true", help="Audit all collected Vidman PLKs. Default behavior.")
    parser.add_argument("--account-id", type=int)
    parser.add_argument("--main-id", type=int)
    parser.add_argument("--duplicates", action="store_true")
    parser.add_argument("--rollout-plan", action="store_true")
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--output", default="")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="PostgreSQL URL; defaults to DATABASE_URL")
    return _run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
