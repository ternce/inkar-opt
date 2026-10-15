"""Create a deterministic synthetic source in an explicitly isolated database."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from backend.app.db import Base
from backend.app.models import CompetitorPriceList, CompetitorPriceListItem, PriceFormat, Product


def prepare(*, database_url: str, rows: int) -> dict:
    if rows < 1:
        raise ValueError("rows must be positive")
    engine = create_engine(database_url)
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            existing_sources = int(db.scalar(select(func.count(CompetitorPriceList.id))) or 0)
            existing_items = int(db.scalar(select(func.count(CompetitorPriceListItem.id))) or 0)
            if existing_sources or existing_items:
                raise RuntimeError(
                    "refusing to seed a non-empty database: "
                    f"sources={existing_sources} items={existing_items}"
                )
            price_format = PriceFormat(
                code="PHASE2B3-SYNTHETIC",
                name="Phase 2B.3 synthetic benchmark",
                branch="synthetic",
            )
            product = Product(
                code="SYNTHETIC-SKU",
                name="Synthetic Medicine 100 mg tablets N10",
                cost=100,
            )
            db.add_all([price_format, product])
            db.flush()
            source = CompetitorPriceList(
                price_format_id=price_format.id,
                source_type="manual",
                source_key="phase2b3:synthetic:790k",
                display_name="Phase 2B.3 synthetic source",
                supplier="synthetic",
                competitor_name="synthetic",
            )
            db.add(source)
            db.flush()
            source_id = int(source.id)
            db.execute(text(
                """INSERT INTO competitor_price_list_items
                   (price_list_id, product_id, name, reg_number,
                    distributor_goods_name, distributor_goods_id,
                    distributor_price, stock, package_count, expiry_date,
                    match_key, match_type, matched_sku, raw_name,
                    raw_manufacturer, normalized_name, normalized_manufacturer,
                    parsed_base_name, parsed_form, parsed_forms_json,
                    parsed_strength_signature, parsed_dimensions_json,
                    parsed_critical_tokens_json, raw_json)
                   SELECT :source_id, NULL,
                          'Synthetic Medicine 100 mg tablets N10', '',
                          'Synthetic Medicine 100 mg tablets N10',
                          'SYN-' || lpad(gs::text, 9, '0'),
                          100 + (gs % 100)::numeric / 100, 10, 1, '',
                          '', 'unmatched', '',
                          'Synthetic Medicine 100 mg tablets N10',
                          'Synthetic Factory', '', '', '', '', '', '', '', '', '{}'
                   FROM generate_series(1, :rows) AS gs"""
            ), {"source_id": source_id, "rows": int(rows)})
            source.items_count = rows
            db.commit()
            return {"sourceId": source_id, "rows": rows, "priceFormatId": int(price_format.id)}
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.getenv("TEST_POSTGRES_URL", ""))
    parser.add_argument("--rows", type=int, default=790000)
    parser.add_argument("--confirm-isolated", action="store_true")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or TEST_POSTGRES_URL is required")
    if not args.confirm_isolated:
        parser.error("--confirm-isolated is required; the target must contain no business data")
    print(prepare(database_url=args.database_url, rows=args.rows))


if __name__ == "__main__":
    main()
