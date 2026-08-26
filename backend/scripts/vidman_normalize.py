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
from backend.app.services.vidman_normalization import process_vidman_stage2


MIGRATION_PATH = ROOT / "backend" / "scripts" / "migrations" / "20260822_vidman_stage2_normalization.sql"


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
        raise SystemExit("Refusing Vidman Stage 2 real-data validation on SQLite/backend/app.db.")
    if identity["engine"] != "postgresql":
        raise SystemExit("Refusing Vidman Stage 2 real-data validation on a non-PostgreSQL engine.")
    if identity["host"] not in {"localhost", "127.0.0.1", "::1"} or identity["database"] != "apteka":
        raise SystemExit("Refusing Vidman Stage 2 real-data validation outside host=localhost database=apteka.")


def _apply_migration() -> None:
    if engine.dialect.name != "postgresql":
        return
    statements = [statement.strip() for statement in MIGRATION_PATH.read_text(encoding="utf-8").split(";")]
    with engine.begin() as conn:
        for statement in statements:
            if statement:
                conn.execute(text(statement))


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize raw Vidman rows into safe canonical Vidman products.")
    parser.add_argument("--account-id", type=int)
    parser.add_argument("--main-id", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--only-unprocessed", action="store_true", default=True)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print("VIDMAN NORMALIZATION START", flush=True)
    _validate_real_database()
    _apply_migration()

    with SessionLocal() as db:
        summary = process_vidman_stage2(
            db,
            account_id=args.account_id,
            main_id=args.main_id,
            limit=args.limit,
            only_unprocessed=args.only_unprocessed,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            rebuild=args.rebuild,
            verbose=args.verbose,
        )

    print(f"raw rows total: {summary.raw_rows_total}")
    print(f"processed: {summary.processed}")
    print(f"normalized: {summary.normalized}")
    print(f"auto-linked: {summary.auto_linked}")
    print(f"new canonical products: {summary.new_canonical_products}")
    print(f"existing canonical reused: {summary.existing_canonical_reused}")
    print(f"ambiguous: {summary.ambiguous}")
    print(f"warnings: {summary.warnings}")
    print(f"errors: {summary.errors}")
    print(f"rows/sec: {summary.rows_per_sec:.2f}")
    print(f"elapsed: {summary.elapsed_seconds:.2f}s")

    analytics = summary.analytics
    print("\nFinal summary:")
    print(f"RAW ROWS: {analytics.get('raw_rows_count')}")
    print(f"NORMALIZED: {analytics.get('normalized_rows_count')}")
    print(f"CANONICAL PRODUCTS: {analytics.get('canonical_products_count')}")
    print(f"AUTO-LINKED: {analytics.get('auto_linked')}")
    print(f"UNRESOLVED: {analytics.get('unresolved')}")
    print(f"WARNINGS: {analytics.get('warnings')}")
    print(f"ELAPSED: {summary.elapsed_seconds:.2f}s")
    print("\nAnalytics:")
    print(json.dumps(analytics, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
