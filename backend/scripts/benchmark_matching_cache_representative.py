"""Synthetic multi-source matching-cache benchmark for a disposable PostgreSQL DB."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import statistics
import sys
import time
import uuid

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import Base
from backend.app.models import (
    CompetitorMatchSnapshot,
    CompetitorPrice,
    CompetitorPriceList,
    CompetitorPriceListItem,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    Product,
)
from backend.app.services import competitor_matching as matching_service
from backend.app.services.competitor_matching import rebuild_competitor_prices_for_selected
from backend.app.services.matching_cache.capture import capture_and_publish_legacy_source_version
from backend.app.services.matching_cache.context import build_source_dependency_vector
from backend.app.services.matching_cache.shadow import build_shadow_snapshot, get_or_create_shadow_snapshot


def run(*, database_url: str, product_count: int, source_count: int, warm_iterations: int) -> dict:
    if product_count < 100 or source_count < 2 or warm_iterations < 3:
        raise ValueError("benchmark requires >=100 products, >=2 sources and >=3 warm iterations")
    admin = create_engine(database_url, poolclass=NullPool)
    with admin.connect() as connection:
        database_name = str(connection.scalar(text("SELECT current_database()")))
    if not database_name.startswith("matchcache_"):
        admin.dispose()
        raise RuntimeError("representative benchmark requires a matchcache_* disposable database")

    schema = f"matchcache_benchmark_{uuid.uuid4().hex}"
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(
        database_url,
        pool_size=2,
        max_overflow=0,
        pool_timeout=5,
        connect_args={"options": f"-csearch_path={schema},public"},
    )
    report: dict = {
        "classification": "isolated_synthetic_multi_source",
        "productionResult": False,
        "database": database_name,
        "productCount": product_count,
        "sourceCount": source_count,
        "expectedMaterializedRows": product_count * source_count,
    }
    original_matcher = matching_service.rematch_price_list_items_by_product
    try:
        Base.metadata.create_all(engine)
        os.environ.update({
            "MATCH_CACHE_ENABLED": "true",
            "MATCH_CACHE_DUAL_WRITE": "true",
            "MATCH_CACHE_READ": "true",
            "MATCH_CACHE_READ_MODE": "required",
            "MATCH_CACHE_READ_ALLOW_ALL": "true",
        })
        snapshot_timings: list[float] = []
        snapshot_ids: list[int] = []
        source_ids: list[int] = []
        with Session(engine) as db:
            price_format = PriceFormat(
                code=f"BENCH-{uuid.uuid4().hex}",
                name="Representative matching-cache benchmark",
                branch="benchmark",
            )
            db.add(price_format)
            products = [
                Product(
                    code=f"BENCH-{index:06d}",
                    name=f"Benchmark medicine {index:06d} 10 mg",
                    cost=Decimal("100.00") + Decimal(index % 50),
                    provisor_goods_id=8_000_000 + index,
                )
                for index in range(product_count)
            ]
            db.add_all(products)
            db.flush()
            for source_index in range(source_count):
                source = CompetitorPriceList(
                    price_format_id=price_format.id,
                    source_type="provisor",
                    source_key=f"benchmark:{source_index}",
                    display_name=f"Benchmark source {source_index}",
                    supplier=f"Benchmark supplier {source_index}",
                    competitor_name=f"Benchmark competitor {source_index}",
                    price_coefficient=Decimal("1.00") + Decimal(source_index) / Decimal("100"),
                    items_count=product_count,
                )
                db.add(source)
                db.flush()
                db.bulk_save_objects([
                    CompetitorPriceListItem(
                        price_list_id=source.id,
                        name=product.name,
                        raw_name=product.name,
                        distributor_goods_name=product.name,
                        distributor_goods_id=f"S{source_index}-{index:06d}",
                        provisor_goods_id=product.provisor_goods_id,
                        distributor_price=Decimal("90.00")
                        + Decimal(source_index * 5)
                        + Decimal(index % 20) / Decimal("10"),
                        raw_manufacturer=f"Manufacturer {index % 20}",
                        raw_json="{}",
                    )
                    for index, product in enumerate(products)
                ])
                db.add(PriceFormatCompetitorAssignment(
                    price_format_id=price_format.id,
                    competitor_price_list_id=source.id,
                    is_active=True,
                ))
                db.flush()
                source_ids.append(int(source.id))

            # The matching context is the complete assigned physical-source
            # cohort. Build only after every assignment exists so the benchmark
            # does not manufacture snapshots that are stale by construction.
            for source_id in source_ids:
                capture = capture_and_publish_legacy_source_version(
                    db=db, competitor_price_list_id=source_id
                )
                vector, _context = build_source_dependency_vector(
                    db=db, competitor_price_list_id=source_id
                )
                snapshot, _ = get_or_create_shadow_snapshot(
                    db=db, source_version_id=int(capture.version_id), vector=vector
                )
                started = time.perf_counter()
                built = build_shadow_snapshot(
                    db=db,
                    snapshot_id=int(snapshot.id),
                    expected_dependency_fingerprint=vector.fingerprint,
                )
                snapshot_timings.append(time.perf_counter() - started)
                snapshot_ids.append(int(built.snapshot_id))
                db.commit()

            matcher_calls = 0

            def forbidden_matcher(**_kwargs):
                nonlocal matcher_calls
                matcher_calls += 1
                raise RuntimeError("cache benchmark invoked the full matcher")

            matching_service.rematch_price_list_items_by_product = forbidden_matcher
            cold_started = time.perf_counter()
            cold = rebuild_competitor_prices_for_selected(
                db=db, price_format_id=int(price_format.id)
            )
            db.commit()
            cold_ms = (time.perf_counter() - cold_started) * 1000
            warm_workflow_ms: list[float] = []
            warm_lookup_ms: list[float] = []
            for _ in range(warm_iterations):
                started = time.perf_counter()
                warm = rebuild_competitor_prices_for_selected(
                    db=db, price_format_id=int(price_format.id)
                )
                db.commit()
                warm_workflow_ms.append((time.perf_counter() - started) * 1000)
                warm_lookup_ms.append(float(warm["_matchingCache"]["cacheLookupDurationMs"]))
            materialized_rows = int(db.scalar(select(func.count(CompetitorPrice.id)).where(
                CompetitorPrice.price_format_id == price_format.id,
                CompetitorPrice.product_id.is_not(None),
            )) or 0)
            matched_snapshot_rows = int(db.scalar(select(func.sum(
                CompetitorMatchSnapshot.matched_row_count
            )).where(CompetitorMatchSnapshot.id.in_(snapshot_ids))) or 0)
            if materialized_rows != product_count * source_count:
                raise RuntimeError(
                    f"representative cardinality failed: {materialized_rows} materialized rows"
                )
            if matcher_calls:
                raise RuntimeError(f"cache path invoked full matcher {matcher_calls} times")
            ordered_workflow = sorted(warm_workflow_ms)
            ordered_lookup = sorted(warm_lookup_ms)
            report.update({
                "matchedSnapshotRows": matched_snapshot_rows,
                "materializedRows": materialized_rows,
                "coldSnapshotConstructionSeconds": {
                    "total": sum(snapshot_timings),
                    "perSource": snapshot_timings,
                },
                "coldWorkflowMilliseconds": cold_ms,
                "coldCacheLookupMilliseconds": cold["_matchingCache"]["cacheLookupDurationMs"],
                "coldMaterializationMilliseconds": cold["_matchingCache"]["materializationDurationMs"],
                "warmWorkflowMilliseconds": {
                    "median": statistics.median(warm_workflow_ms),
                    "p95": ordered_workflow[max(0, int(warm_iterations * 0.95) - 1)],
                },
                "warmCacheLookupMilliseconds": {
                    "median": statistics.median(warm_lookup_ms),
                    "p95": ordered_lookup[max(0, int(warm_iterations * 0.95) - 1)],
                },
                "fullRematchInvocationCountDuringCachePaths": matcher_calls,
            })
        return report
    finally:
        matching_service.rematch_price_list_items_by_product = original_matcher
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.getenv("TEST_POSTGRES_URL", ""))
    parser.add_argument("--products", type=int, default=1000)
    parser.add_argument("--sources", type=int, default=3)
    parser.add_argument("--warm-iterations", type=int, default=10)
    parser.add_argument("--output")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or TEST_POSTGRES_URL is required")
    report = run(
        database_url=args.database_url,
        product_count=args.products,
        source_count=args.sources,
        warm_iterations=args.warm_iterations,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
