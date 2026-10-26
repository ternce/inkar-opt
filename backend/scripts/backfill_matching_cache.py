"""Prepare matching-cache artifacts from existing competitor source rows.

Dry-run is read-only.  ``--apply`` commits one source at a time so an
interrupted invocation can be repeated without duplicating immutable versions,
snapshots, or jobs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import SessionLocal
from backend.app.models import (
    CompetitorPriceList,
    PriceFormat,
    PriceFormatCompetitorAssignment,
)
from backend.app.services.matching_cache.backfill import (
    backfill_existing_source,
    plan_existing_source,
)
from backend.app.services.matching_cache.dependency_tracking import (
    assert_database_dependency_tracking,
)


def _source_ids(value: str) -> list[int]:
    result: list[int] = []
    for token in str(value or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            source_id = int(token)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("source IDs must be integers") from exc
        if source_id <= 0:
            raise argparse.ArgumentTypeError("source IDs must be positive")
        if source_id not in result:
            result.append(source_id)
    if not result:
        raise argparse.ArgumentTypeError("at least one source ID is required")
    return result


def _emit(event: str, **payload: object) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False, default=str))


def _validate_scope(db, *, source_ids: list[int], price_format_id: int | None) -> None:
    existing = set(
        db.scalars(
            select(CompetitorPriceList.id).where(CompetitorPriceList.id.in_(source_ids))
        )
    )
    missing = sorted(set(source_ids) - {int(value) for value in existing})
    if missing:
        raise ValueError(f"competitor sources do not exist: {missing}")
    if price_format_id is None:
        return
    if db.get(PriceFormat, int(price_format_id)) is None:
        raise ValueError(f"price format does not exist: {price_format_id}")
    assigned = set(
        db.scalars(
            select(PriceFormatCompetitorAssignment.competitor_price_list_id).where(
                PriceFormatCompetitorAssignment.price_format_id == int(price_format_id),
                PriceFormatCompetitorAssignment.competitor_price_list_id.in_(source_ids),
                PriceFormatCompetitorAssignment.is_active.is_(True),
            )
        )
    )
    outside = sorted(set(source_ids) - {int(value) for value in assigned})
    if outside:
        raise ValueError(
            f"sources are not active assignments of price format {price_format_id}: {outside}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-ids", required=True, type=_source_ids)
    parser.add_argument("--price-format-id", type=int)
    parser.add_argument("--batch-size", type=int, default=2000)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.price_format_id is not None and args.price_format_id <= 0:
        parser.error("--price-format-id must be positive")
    if not 100 <= args.batch_size <= 10000:
        parser.error("--batch-size must be between 100 and 10000")

    started = time.perf_counter()
    processed = 0
    with SessionLocal() as db:
        _validate_scope(
            db,
            source_ids=args.source_ids,
            price_format_id=args.price_format_id,
        )
        if args.apply:
            if db.get_bind().dialect.name != "postgresql":
                raise RuntimeError("write-enabled matching-cache backfill requires PostgreSQL")
            assert_database_dependency_tracking(db)
        db.rollback()

        for source_id in args.source_ids:
            source_started = time.perf_counter()
            try:
                if args.dry_run:
                    result = plan_existing_source(
                        db=db,
                        competitor_price_list_id=source_id,
                        batch_size=args.batch_size,
                    ).as_dict()
                    db.rollback()
                else:
                    result = backfill_existing_source(
                        db=db,
                        competitor_price_list_id=source_id,
                        price_format_id=args.price_format_id,
                        batch_size=args.batch_size,
                    ).as_dict()
                    db.commit()
                processed += 1
                _emit(
                    "matching_cache_backfill_source",
                    mode="dry-run" if args.dry_run else "apply",
                    durationSeconds=round(time.perf_counter() - source_started, 6),
                    **result,
                )
            except Exception as exc:
                db.rollback()
                _emit(
                    "matching_cache_backfill_failed",
                    sourceId=source_id,
                    error=str(exc),
                    processed=processed,
                )
                return 1
    _emit(
        "matching_cache_backfill_complete",
        mode="dry-run" if args.dry_run else "apply",
        sourceCount=processed,
        durationSeconds=round(time.perf_counter() - started, 6),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
