"""Benchmark Phase 2B.4 cache materialization in an isolated PostgreSQL DB.

The source/version/snapshot are treated as immutable inputs.  The script may
create the missing active assignment and publishes competitor-price
materializations for the source's existing price format.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import Session

from backend.app.models import (
    CompetitorMatchSnapshot,
    CompetitorPrice,
    CompetitorPriceList,
    CompetitorSourcePublication,
    CompetitorSourceVersion,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    PriceFormatCompetitorMaterialization,
)
from backend.app.services import competitor_matching as matching_service
from backend.app.services.competitor_matching import rebuild_competitor_prices_for_selected
from backend.scripts.benchmark_matching_cache import (
    _POSTGRES_LOAD_FIELDS,
    _peak_rss_sampler,
    _postgres_load_sample,
)


def _relation_bytes(db: Session, relation: str) -> dict[str, int]:
    return {
        "table": int(db.scalar(text("SELECT pg_table_size(CAST(:name AS regclass))"), {"name": relation}) or 0),
        "indexes": int(db.scalar(text("SELECT pg_indexes_size(CAST(:name AS regclass))"), {"name": relation}) or 0),
        "total": int(db.scalar(text("SELECT pg_total_relation_size(CAST(:name AS regclass))"), {"name": relation}) or 0),
    }


def _wal_lsn(db: Session) -> str:
    return str(db.scalar(text("SELECT pg_current_wal_lsn()")))


def _wal_delta(db: Session, before: str, after: str) -> int:
    return int(db.scalar(text("SELECT pg_wal_lsn_diff(CAST(:after AS pg_lsn), CAST(:before AS pg_lsn))"), {"before": before, "after": after}) or 0)


def run(
    *,
    database_url: str,
    source_id: int,
    expected_rows: int,
    warm_iterations: int,
    target_format_code: str | None,
) -> dict:
    os.environ["MATCH_CACHE_ENABLED"] = "true"
    os.environ["MATCH_CACHE_DUAL_WRITE"] = "true"
    os.environ["MATCH_CACHE_SHADOW_ENABLED"] = "false"
    os.environ["MATCH_CACHE_READ"] = "true"
    os.environ["MATCH_CACHE_READ_MODE"] = "required"

    engine = create_engine(database_url, pool_size=2, max_overflow=0, pool_timeout=5)
    pool = {"checkedOut": 0, "peakCheckedOut": 0, "totalCheckouts": 0}
    sql = {"rawSourceSelects": 0, "statements": 0}

    @event.listens_for(engine, "checkout")
    def _checkout(*_args) -> None:
        pool["checkedOut"] += 1
        pool["totalCheckouts"] += 1
        pool["peakCheckedOut"] = max(pool["peakCheckedOut"], pool["checkedOut"])

    @event.listens_for(engine, "checkin")
    def _checkin(*_args) -> None:
        pool["checkedOut"] = max(0, pool["checkedOut"] - 1)

    @event.listens_for(engine, "before_cursor_execute")
    def _observe(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        sql["statements"] += 1
        normalized = str(statement).casefold()
        if normalized.lstrip().startswith("select") and "competitor_price_list_items" in normalized:
            sql["rawSourceSelects"] += 1

    matcher_calls = 0
    original_matcher = matching_service.rematch_price_list_items_by_product

    def _forbidden_matcher(**_kwargs):
        nonlocal matcher_calls
        matcher_calls += 1
        raise RuntimeError("full matcher invoked during cache-hit benchmark")

    matching_service.rematch_price_list_items_by_product = _forbidden_matcher
    result: dict = {
        "classification": "synthetic-790k-cache-hit",
        "sourceId": source_id,
        "expectedSourceRows": expected_rows,
        "warmIterations": warm_iterations,
    }
    try:
        with Session(engine) as db:
            source = db.get(CompetitorPriceList, source_id)
            if source is None:
                raise RuntimeError(f"source {source_id} does not exist")
            if int(source.items_count or 0) != expected_rows:
                raise RuntimeError(f"expected {expected_rows} declared rows, found {source.items_count}")
            origin_format = db.get(PriceFormat, int(source.price_format_id))
            if origin_format is None:
                raise RuntimeError("source price format does not exist")
            target_format = origin_format
            if target_format_code:
                target_format = db.scalar(
                    select(PriceFormat).where(PriceFormat.code == target_format_code)
                )
                if target_format is None:
                    target_format = PriceFormat(
                        code=target_format_code,
                        name=f"Phase 2B.4 cache benchmark {target_format_code}",
                        branch=origin_format.branch,
                        reference_branch_id=origin_format.reference_branch_id,
                        competitor_price_mode=origin_format.competitor_price_mode,
                        percentile_number=origin_format.percentile_number,
                        progib=origin_format.progib,
                    )
                    db.add(target_format)
                    db.flush()
            price_format_id = int(target_format.id)
            version = db.scalar(select(CompetitorSourceVersion).where(
                CompetitorSourceVersion.competitor_price_list_id == source_id,
                CompetitorSourceVersion.status == "published",
            ))
            if version is None or int(version.row_count) != expected_rows:
                raise RuntimeError("complete published 790k source version is required")
            snapshot = db.scalar(select(CompetitorMatchSnapshot).where(
                CompetitorMatchSnapshot.source_version_id == int(version.id),
                CompetitorMatchSnapshot.status.in_(("ready", "published")),
            ).order_by(CompetitorMatchSnapshot.id.desc()).limit(1))
            if snapshot is None or int(snapshot.input_row_count) != expected_rows:
                raise RuntimeError("complete ready 790k matching snapshot is required")
            source_version_id = int(version.id)
            snapshot_id = int(snapshot.id)
            matched_snapshot_rows = int(snapshot.matched_row_count)
            publication = db.scalar(select(CompetitorSourcePublication).where(
                CompetitorSourcePublication.competitor_price_list_id == source_id,
                CompetitorSourcePublication.status == "published",
            ))
            if (
                publication is None
                or int(publication.source_version_id) != source_version_id
                or int(publication.row_count) != expected_rows
            ):
                raise RuntimeError("complete current physical source lineage is required")
            source_publication_id = int(publication.id)
            assignment = db.scalar(select(PriceFormatCompetitorAssignment).where(
                PriceFormatCompetitorAssignment.price_format_id == price_format_id,
                PriceFormatCompetitorAssignment.competitor_price_list_id == source_id,
            ))
            if assignment is None:
                db.add(PriceFormatCompetitorAssignment(
                    price_format_id=price_format_id,
                    competitor_price_list_id=source_id,
                    is_active=True,
                ))
            else:
                assignment.is_active = True
            db.commit()

        with engine.connect() as connection:
            load_before = _postgres_load_sample(connection)
        wall_started = time.perf_counter()
        cpu_started = time.process_time()
        with Session(engine) as db, _peak_rss_sampler() as memory:
            connections_before = int(db.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"
            )) or 0)
            storage_before = {
                name: _relation_bytes(db, name)
                for name in (
                    "competitors_prices",
                    "price_format_competitor_materializations",
                    "competitor_source_publications",
                    "competitor_source_publication_items",
                )
            }
            wal_before = _wal_lsn(db)
            cold_started = time.perf_counter()
            cold = rebuild_competitor_prices_for_selected(
                db=db, price_format_id=price_format_id
            )
            db.commit()
            cold_seconds = time.perf_counter() - cold_started
            wal_after_cold = _wal_lsn(db)
            cold_wal_bytes = _wal_delta(db, wal_before, wal_after_cold)

            warm_ms: list[float] = []
            warm_lookup_ms: list[float] = []
            for _ in range(warm_iterations):
                started = time.perf_counter()
                warm = rebuild_competitor_prices_for_selected(
                    db=db, price_format_id=price_format_id
                )
                db.commit()
                warm_ms.append((time.perf_counter() - started) * 1000)
                warm_lookup_ms.append(float(warm["_matchingCache"]["cacheLookupDurationMs"]))
                if not warm["_matchingCache"]["materializationReused"]:
                    raise RuntimeError("warm materialization was unexpectedly rebuilt")

            storage_after = {
                name: _relation_bytes(db, name)
                for name in storage_before
            }
            materialization = db.scalar(select(PriceFormatCompetitorMaterialization).where(
                PriceFormatCompetitorMaterialization.price_format_id == price_format_id,
                PriceFormatCompetitorMaterialization.is_current.is_(True),
            ))
            active_rows = int(db.scalar(select(func.count(CompetitorPrice.id)).where(
                CompetitorPrice.price_format_id == price_format_id,
                CompetitorPrice.product_id.is_not(None),
            )) or 0)
            connections_after = int(db.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"
            )) or 0)
            materialization_id = int(materialization.id)
            db.execute(text("SELECT pg_stat_force_next_flush()"))
            db.commit()

        total_seconds = time.perf_counter() - wall_started
        with engine.connect() as connection:
            load_after = _postgres_load_sample(connection)
        cold_cache = cold["_matchingCache"]
        result.update({
            "priceFormatId": price_format_id,
            "sourceVersionId": source_version_id,
            "sourcePublicationId": source_publication_id,
            "snapshotId": snapshot_id,
            "matchedSnapshotRows": matched_snapshot_rows,
            "activeMaterializedRows": active_rows,
            "cold": {
                "workflowSeconds": cold_seconds,
                "lookupMilliseconds": cold_cache["cacheLookupDurationMs"],
                "materializationMilliseconds": cold_cache["materializationDurationMs"],
                "materializationId": materialization_id,
                "fingerprint": cold_cache["materializationFingerprint"],
                "walBytes": cold_wal_bytes,
            },
            "warm": {
                "iterations": warm_iterations,
                "workflowMillisecondsMedian": statistics.median(warm_ms),
                "workflowMillisecondsP95": sorted(warm_ms)[max(0, int(warm_iterations * 0.95) - 1)],
                "lookupMillisecondsMedian": statistics.median(warm_lookup_ms),
                "lookupMillisecondsP95": sorted(warm_lookup_ms)[max(0, int(warm_iterations * 0.95) - 1)],
            },
            "fullMatcherCallCount": matcher_calls,
            "rawSourceSelectCount": sql["rawSourceSelects"],
            "sqlStatementCount": sql["statements"],
            "processRssBytes": memory,
            "totalSeconds": total_seconds,
            "processCpuSeconds": time.process_time() - cpu_started,
            "databaseConnectionsBefore": connections_before,
            "databaseConnectionsAfter": connections_after,
            "applicationPool": {**pool, "configuredSize": 2, "configuredMaxOverflow": 0},
            "storageBefore": storage_before,
            "storageAfter": storage_after,
            "postgresLoadDelta": {
                field: max(0, load_after[field] - load_before[field])
                for field in _POSTGRES_LOAD_FIELDS
            },
        })
        return result
    finally:
        matching_service.rematch_price_list_items_by_product = original_matcher
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.getenv("TEST_POSTGRES_URL", ""))
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--expected-rows", type=int, default=790_000)
    parser.add_argument("--warm-iterations", type=int, default=25)
    parser.add_argument("--target-format-code")
    parser.add_argument("--output")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or TEST_POSTGRES_URL is required")
    report = run(
        database_url=args.database_url,
        source_id=args.source_id,
        expected_rows=args.expected_rows,
        warm_iterations=args.warm_iterations,
        target_format_code=args.target_format_code,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
