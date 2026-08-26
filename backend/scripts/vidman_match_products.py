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
from backend.app.services.vidman_product_matching import process_vidman_product_matches


MIGRATION_PATH = ROOT / "backend" / "scripts" / "migrations" / "20260823_vidman_stage3_product_matches.sql"


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
        raise SystemExit("Refusing Vidman Stage 3 real-data matching on SQLite/backend/app.db.")
    if identity["engine"] != "postgresql":
        raise SystemExit("Refusing Vidman Stage 3 real-data matching on a non-PostgreSQL engine.")
    if identity["host"] not in {"localhost", "127.0.0.1", "::1"} or identity["database"] != "apteka":
        raise SystemExit("Refusing Vidman Stage 3 real-data matching outside host=localhost database=apteka.")


def _apply_migration() -> None:
    statements = [statement.strip() for statement in MIGRATION_PATH.read_text(encoding="utf-8").split(";")]
    with engine.begin() as conn:
        for statement in statements:
            if statement:
                conn.execute(text(statement))


def main() -> int:
    parser = argparse.ArgumentParser(description="Match frozen Vidman canonical products to internal products.")
    parser.add_argument("--dry-run", action="store_true", help="Read-only run; this is the safe default.")
    parser.add_argument("--apply", action="store_true", help="Persist automatic mappings and review candidates.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--canonical-id", type=int)
    parser.add_argument("--only-unmatched", action="store_true")
    parser.add_argument("--rebuild-auto", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.apply and args.dry_run:
        raise SystemExit("Choose either --dry-run or --apply, not both.")

    _validate_real_database()
    if args.apply:
        _apply_migration()

    with SessionLocal() as db:
        summary = process_vidman_product_matches(
            db,
            limit=args.limit,
            canonical_id=args.canonical_id,
            only_unmatched=args.only_unmatched,
            apply=args.apply,
            rebuild_auto=args.rebuild_auto,
            verbose=args.verbose,
        )

    print(json.dumps(summary.analytics(), ensure_ascii=False, indent=2, default=str))
    if not args.apply:
        print("DRY_RUN: no mappings or candidates were written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
