from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import SessionLocal, engine
from backend.app.services.vidman_review_triage import REVIEW_TIERS, process_review_triage


MIGRATION_PATH = ROOT / "backend" / "scripts" / "migrations" / "20260823_vidman_stage3_3_review_triage.sql"


def _database_identity() -> dict[str, str]:
    url = engine.url
    return {
        "engine": engine.dialect.name,
        "host": url.host or "",
        "database": url.database or "",
    }


def _validate_real_database() -> None:
    identity = _database_identity()
    print(f"engine={identity['engine']}", flush=True)
    print(f"host={identity['host']}", flush=True)
    print(f"database={identity['database']}", flush=True)

    database_path = (identity["database"] or "").replace("\\", "/").lower()
    if identity["engine"] == "sqlite" or database_path.endswith("/backend/app.db") or database_path.endswith("app.db"):
        raise SystemExit("Refusing Vidman review triage on SQLite/backend/app.db.")
    if identity["engine"] != "postgresql":
        raise SystemExit("Refusing Vidman review triage on a non-PostgreSQL engine.")
    if identity["host"] not in {"localhost", "127.0.0.1", "::1"} or identity["database"] != "apteka":
        raise SystemExit("Refusing Vidman review triage outside host=localhost database=apteka.")


def _apply_migration() -> None:
    statements = [statement.strip() for statement in MIGRATION_PATH.read_text(encoding="utf-8").split(";")]
    with engine.begin() as conn:
        for statement in statements:
            if statement:
                conn.execute(text(statement))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build review triage metadata for Vidman REVIEW_REQUIRED rows.")
    parser.add_argument("--dry-run", action="store_true", help="Read-only run; this is the safe default.")
    parser.add_argument("--apply-triage", action="store_true", help="Write only vidman_product_review_queue metadata.")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild review queue metadata when used with --apply-triage.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--canonical-id", type=int)
    parser.add_argument("--tier", choices=sorted(REVIEW_TIERS))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.dry_run and args.apply_triage:
        raise SystemExit("Choose either --dry-run or --apply-triage, not both.")

    _validate_real_database()
    _apply_migration()

    with SessionLocal() as db:
        summary = process_review_triage(
            db,
            limit=args.limit,
            canonical_id=args.canonical_id,
            tier=args.tier,
            apply_triage=args.apply_triage,
            rebuild=args.rebuild,
            verbose=args.verbose,
        )

    print(json.dumps(summary.analytics(), ensure_ascii=False, indent=2, default=str))
    if args.verbose or not args.apply_triage:
        print("\nSamples:")
        print(json.dumps(summary.samples, ensure_ascii=False, indent=2, default=str))
    if not args.apply_triage:
        print("DRY_RUN: no review queue metadata was written.")
    else:
        print("APPLY_TRIAGE: wrote review queue metadata only; no product matches were approved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
