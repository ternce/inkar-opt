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
from backend.app.services.internal_product_normalization import (
    audit_internal_products,
    process_internal_product_normalization,
)


MIGRATION_PATH = ROOT / "backend" / "scripts" / "migrations" / "20260823_vidman_stage3_1_internal_product_normalization.sql"


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
        raise SystemExit("Refusing internal product normalization on SQLite/backend/app.db.")
    if identity["engine"] != "postgresql":
        raise SystemExit("Refusing internal product normalization on a non-PostgreSQL engine.")
    if identity["host"] not in {"localhost", "127.0.0.1", "::1"} or identity["database"] != "apteka":
        raise SystemExit("Refusing internal product normalization outside host=localhost database=apteka.")


def _apply_migration() -> None:
    statements = [statement.strip() for statement in MIGRATION_PATH.read_text(encoding="utf-8").split(";")]
    with engine.begin() as conn:
        for statement in statements:
            if statement:
                conn.execute(text(statement))


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize internal products into deterministic Vidman-compatible identities.")
    parser.add_argument("--dry-run", action="store_true", help="Read-only run; this is the safe default.")
    parser.add_argument("--rebuild", action="store_true", help="Write/rebuild internal_product_normalized.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--product-id", type=int)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()

    if args.dry_run and args.rebuild:
        raise SystemExit("Choose either --dry-run or --rebuild, not both.")

    _validate_real_database()
    _apply_migration()

    dry_run = not args.rebuild
    with SessionLocal() as db:
        audit = audit_internal_products(db)
        print("\nInternal product audit:")
        print(f"TOTAL_PRODUCTS={audit.total_products}")
        print(f"PRODUCTS_WITH_MANUFACTURER={audit.products_with_manufacturer}")
        print(f"PRODUCTS_WITHOUT_MANUFACTURER={audit.products_without_manufacturer}")
        print(f"PRODUCTS_WITH_PROVISOR_GOODS_ID={audit.products_with_provisor_goods_id}")
        print(f"PRODUCTS_WITHOUT_PROVISOR_GOODS_ID={audit.products_without_provisor_goods_id}")
        print("PATTERN_COUNTS=" + json.dumps(audit.pattern_counts, ensure_ascii=False, sort_keys=True))

        if args.audit_only:
            print("AUDIT_ONLY: no normalization was run.")
            return 0

        summary = process_internal_product_normalization(
            db,
            dry_run=dry_run,
            rebuild=args.rebuild,
            limit=args.limit,
            product_id=args.product_id,
            batch_size=args.batch_size,
            verbose=args.verbose,
        )

    print("\nInternal normalization summary:")
    print(json.dumps(summary.metrics(), ensure_ascii=False, indent=2, default=str))
    if args.verbose or args.dry_run or not args.rebuild:
        print("\nSample rows:")
        print(json.dumps(summary.sample_rows[:20], ensure_ascii=False, indent=2, default=str))
    if dry_run:
        print("DRY_RUN: no internal normalized rows were written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
