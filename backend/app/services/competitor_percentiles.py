from __future__ import annotations

from collections import defaultdict
import json
import logging
import os
import time
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import Session

from ..models import (
    CompetitorPriceList,
    CompetitorPriceListItem,
    CompetitorPricePercentile,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    Product,
    RegularCompetitorPricePercentile,
)
from ..timezone import now_kz_naive
from .competitor_assignments import get_assigned_competitor_price_lists
from .competitor_read_models import (
    refresh_emit_percentile_source_summaries,
    refresh_regular_percentile_source_summaries,
)
from .competitor_source_config import (
    MULTI_PRICE_PERCENTILE_MODE,
    canonical_competitor_source_key,
    default_percentile_mode_for_source,
    effective_percentile_mode,
)
from .competitors.identity import (
    canonical_regular_competitor_identity,
    normalize_regular_competitor_text,
    regular_competitor_alias_obsolete_identities,
    regular_competitor_display_name,
)


logger = logging.getLogger(__name__)


PERCENTILES = (10, 20, 30, 40, 60)
DEFAULT_BRANCH = "Без филиала"
REGIONAL_SCOPE = "regional"
KAZAKHSTAN_SCOPE = "kazakhstan"
REGULAR_COMPETITOR_SCOPE = "regular_competitor"
KAZAKHSTAN_REGION = "Kazakhstan"
STATUS_CALCULATED = "Calculated"
STATUS_ONE_PRICE = "Calculated from one price"
STATUS_NO_DATA = "No data"
DEFAULT_TRACE_SKU = "163571"
REGULAR_PERCENTILE_ALGORITHM_VERSION = "percentile_inc_v1"
INACTIVE_REFRESH_STATUSES = {"failed", "error", "stale"}


def _as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except Exception:
        return None
    return dec if dec > 0 else None


def percentile_inc_linear(values: list[Decimal], percentile: int) -> Decimal | None:
    """Excel PERCENTILE/PERCENTILE.INC compatible linear interpolation.

    `percentile` is passed as 10, 20, ... and converted to k=0.10, 0.20, ...
    before applying the inclusive `(n - 1) * k` rank used by Excel and NumPy's
    default `method="linear"`.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = Decimal(percentile) / Decimal(100)
    pos = k * Decimal(len(ordered) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = pos - Decimal(lower)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _percentile(values: list[Decimal], percentile: int) -> Decimal:
    value = percentile_inc_linear(values, percentile)
    if value is None:
        raise ValueError("percentile requires at least one value")
    return value


def _branch_name(price_list: CompetitorPriceList) -> str:
    return (price_list.branch_name or price_list.region or "").strip() or DEFAULT_BRANCH


def _competitor_name(price_list: CompetitorPriceList) -> str:
    competitor = (
        price_list.competitor_name
        or price_list.supplier
        or price_list.display_name
        or price_list.source_type
        or ""
    )
    return competitor.strip()


def regular_competitor_identity(price_list: CompetitorPriceList) -> str:
    return canonical_regular_competitor_identity(
        price_list.competitor_name,
        supplier_name=price_list.supplier,
        display_name=price_list.display_name,
    )


def _stored_regular_competitor_identity(price_list: CompetitorPriceList) -> str:
    return normalize_regular_competitor_text(_competitor_name(price_list))


def _legacy_regular_competitor_identity(price_list: CompetitorPriceList) -> str:
    return " ".join(_competitor_name(price_list).strip().casefold().split())


def _is_emit_price_list(price_list: CompetitorPriceList) -> bool:
    source_key = _source_key(price_list)
    if source_key.startswith("emit:"):
        return True
    if str(price_list.source_type or "").strip().casefold() == "emit":
        return True
    return default_percentile_mode_for_source(price_list) == MULTI_PRICE_PERCENTILE_MODE


def _regular_price_list_is_usable(price_list: CompetitorPriceList) -> bool:
    if _is_emit_price_list(price_list):
        return False
    if not regular_competitor_identity(price_list):
        return False
    status = str(price_list.last_refresh_status or "").strip().casefold()
    return status not in INACTIVE_REFRESH_STATUSES


def _status_for_values(values: list[Decimal]) -> str:
    if len(values) == 1:
        return STATUS_ONE_PRICE
    if values:
        return STATUS_CALCULATED
    return STATUS_NO_DATA


def _matched_positive_counts_by_price_list(*, db: Session, price_list_ids: list[int]) -> dict[int, int]:
    if not price_list_ids:
        return {}
    rows = db.execute(
        select(CompetitorPriceListItem.price_list_id, func.count())
        .where(CompetitorPriceListItem.price_list_id.in_(price_list_ids))
        .where(CompetitorPriceListItem.distributor_price.is_not(None))
        .where(CompetitorPriceListItem.distributor_price > 0)
        .where(
            (CompetitorPriceListItem.product_id.is_not(None))
            | (CompetitorPriceListItem.provisor_goods_id.is_not(None))
            | (CompetitorPriceListItem.matched_sku != "")
            | (CompetitorPriceListItem.distributor_goods_id != "")
        )
        .group_by(CompetitorPriceListItem.price_list_id)
    ).all()
    return {int(price_list_id): int(count or 0) for price_list_id, count in rows}


def eligible_percentile_assignments(*, db: Session, price_format_id: int, require_matched_prices: bool = True):
    assigned = [
        item
        for item in get_assigned_competitor_price_lists(db=db, price_format_id=price_format_id)
        if effective_percentile_mode(item.price_list, item.assignment.percentile_mode) == MULTI_PRICE_PERCENTILE_MODE
        and canonical_competitor_source_key(item.price_list)
    ]
    if not require_matched_prices:
        return assigned
    counts = _matched_positive_counts_by_price_list(db=db, price_list_ids=[int(item.price_list.id) for item in assigned])
    return [item for item in assigned if int(counts.get(int(item.price_list.id), 0)) > 0]


def emit_percentile_assignments(*, db: Session, price_format_id: int):
    return [
        item
        for item in eligible_percentile_assignments(db=db, price_format_id=price_format_id, require_matched_prices=False)
        if _is_emit_price_list(item.price_list)
    ]


def _source_key(price_list: CompetitorPriceList) -> str:
    return canonical_competitor_source_key(price_list)


def _percentile_source_type(price_list: CompetitorPriceList) -> str:
    source_key = _source_key(price_list)
    if source_key.startswith("emit:"):
        return "emit"
    return str(price_list.source_type or "").strip()


def _kazakhstan_source_key(competitor: str) -> str:
    return f"emit:kazakhstan:{competitor}"


def emit_percentile_group_keys(*, db: Session, price_format_id: int) -> set[tuple[str, str, str]]:
    return {
        (_branch_name(item.price_list), _competitor_name(item.price_list), _source_key(item.price_list))
        for item in emit_percentile_assignments(db=db, price_format_id=price_format_id)
    }


def _trace_sku() -> str:
    return str(os.getenv("EMIT_TRACE_SKU", DEFAULT_TRACE_SKU) or "").strip()


def recalculate_competitor_percentiles(
    *,
    db: Session,
    price_format_id: int,
    source_price_list_ids: list[int] | None = None,
) -> dict[str, Any]:
    if (db.get_bind().dialect.name or "").lower() == "postgresql":
        summary = _recalculate_competitor_percentiles_postgresql(
            db=db,
            price_format_id=price_format_id,
            source_price_list_ids=source_price_list_ids,
        )
    else:
        summary = _recalculate_competitor_percentiles_python(
            db=db,
            price_format_id=price_format_id,
            source_price_list_ids=source_price_list_ids,
        )
    refresh_emit_percentile_source_summaries(db=db, price_format_id=price_format_id)
    return summary


def fanout_emit_percentiles_from_price_format(
    *,
    db: Session,
    source_price_format_id: int,
    target_price_format_id: int,
    source_price_list_ids: list[int],
) -> dict[str, Any]:
    """Copy already-calculated Emit percentile rows to another assigned format.

    This preserves the existing compatibility storage shape: readers still use
    price_format_id-specific rows, while Emit's expensive raw-price aggregation
    can run once per source refresh.
    """
    started_at = time.perf_counter()
    selected_sources = _selected_source_rows(
        db=db,
        price_format_id=target_price_format_id,
        source_price_list_ids=source_price_list_ids,
    )
    if not selected_sources:
        return _skip_summary(target_price_format_id) | {
            "engine": "fanout",
            "source_price_format_id": source_price_format_id,
            "compatibility_rows_created": 0,
        }

    dialect = (db.get_bind().dialect.name or "").lower()
    if dialect == "postgresql":
        summary = _fanout_emit_percentiles_postgresql(
            db=db,
            source_price_format_id=source_price_format_id,
            target_price_format_id=target_price_format_id,
            selected_sources=selected_sources,
            started_at=started_at,
        )
    else:
        summary = _fanout_emit_percentiles_python(
            db=db,
            source_price_format_id=source_price_format_id,
            target_price_format_id=target_price_format_id,
            selected_sources=selected_sources,
            started_at=started_at,
        )
    refresh_emit_percentile_source_summaries(db=db, price_format_id=target_price_format_id)
    return summary


def _selected_source_rows(
    *,
    db: Session,
    price_format_id: int,
    source_price_list_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    selected = eligible_percentile_assignments(db=db, price_format_id=price_format_id)
    scoped_ids = {int(item) for item in (source_price_list_ids or []) if int(item) > 0}
    if scoped_ids:
        selected = [item for item in selected if int(item.price_list.id) in scoped_ids]

    rows_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in selected:
        price_list = item.price_list
        branch = _branch_name(price_list)
        competitor = _competitor_name(price_list)
        source_key = _source_key(price_list)
        key = (branch, competitor, source_key)
        existing = rows_by_key.get(key)
        price_list_id = int(price_list.id)
        if existing is not None and int(existing["price_list_id"]) <= price_list_id:
            continue
        rows_by_key[key] = {
            "price_list_id": price_list_id,
            "branch_name": branch,
            "competitor_name": competitor,
            "source_key": source_key,
            "source_type": _percentile_source_type(price_list),
            "filial_id": str(price_list.branch_id or price_list.external_price_list_id or ""),
            "source_type_raw": str(price_list.source_type or ""),
        }
    return sorted(rows_by_key.values(), key=lambda row: (row["branch_name"], row["competitor_name"], row["source_key"], row["price_list_id"]))


def _skip_summary(price_format_id: int) -> dict[str, Any]:
    logger.info(
        "[PERCENTILE_MUTATION] action=skip reason=%s price_format_id=%s source_price_list_id=%s "
        "source_type=%s percentile_mode=%s rows_before=%s rows_deleted=%s rows_inserted=%s",
        "No eligible percentile source assigned; percentile rebuild skipped.",
        price_format_id,
        "",
        "",
        "",
        0,
        0,
        0,
    )
    return {
        "products_processed": 0,
        "products_with_competitors": 0,
        "products_without_competitors": 0,
        "rows_created": 0,
        "rows_updated": 0,
        "rows_skipped": 1,
        "rows_deleted": 0,
        "message": "No eligible percentile source assigned; percentile rebuild skipped.",
    }


def _source_scope_filter(selected_sources: list[dict[str, Any]]):
    regional_filters = [
        (
            (
                (func.coalesce(CompetitorPricePercentile.source_key, "") == str(source["source_key"] or ""))
                | (
                    (func.coalesce(CompetitorPricePercentile.source_key, "") == "")
                    & (CompetitorPricePercentile.branch_name == str(source["branch_name"] or ""))
                    & (CompetitorPricePercentile.competitor_name == str(source["competitor_name"] or ""))
                )
            )
            & (CompetitorPricePercentile.percentile_scope == REGIONAL_SCOPE)
        )
        for source in selected_sources
    ]
    kazakhstan_filters = [
        (
            (CompetitorPricePercentile.branch_name == KAZAKHSTAN_REGION)
            & (CompetitorPricePercentile.competitor_name == competitor)
            & (CompetitorPricePercentile.percentile_scope == KAZAKHSTAN_SCOPE)
        )
        for competitor in sorted({str(source["competitor_name"] or "") for source in selected_sources})
    ]
    return or_(*(regional_filters + kazakhstan_filters))


def _fanout_emit_percentiles_python(
    *,
    db: Session,
    source_price_format_id: int,
    target_price_format_id: int,
    selected_sources: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    scoped_filter = _source_scope_filter(selected_sources)
    existing_rows = int(
        db.execute(
            select(func.count(CompetitorPricePercentile.id))
            .where(CompetitorPricePercentile.price_format_id == target_price_format_id)
            .where(scoped_filter)
        ).scalar_one()
        or 0
    )
    deleted_rows = int(
        db.execute(
            delete(CompetitorPricePercentile)
            .where(CompetitorPricePercentile.price_format_id == target_price_format_id)
            .where(scoped_filter)
        ).rowcount
        or 0
    )
    source_rows = (
        db.execute(
            select(CompetitorPricePercentile)
            .where(CompetitorPricePercentile.price_format_id == source_price_format_id)
            .where(scoped_filter)
            .order_by(
                CompetitorPricePercentile.product_id,
                CompetitorPricePercentile.source_key,
                CompetitorPricePercentile.branch_name,
                CompetitorPricePercentile.competitor_name,
                CompetitorPricePercentile.percentile_scope,
                CompetitorPricePercentile.percentile,
            )
        )
        .scalars()
        .all()
    )
    mappings = [
        {
            "price_format_id": target_price_format_id,
            "product_id": row.product_id,
            "competitor_price_list_id": row.competitor_price_list_id,
            "source_type": row.source_type,
            "source_key": row.source_key,
            "branch_name": row.branch_name,
            "competitor_name": row.competitor_name,
            "percentile_scope": row.percentile_scope,
            "percentile": row.percentile,
            "value": row.value,
            "source_count": row.source_count,
            "price_count": row.price_count,
            "used_price_count": row.used_price_count,
            "status": row.status,
            "updated_at": row.updated_at,
        }
        for row in source_rows
    ]
    if mappings:
        db.bulk_insert_mappings(CompetitorPricePercentile, mappings)

    products_processed = int(
        db.execute(select(func.count(Product.id))).scalar_one()
        or 0
    )
    products_with_competitors = {
        int(row.product_id)
        for row in source_rows
        if row.percentile_scope == REGIONAL_SCOPE and row.value is not None
    }
    inserted = len(mappings)
    summary = {
        "products_processed": products_processed,
        "products_with_competitors": len(products_with_competitors),
        "products_without_competitors": max(0, products_processed - len(products_with_competitors)),
        "rows_created": inserted,
        "rows_updated": 0,
        "rows_skipped": 0,
        "rows_deleted": deleted_rows,
        "execution_time_seconds": round(time.perf_counter() - started_at, 3),
        "engine": "fanout_python",
        "source_price_format_id": source_price_format_id,
        "rows_before": existing_rows,
        "compatibility_rows_created": inserted,
    }
    return summary


def _fanout_emit_percentiles_postgresql(
    *,
    db: Session,
    source_price_format_id: int,
    target_price_format_id: int,
    selected_sources: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    db.execute(text("DROP TABLE IF EXISTS tmp_emit_percentile_fanout_sources"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE tmp_emit_percentile_fanout_sources (
                price_list_id BIGINT PRIMARY KEY,
                branch_name TEXT NOT NULL,
                competitor_name TEXT NOT NULL,
                source_key TEXT NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    db.execute(
        text(
            """
            INSERT INTO tmp_emit_percentile_fanout_sources (
                price_list_id,
                branch_name,
                competitor_name,
                source_key
            )
            VALUES (
                :price_list_id,
                :branch_name,
                :competitor_name,
                :source_key
            )
            """
        ),
        [
            {
                "price_list_id": source["price_list_id"],
                "branch_name": source["branch_name"],
                "competitor_name": source["competitor_name"],
                "source_key": source["source_key"],
            }
            for source in selected_sources
        ],
    )
    scope_sql = """
        (
          cpp.percentile_scope = :regional_scope
          AND EXISTS (
            SELECT 1
            FROM tmp_emit_percentile_fanout_sources s
            WHERE (
                cpp.source_key = s.source_key
                OR (
                    coalesce(cpp.source_key, '') = ''
                    AND cpp.branch_name = s.branch_name
                    AND cpp.competitor_name = s.competitor_name
                )
            )
          )
        )
        OR (
          cpp.percentile_scope = :kazakhstan_scope
          AND cpp.branch_name = :kazakhstan_region
          AND EXISTS (
            SELECT 1
            FROM tmp_emit_percentile_fanout_sources s
            WHERE cpp.competitor_name = s.competitor_name
          )
        )
    """
    params = {
        "source_price_format_id": source_price_format_id,
        "target_price_format_id": target_price_format_id,
        "regional_scope": REGIONAL_SCOPE,
        "kazakhstan_scope": KAZAKHSTAN_SCOPE,
        "kazakhstan_region": KAZAKHSTAN_REGION,
    }
    existing_rows = int(
        db.execute(
            text(
                f"""
                SELECT count(cpp.id)
                FROM competitor_price_percentiles cpp
                WHERE cpp.price_format_id = :target_price_format_id
                  AND ({scope_sql})
                """
            ),
            params,
        ).scalar()
        or 0
    )
    deleted_rows = int(
        db.execute(
            text(
                f"""
                DELETE FROM competitor_price_percentiles cpp
                WHERE cpp.price_format_id = :target_price_format_id
                  AND ({scope_sql})
                """
            ),
            params,
        ).rowcount
        or 0
    )
    insert_result = db.execute(
        text(
            f"""
            INSERT INTO competitor_price_percentiles (
                price_format_id,
                product_id,
                competitor_price_list_id,
                source_type,
                source_key,
                branch_name,
                competitor_name,
                percentile_scope,
                percentile,
                value,
                source_count,
                price_count,
                used_price_count,
                status,
                updated_at
            )
            SELECT
                :target_price_format_id AS price_format_id,
                cpp.product_id,
                cpp.competitor_price_list_id,
                cpp.source_type,
                cpp.source_key,
                cpp.branch_name,
                cpp.competitor_name,
                cpp.percentile_scope,
                cpp.percentile,
                cpp.value,
                cpp.source_count,
                cpp.price_count,
                cpp.used_price_count,
                cpp.status,
                cpp.updated_at
            FROM competitor_price_percentiles cpp
            WHERE cpp.price_format_id = :source_price_format_id
              AND ({scope_sql})
            """
        ),
        params,
    )
    inserted = int(insert_result.rowcount or 0)
    stats = db.execute(
        text(
            f"""
            SELECT
                (SELECT count(*) FROM products) AS products_processed,
                count(DISTINCT cpp.product_id) FILTER (
                    WHERE cpp.percentile_scope = :regional_scope AND cpp.value IS NOT NULL
                ) AS products_with_competitors
            FROM competitor_price_percentiles cpp
            WHERE cpp.price_format_id = :source_price_format_id
              AND ({scope_sql})
            """
        ),
        params,
    ).mappings().one()
    products_processed = int(stats["products_processed"] or 0)
    products_with_competitors = int(stats["products_with_competitors"] or 0)
    return {
        "products_processed": products_processed,
        "products_with_competitors": products_with_competitors,
        "products_without_competitors": max(0, products_processed - products_with_competitors),
        "rows_created": inserted,
        "rows_updated": 0,
        "rows_skipped": 0,
        "rows_deleted": deleted_rows,
        "execution_time_seconds": round(time.perf_counter() - started_at, 3),
        "engine": "fanout_postgresql",
        "source_price_format_id": source_price_format_id,
        "rows_before": existing_rows,
        "compatibility_rows_created": inserted,
    }


def _recalculate_competitor_percentiles_postgresql(
    *,
    db: Session,
    price_format_id: int,
    source_price_list_ids: list[int] | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    selected_sources = _selected_source_rows(
        db=db,
        price_format_id=price_format_id,
        source_price_list_ids=source_price_list_ids,
    )
    if not selected_sources:
        return _skip_summary(price_format_id)

    db.execute(text("DROP TABLE IF EXISTS tmp_emit_percentile_sources"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE tmp_emit_percentile_sources (
                price_list_id BIGINT PRIMARY KEY,
                branch_name TEXT NOT NULL,
                competitor_name TEXT NOT NULL,
                source_key TEXT NOT NULL,
                source_type TEXT NOT NULL,
                filial_id TEXT NOT NULL,
                source_type_raw TEXT NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    db.execute(
        text(
            """
            INSERT INTO tmp_emit_percentile_sources (
                price_list_id,
                branch_name,
                competitor_name,
                source_key,
                source_type,
                filial_id,
                source_type_raw
            )
            VALUES (
                :price_list_id,
                :branch_name,
                :competitor_name,
                :source_key,
                :source_type,
                :filial_id,
                :source_type_raw
            )
            """
        ),
        selected_sources,
    )

    rows_before_by_source = {
        str(source_key or ""): int(count or 0)
        for source_key, count in db.execute(
            text(
                """
                SELECT p.source_key, count(cpp.id) AS rows_before
                FROM tmp_emit_percentile_sources p
                LEFT JOIN competitor_price_percentiles cpp
                  ON cpp.price_format_id = :price_format_id
                 AND cpp.percentile_scope = :regional_scope
                 AND cpp.source_key = p.source_key
                GROUP BY p.source_key
                """
            ),
            {"price_format_id": price_format_id, "regional_scope": REGIONAL_SCOPE},
        ).all()
    }
    existing_rows = int(
        db.execute(
            text(
                """
                SELECT count(cpp.id)
                FROM competitor_price_percentiles cpp
                WHERE cpp.price_format_id = :price_format_id
                  AND (
                    (
                      cpp.percentile_scope = :regional_scope
                      AND EXISTS (
                        SELECT 1
                        FROM tmp_emit_percentile_sources s
                        WHERE (
                            cpp.source_key = s.source_key
                            OR (
                                coalesce(cpp.source_key, '') = ''
                                AND cpp.branch_name = s.branch_name
                                AND cpp.competitor_name = s.competitor_name
                            )
                        )
                      )
                    )
                    OR (
                      cpp.percentile_scope = :kazakhstan_scope
                      AND cpp.branch_name = :kazakhstan_region
                      AND EXISTS (
                        SELECT 1
                        FROM tmp_emit_percentile_sources s
                        WHERE cpp.competitor_name = s.competitor_name
                      )
                    )
                  )
                """
            ),
            {
                "price_format_id": price_format_id,
                "regional_scope": REGIONAL_SCOPE,
                "kazakhstan_scope": KAZAKHSTAN_SCOPE,
                "kazakhstan_region": KAZAKHSTAN_REGION,
            },
        ).scalar()
        or 0
    )

    deleted_rows = int(
        db.execute(
            text(
                """
                DELETE FROM competitor_price_percentiles cpp
                WHERE cpp.price_format_id = :price_format_id
                  AND (
                    (
                      cpp.percentile_scope = :regional_scope
                      AND EXISTS (
                        SELECT 1
                        FROM tmp_emit_percentile_sources s
                        WHERE (
                            cpp.source_key = s.source_key
                            OR (
                                coalesce(cpp.source_key, '') = ''
                                AND cpp.branch_name = s.branch_name
                                AND cpp.competitor_name = s.competitor_name
                            )
                        )
                      )
                    )
                    OR (
                      cpp.percentile_scope = :kazakhstan_scope
                      AND cpp.branch_name = :kazakhstan_region
                      AND EXISTS (
                        SELECT 1
                        FROM tmp_emit_percentile_sources s
                        WHERE cpp.competitor_name = s.competitor_name
                      )
                    )
                  )
                """
            ),
            {
                "price_format_id": price_format_id,
                "regional_scope": REGIONAL_SCOPE,
                "kazakhstan_scope": KAZAKHSTAN_SCOPE,
                "kazakhstan_region": KAZAKHSTAN_REGION,
            },
        ).rowcount
        or 0
    )

    for source in selected_sources:
        logger.info(
            "[PERCENTILE_MUTATION] action=delete reason=%s price_format_id=%s source_price_list_id=%s "
            "source_type=%s percentile_mode=%s rows_before=%s rows_deleted=%s rows_inserted=%s",
            "emit_percentile_rebuild_scoped",
            price_format_id,
            int(source["price_list_id"]),
            source["source_type_raw"],
            MULTI_PRICE_PERCENTILE_MODE,
            existing_rows,
            deleted_rows,
            0,
        )

    params = {
        "price_format_id": price_format_id,
        "regional_scope": REGIONAL_SCOPE,
        "kazakhstan_scope": KAZAKHSTAN_SCOPE,
        "kazakhstan_region": KAZAKHSTAN_REGION,
        "status_calculated": STATUS_CALCULATED,
        "status_one_price": STATUS_ONE_PRICE,
        "status_no_data": STATUS_NO_DATA,
        "updated_at": now_kz_naive(),
    }

    regional_result = db.execute(
        text(
            """
            WITH matched_prices AS (
                SELECT
                    coalesce(i.product_id, p_goods.id, p_sku.id, p_distributor.id) AS product_id,
                    s.branch_name,
                    s.competitor_name,
                    s.source_key,
                    min(s.price_list_id) AS competitor_price_list_id,
                    min(s.source_type) AS source_type,
                    i.distributor_price::numeric AS distributor_price
                FROM tmp_emit_percentile_sources s
                JOIN competitor_price_list_items i
                  ON i.price_list_id = s.price_list_id
                LEFT JOIN LATERAL (
                    SELECT p.id
                    FROM products p
                    WHERE i.product_id IS NULL
                      AND i.provisor_goods_id IS NOT NULL
                      AND p.provisor_goods_id = i.provisor_goods_id
                    ORDER BY p.id
                    LIMIT 1
                ) p_goods ON TRUE
                LEFT JOIN LATERAL (
                    SELECT p.id
                    FROM products p
                    WHERE i.product_id IS NULL
                      AND p_goods.id IS NULL
                      AND nullif(i.matched_sku, '') IS NOT NULL
                      AND p.code = nullif(i.matched_sku, '')
                    ORDER BY p.id
                    LIMIT 1
                ) p_sku ON TRUE
                LEFT JOIN LATERAL (
                    SELECT p.id
                    FROM products p
                    WHERE i.product_id IS NULL
                      AND p_goods.id IS NULL
                      AND p_sku.id IS NULL
                      AND nullif(i.distributor_goods_id, '') IS NOT NULL
                      AND p.code = nullif(i.distributor_goods_id, '')
                    ORDER BY p.id
                    LIMIT 1
                ) p_distributor ON TRUE
                WHERE i.distributor_price IS NOT NULL
                  AND i.distributor_price > 0
                  AND coalesce(i.product_id, p_goods.id, p_sku.id, p_distributor.id) IS NOT NULL
                GROUP BY
                    coalesce(i.product_id, p_goods.id, p_sku.id, p_distributor.id),
                    s.branch_name,
                    s.competitor_name,
                    s.source_key,
                    i.id,
                    i.distributor_price
            ),
            calculated_arrays AS (
                SELECT
                    product_id,
                    branch_name,
                    competitor_name,
                    source_key,
                    min(competitor_price_list_id) AS competitor_price_list_id,
                    min(source_type) AS source_type,
                    count(*)::integer AS price_count,
                    count(DISTINCT competitor_price_list_id)::integer AS source_count,
                    percentile_cont(ARRAY[0.10, 0.20, 0.30, 0.40, 0.60])
                        WITHIN GROUP (ORDER BY distributor_price) AS percentile_values
                FROM matched_prices
                GROUP BY product_id, branch_name, competitor_name, source_key
            ),
            calculated AS (
                SELECT
                    ca.product_id,
                    ca.branch_name,
                    ca.competitor_name,
                    ca.source_key,
                    ca.competitor_price_list_id,
                    ca.source_type,
                    ca.price_count,
                    ca.source_count,
                    u.percentile,
                    u.value
                FROM calculated_arrays ca
                CROSS JOIN LATERAL unnest(
                    ARRAY[10, 20, 30, 40, 60]::integer[],
                    ca.percentile_values
                ) AS u(percentile, value)
            ),
            source_groups AS (
                SELECT DISTINCT
                    branch_name,
                    competitor_name,
                    source_key,
                    min(price_list_id) OVER (PARTITION BY branch_name, competitor_name, source_key) AS competitor_price_list_id,
                    min(source_type) OVER (PARTITION BY branch_name, competitor_name, source_key) AS source_type
                FROM tmp_emit_percentile_sources
            ),
            insert_rows AS (
                SELECT
                    :price_format_id AS price_format_id,
                    p.id AS product_id,
                    sg.competitor_price_list_id,
                    sg.source_type,
                    sg.source_key,
                    sg.branch_name,
                    sg.competitor_name,
                    :regional_scope AS percentile_scope,
                    pct.percentile,
                    c.value,
                    coalesce(c.source_count, 0) AS source_count,
                    coalesce(c.price_count, 0) AS price_count,
                    coalesce(c.price_count, 0) AS used_price_count,
                    CASE
                        WHEN coalesce(c.price_count, 0) = 0 THEN :status_no_data
                        WHEN c.price_count = 1 THEN :status_one_price
                        ELSE :status_calculated
                    END AS status,
                    :updated_at AS updated_at
                FROM source_groups sg
                CROSS JOIN products p
                CROSS JOIN (SELECT unnest(ARRAY[10, 20, 30, 40, 60]::integer[]) AS percentile) pct
                LEFT JOIN calculated c
                  ON c.product_id = p.id
                 AND c.branch_name = sg.branch_name
                 AND c.competitor_name = sg.competitor_name
                 AND c.source_key = sg.source_key
                 AND c.percentile = pct.percentile
            )
            INSERT INTO competitor_price_percentiles (
                price_format_id,
                product_id,
                competitor_price_list_id,
                source_type,
                source_key,
                branch_name,
                competitor_name,
                percentile_scope,
                percentile,
                value,
                source_count,
                price_count,
                used_price_count,
                status,
                updated_at
            )
            SELECT
                price_format_id,
                product_id,
                competitor_price_list_id,
                source_type,
                source_key,
                branch_name,
                competitor_name,
                percentile_scope,
                percentile,
                value,
                source_count,
                price_count,
                used_price_count,
                status,
                updated_at
            FROM insert_rows
            """
        ),
        params,
    )
    regional_inserted = int(regional_result.rowcount or 0)

    kazakhstan_result = db.execute(
        text(
            """
            WITH regional_values AS (
                SELECT
                    product_id,
                    competitor_name,
                    percentile,
                    value::numeric AS value
                FROM competitor_price_percentiles
                WHERE price_format_id = :price_format_id
                  AND percentile_scope = :regional_scope
                  AND value IS NOT NULL
                  AND EXISTS (
                    SELECT 1
                    FROM tmp_emit_percentile_sources s
                    WHERE competitor_price_percentiles.competitor_name = s.competitor_name
                  )
            ),
            calculated AS (
                SELECT
                    product_id,
                    competitor_name,
                    percentile,
                    percentile_cont((percentile::double precision / 100.0)) WITHIN GROUP (ORDER BY value) AS value,
                    count(*)::integer AS price_count
                FROM regional_values
                GROUP BY product_id, competitor_name, percentile
            ),
            competitors AS (
                SELECT DISTINCT competitor_name FROM tmp_emit_percentile_sources
            ),
            insert_rows AS (
                SELECT
                    :price_format_id AS price_format_id,
                    p.id AS product_id,
                    NULL::bigint AS competitor_price_list_id,
                    'emit' AS source_type,
                    ('emit:kazakhstan:' || c.competitor_name) AS source_key,
                    :kazakhstan_region AS branch_name,
                    c.competitor_name,
                    :kazakhstan_scope AS percentile_scope,
                    pct.percentile,
                    calc.value,
                    coalesce(calc.price_count, 0) AS source_count,
                    coalesce(calc.price_count, 0) AS price_count,
                    coalesce(calc.price_count, 0) AS used_price_count,
                    CASE
                        WHEN coalesce(calc.price_count, 0) = 0 THEN :status_no_data
                        WHEN calc.price_count = 1 THEN :status_one_price
                        ELSE :status_calculated
                    END AS status,
                    :updated_at AS updated_at
                FROM competitors c
                CROSS JOIN products p
                CROSS JOIN (SELECT unnest(ARRAY[10, 20, 30, 40, 60]::integer[]) AS percentile) pct
                LEFT JOIN calculated calc
                  ON calc.product_id = p.id
                 AND calc.competitor_name = c.competitor_name
                 AND calc.percentile = pct.percentile
            )
            INSERT INTO competitor_price_percentiles (
                price_format_id,
                product_id,
                competitor_price_list_id,
                source_type,
                source_key,
                branch_name,
                competitor_name,
                percentile_scope,
                percentile,
                value,
                source_count,
                price_count,
                used_price_count,
                status,
                updated_at
            )
            SELECT
                price_format_id,
                product_id,
                competitor_price_list_id,
                source_type,
                source_key,
                branch_name,
                competitor_name,
                percentile_scope,
                percentile,
                value,
                source_count,
                price_count,
                used_price_count,
                status,
                updated_at
            FROM insert_rows
            """
        ),
        params,
    )
    kazakhstan_inserted = int(kazakhstan_result.rowcount or 0)
    inserted = regional_inserted + kazakhstan_inserted

    stats = db.execute(
        text(
            """
            WITH matched_prices AS (
                SELECT
                    coalesce(i.product_id, p_goods.id, p_sku.id, p_distributor.id) AS product_id,
                    s.source_key,
                    i.distributor_price
                FROM tmp_emit_percentile_sources s
                JOIN competitor_price_list_items i
                  ON i.price_list_id = s.price_list_id
                LEFT JOIN LATERAL (
                    SELECT p.id
                    FROM products p
                    WHERE i.product_id IS NULL
                      AND i.provisor_goods_id IS NOT NULL
                      AND p.provisor_goods_id = i.provisor_goods_id
                    ORDER BY p.id
                    LIMIT 1
                ) p_goods ON TRUE
                LEFT JOIN LATERAL (
                    SELECT p.id
                    FROM products p
                    WHERE i.product_id IS NULL
                      AND p_goods.id IS NULL
                      AND nullif(i.matched_sku, '') IS NOT NULL
                      AND p.code = nullif(i.matched_sku, '')
                    ORDER BY p.id
                    LIMIT 1
                ) p_sku ON TRUE
                LEFT JOIN LATERAL (
                    SELECT p.id
                    FROM products p
                    WHERE i.product_id IS NULL
                      AND p_goods.id IS NULL
                      AND p_sku.id IS NULL
                      AND nullif(i.distributor_goods_id, '') IS NOT NULL
                      AND p.code = nullif(i.distributor_goods_id, '')
                    ORDER BY p.id
                    LIMIT 1
                ) p_distributor ON TRUE
                WHERE i.distributor_price IS NOT NULL
                  AND i.distributor_price > 0
                  AND coalesce(i.product_id, p_goods.id, p_sku.id, p_distributor.id) IS NOT NULL
            )
            SELECT
                (SELECT count(*) FROM products) AS products_processed,
                count(DISTINCT product_id) AS products_with_competitors,
                count(*) AS raw_price_rows
            FROM matched_prices
            """
        )
    ).mappings().one()

    inventory_rows = db.execute(
        text(
            """
            WITH matched_prices AS (
                SELECT
                    coalesce(i.product_id, p_goods.id, p_sku.id, p_distributor.id) AS product_id,
                    s.source_key
                FROM tmp_emit_percentile_sources s
                JOIN competitor_price_list_items i
                  ON i.price_list_id = s.price_list_id
                LEFT JOIN LATERAL (
                    SELECT p.id
                    FROM products p
                    WHERE i.product_id IS NULL
                      AND i.provisor_goods_id IS NOT NULL
                      AND p.provisor_goods_id = i.provisor_goods_id
                    ORDER BY p.id
                    LIMIT 1
                ) p_goods ON TRUE
                LEFT JOIN LATERAL (
                    SELECT p.id
                    FROM products p
                    WHERE i.product_id IS NULL
                      AND p_goods.id IS NULL
                      AND nullif(i.matched_sku, '') IS NOT NULL
                      AND p.code = nullif(i.matched_sku, '')
                    ORDER BY p.id
                    LIMIT 1
                ) p_sku ON TRUE
                LEFT JOIN LATERAL (
                    SELECT p.id
                    FROM products p
                    WHERE i.product_id IS NULL
                      AND p_goods.id IS NULL
                      AND p_sku.id IS NULL
                      AND nullif(i.distributor_goods_id, '') IS NOT NULL
                      AND p.code = nullif(i.distributor_goods_id, '')
                    ORDER BY p.id
                    LIMIT 1
                ) p_distributor ON TRUE
                WHERE i.distributor_price IS NOT NULL
                  AND i.distributor_price > 0
                  AND coalesce(i.product_id, p_goods.id, p_sku.id, p_distributor.id) IS NOT NULL
            ),
            grouped AS (
                SELECT
                    source_key,
                    count(*) AS raw_price_rows,
                    count(DISTINCT product_id) AS product_count
                FROM matched_prices
                GROUP BY source_key
            ),
            rows_after AS (
                SELECT source_key, count(*) AS percentile_rows_after
                FROM competitor_price_percentiles
                WHERE price_format_id = :price_format_id
                  AND percentile_scope = :regional_scope
                GROUP BY source_key
            )
            SELECT
                s.price_list_id,
                s.filial_id,
                s.source_key,
                coalesce(g.raw_price_rows, 0) AS raw_price_rows,
                coalesce(g.product_count, 0) AS product_count,
                coalesce(a.percentile_rows_after, 0) AS percentile_rows_after
            FROM tmp_emit_percentile_sources s
            LEFT JOIN grouped g ON g.source_key = s.source_key
            LEFT JOIN rows_after a ON a.source_key = s.source_key
            ORDER BY s.price_list_id
            """
        ),
        {"price_format_id": price_format_id, "regional_scope": REGIONAL_SCOPE},
    ).mappings().all()

    products_processed = int(stats["products_processed"] or 0)
    products_with_competitors_count = int(stats["products_with_competitors"] or 0)
    summary = {
        "products_processed": products_processed,
        "products_with_competitors": products_with_competitors_count,
        "products_without_competitors": max(0, products_processed - products_with_competitors_count),
        "raw_price_rows": int(stats["raw_price_rows"] or 0),
        "rows_created": inserted,
        "rows_updated": 0,
        "rows_skipped": 0,
        "rows_deleted": deleted_rows,
        "execution_time_seconds": round(time.perf_counter() - started_at, 3),
        "engine": "postgresql",
    }

    for row in inventory_rows:
        source_key = str(row["source_key"] or "")
        logger.info(
            "[EMIT_PERCENTILE_INVENTORY] stage=percentile_rebuild price_format_id=%s inventory=%s",
            price_format_id,
            json.dumps(
                {
                    "filial_id": row["filial_id"] or "",
                    "source_key": source_key,
                    "competitor_price_list_id": int(row["price_list_id"]),
                    "raw_price_rows": int(row["raw_price_rows"] or 0),
                    "product_count": int(row["product_count"] or 0),
                    "percentile_rows_before": int(rows_before_by_source.get(source_key, 0)),
                    "percentile_rows_after": int(row["percentile_rows_after"] or 0),
                    "generated_levels": list(PERCENTILES),
                    "result": "success",
                    "failure_reason": "",
                },
                ensure_ascii=False,
            ),
        )
    for source in selected_sources:
        logger.info(
            "[PERCENTILE_MUTATION] action=insert reason=%s price_format_id=%s source_price_list_id=%s "
            "source_type=%s percentile_mode=%s rows_before=%s rows_deleted=%s rows_inserted=%s",
            "emit_percentile_rebuild_scoped",
            price_format_id,
            int(source["price_list_id"]),
            source["source_type_raw"],
            MULTI_PRICE_PERCENTILE_MODE,
            existing_rows,
            deleted_rows,
            inserted,
        )
    logger.info(
        "[PERCENTILE_REBUILD] price_format_id=%s products_processed=%s products_with_competitors=%s "
        "products_without_competitors=%s rows_created=%s rows_updated=%s rows_skipped=%s rows_deleted=%s engine=%s duration_sec=%s",
        price_format_id,
        summary["products_processed"],
        summary["products_with_competitors"],
        summary["products_without_competitors"],
        summary["rows_created"],
        summary["rows_updated"],
        summary["rows_skipped"],
        summary["rows_deleted"],
        summary["engine"],
        summary["execution_time_seconds"],
    )
    return summary


def _recalculate_competitor_percentiles_python(
    *,
    db: Session,
    price_format_id: int,
    source_price_list_ids: list[int] | None = None,
) -> dict[str, Any]:
    selected = eligible_percentile_assignments(db=db, price_format_id=price_format_id)
    scoped_ids = {
        int(item)
        for item in (source_price_list_ids or [])
        if int(item) > 0
    }
    if scoped_ids:
        selected = [item for item in selected if int(item.price_list.id) in scoped_ids]
    if not selected:
        logger.info(
            "[PERCENTILE_MUTATION] action=skip reason=%s price_format_id=%s source_price_list_id=%s "
            "source_type=%s percentile_mode=%s rows_before=%s rows_deleted=%s rows_inserted=%s",
            "No eligible percentile source assigned; percentile rebuild skipped.",
            price_format_id,
            "",
            "",
            "",
            0,
            0,
            0,
        )
        return {
            "products_processed": 0,
            "products_with_competitors": 0,
            "products_without_competitors": 0,
            "rows_created": 0,
            "rows_updated": 0,
            "rows_skipped": 1,
            "rows_deleted": 0,
            "message": "No eligible percentile source assigned; percentile rebuild skipped.",
        }

    regional_group_filters = [
        (
            (
                (func.coalesce(CompetitorPricePercentile.source_key, "") == _source_key(item.price_list))
                | (
                    (func.coalesce(CompetitorPricePercentile.source_key, "") == "")
                    & (CompetitorPricePercentile.branch_name == _branch_name(item.price_list))
                    & (CompetitorPricePercentile.competitor_name == _competitor_name(item.price_list))
                )
            )
            & (CompetitorPricePercentile.percentile_scope == REGIONAL_SCOPE)
        )
        for item in selected
    ]
    rows_before_by_source: dict[str, int] = {}
    for item in selected:
        source_key = _source_key(item.price_list)
        rows_before_by_source[source_key] = int(
            db.execute(
                select(func.count(CompetitorPricePercentile.id))
                .where(CompetitorPricePercentile.price_format_id == price_format_id)
                .where(CompetitorPricePercentile.percentile_scope == REGIONAL_SCOPE)
                .where(func.coalesce(CompetitorPricePercentile.source_key, "") == source_key)
            ).scalar_one()
            or 0
        )
    kazakhstan_competitors = sorted({_competitor_name(item.price_list) for item in selected})
    kazakhstan_group_filters = [
        (
            (CompetitorPricePercentile.branch_name == KAZAKHSTAN_REGION)
            & (CompetitorPricePercentile.competitor_name == competitor)
            & (CompetitorPricePercentile.percentile_scope == KAZAKHSTAN_SCOPE)
        )
        for competitor in kazakhstan_competitors
    ]
    scoped_filter = or_(*(regional_group_filters + kazakhstan_group_filters))
    existing_rows = int(
        db.execute(
            select(func.count(CompetitorPricePercentile.id))
            .where(CompetitorPricePercentile.price_format_id == price_format_id)
            .where(scoped_filter)
        ).scalar_one()
        or 0
    )
    delete_result = db.execute(
        delete(CompetitorPricePercentile)
        .where(CompetitorPricePercentile.price_format_id == price_format_id)
        .where(scoped_filter)
    )
    deleted_rows = int(delete_result.rowcount or 0)
    for item in selected:
        logger.info(
            "[PERCENTILE_MUTATION] action=delete reason=%s price_format_id=%s source_price_list_id=%s "
            "source_type=%s percentile_mode=%s rows_before=%s rows_deleted=%s rows_inserted=%s",
            "emit_percentile_rebuild_scoped",
            price_format_id,
            int(item.price_list.id),
            item.price_list.source_type,
            MULTI_PRICE_PERCENTILE_MODE,
            existing_rows,
            deleted_rows,
            0,
        )

    selected_ids = [int(item.price_list.id) for item in selected]
    product_rows = db.execute(select(Product.id, Product.code, Product.provisor_goods_id)).all()
    product_ids = [int(product_id) for product_id, _code, _goods_id in product_rows]
    product_id_by_goods_id: dict[int, int] = {}
    product_id_by_code: dict[str, int] = {}
    for product_id, code, goods_id in sorted(product_rows, key=lambda row: int(row[0])):
        if goods_id is not None:
            product_id_by_goods_id.setdefault(int(goods_id), int(product_id))
        product_code = str(code or "").strip()
        if product_code:
            product_id_by_code.setdefault(product_code, int(product_id))
    trace_sku = _trace_sku()
    trace_product_ids = {
        int(product_id)
        for product_id, code, goods_id in product_rows
        if str(code or "").strip() == trace_sku or str(goods_id or "").strip() == trace_sku
    }
    rows = (
        db.execute(
            select(CompetitorPriceList, CompetitorPriceListItem)
            .join(CompetitorPriceListItem, CompetitorPriceListItem.price_list_id == CompetitorPriceList.id)
            .where(CompetitorPriceList.id.in_(selected_ids))
            .where(CompetitorPriceListItem.distributor_price.is_not(None))
            .order_by(
                CompetitorPriceList.id.asc(),
                CompetitorPriceListItem.product_id.asc(),
                CompetitorPriceListItem.provisor_goods_id.asc(),
                CompetitorPriceListItem.id.asc(),
            )
        )
        .all()
        if selected_ids
        else []
    )
    raw_count_by_source: dict[str, int] = defaultdict(int)
    matched_products_by_source: dict[str, set[int]] = defaultdict(set)

    source_groups: set[tuple[str, str, str, int, str]] = set()
    for item in selected:
        source_groups.add(
            (
                _branch_name(item.price_list),
                _competitor_name(item.price_list),
                _source_key(item.price_list),
                int(item.price_list.id),
                _percentile_source_type(item.price_list),
            )
        )

    # Active assignments are the account set. For duplicate rows inside the
    # same account/SKU, Emit percentile sources keep every valid parsed row.
    multi_price_groups: dict[tuple[int, str, str, str, int], list[Decimal]] = defaultdict(list)
    for price_list, item in rows:
        product_id = int(item.product_id or 0)
        if not product_id and item.provisor_goods_id is not None:
            product_id = int(product_id_by_goods_id.get(int(item.provisor_goods_id)) or 0)
        if not product_id:
            matched_sku = str(item.matched_sku or "").strip()
            distributor_goods_id = str(item.distributor_goods_id or "").strip()
            product_id = int(product_id_by_code.get(matched_sku) or product_id_by_code.get(distributor_goods_id) or 0)
        price = _as_decimal(item.distributor_price)
        if not product_id or price is None:
            continue
        branch = _branch_name(price_list)
        competitor = _competitor_name(price_list)
        source_key = _source_key(price_list)
        raw_count_by_source[source_key] += 1
        matched_products_by_source[source_key].add(product_id)
        source_groups.add((branch, competitor, source_key, int(price_list.id), _percentile_source_type(price_list)))
        key = (product_id, branch, competitor, source_key, int(price_list.id))
        multi_price_groups[key].append(price)

    grouped: dict[tuple[int, str, str, str], list[Decimal]] = defaultdict(list)
    source_count_by_group: dict[tuple[int, str, str, str], set[int]] = defaultdict(set)
    for key, prices in multi_price_groups.items():
        product_id, branch, competitor, source_key, price_list_id = key
        grouped[(product_id, branch, competitor, source_key)].extend(prices)
        source_count_by_group[(product_id, branch, competitor, source_key)].add(price_list_id)

    now = now_kz_naive()
    inserted = 0
    products_with_competitors: set[int] = set()
    regional_percentiles: dict[tuple[int, str, int], list[Decimal]] = defaultdict(list)

    for branch, competitor, source_key, price_list_id, source_type in sorted(source_groups):
        for product_id in product_ids:
            values = grouped.get((product_id, branch, competitor, source_key), [])
            source_count = len(source_count_by_group.get((product_id, branch, competitor, source_key), set()))
            price_count = len(values)
            status = _status_for_values(values)
            if values:
                products_with_competitors.add(product_id)
            if product_id in trace_product_ids and values:
                calculated = {
                    pct: float(_percentile(values, pct))
                    for pct in PERCENTILES
                }
                logger.info(
                    "[EMIT_TRACE] stage=percentile_calc price_format_id=%s sku=%s product_id=%s branch=%s competitor=%s "
                    "prices_passed_to_percentile_calculation=%s used_price_count=%s calculated=%s",
                    price_format_id,
                    trace_sku,
                    product_id,
                    branch,
                    competitor,
                    [float(value) for value in values],
                    len(values),
                    calculated,
                )
            for pct in PERCENTILES:
                value: float | None = None
                if values:
                    regional_value = _percentile(values, pct)
                    regional_percentiles[(product_id, competitor, pct)].append(regional_value)
                    value = float(regional_value)
                db.add(
                    CompetitorPricePercentile(
                        price_format_id=price_format_id,
                        product_id=product_id,
                        competitor_price_list_id=price_list_id,
                        source_type=source_type,
                        source_key=source_key,
                        branch_name=branch,
                        competitor_name=competitor,
                        percentile_scope=REGIONAL_SCOPE,
                        percentile=pct,
                        value=value,
                        source_count=source_count,
                        price_count=price_count,
                        used_price_count=price_count,
                        status=status,
                        updated_at=now,
                    )
                )
                inserted += 1

    for competitor in sorted({competitor for _branch, competitor, _source_key, _price_list_id, _source_type in source_groups}):
        for product_id in product_ids:
            for pct in PERCENTILES:
                values = regional_percentiles.get((product_id, competitor, pct), [])
                status = _status_for_values(values)
                db.add(
                    CompetitorPricePercentile(
                        price_format_id=price_format_id,
                        product_id=product_id,
                        competitor_price_list_id=None,
                        source_type="emit",
                        source_key=_kazakhstan_source_key(competitor),
                        branch_name=KAZAKHSTAN_REGION,
                        competitor_name=competitor,
                        percentile_scope=KAZAKHSTAN_SCOPE,
                        percentile=pct,
                        value=float(_percentile(values, pct)) if values else None,
                        source_count=len(values),
                        price_count=len(values),
                        used_price_count=len(values),
                        status=status,
                        updated_at=now,
                    )
                )
                inserted += 1

    products_processed = len(product_ids)
    products_with_competitors_count = len(products_with_competitors)
    summary = {
        "products_processed": products_processed,
        "products_with_competitors": products_with_competitors_count,
        "products_without_competitors": max(0, products_processed - products_with_competitors_count),
        "raw_price_rows": sum(int(value) for value in raw_count_by_source.values()),
        "rows_created": inserted,
        "rows_updated": 0,
        "rows_skipped": 0,
        "rows_deleted": deleted_rows,
    }
    for item in selected:
        price_list = item.price_list
        source_key = _source_key(price_list)
        rows_after = int(
            db.execute(
                select(func.count(CompetitorPricePercentile.id))
                .where(CompetitorPricePercentile.price_format_id == price_format_id)
                .where(CompetitorPricePercentile.percentile_scope == REGIONAL_SCOPE)
                .where(CompetitorPricePercentile.source_key == source_key)
            ).scalar_one()
            or 0
        )
        logger.info(
            "[EMIT_PERCENTILE_INVENTORY] stage=percentile_rebuild price_format_id=%s inventory=%s",
            price_format_id,
            json.dumps(
                {
                    "filial_id": price_list.branch_id or price_list.external_price_list_id or "",
                    "source_key": source_key,
                    "competitor_price_list_id": int(price_list.id),
                    "raw_price_rows": int(raw_count_by_source.get(source_key, 0)),
                    "product_count": len(matched_products_by_source.get(source_key, set())),
                    "percentile_rows_before": int(rows_before_by_source.get(source_key, 0)),
                    "percentile_rows_after": rows_after,
                    "generated_levels": list(PERCENTILES),
                    "result": "success",
                    "failure_reason": "",
                },
                ensure_ascii=False,
            ),
        )
    for item in selected:
        logger.info(
            "[PERCENTILE_MUTATION] action=insert reason=%s price_format_id=%s source_price_list_id=%s "
            "source_type=%s percentile_mode=%s rows_before=%s rows_deleted=%s rows_inserted=%s",
            "emit_percentile_rebuild_scoped",
            price_format_id,
            int(item.price_list.id),
            item.price_list.source_type,
            MULTI_PRICE_PERCENTILE_MODE,
            existing_rows,
            deleted_rows,
            inserted,
        )
    logger.info(
        "[PERCENTILE_REBUILD] price_format_id=%s products_processed=%s products_with_competitors=%s "
        "products_without_competitors=%s rows_created=%s rows_updated=%s rows_skipped=%s rows_deleted=%s",
        price_format_id,
        summary["products_processed"],
        summary["products_with_competitors"],
        summary["products_without_competitors"],
        summary["rows_created"],
        summary["rows_updated"],
        summary["rows_skipped"],
        summary["rows_deleted"],
    )
    return summary


def recalculate_competitor_percentiles_if_needed(*, db: Session, price_format_id: int) -> dict[str, Any]:
    if not eligible_percentile_assignments(db=db, price_format_id=price_format_id):
        logger.info(
            "[PERCENTILE_MUTATION] action=skip reason=%s price_format_id=%s source_price_list_id=%s "
            "source_type=%s percentile_mode=%s rows_before=%s rows_deleted=%s rows_inserted=%s",
            "No eligible percentile source assigned; percentile rebuild skipped.",
            price_format_id,
            "",
            "",
            "",
            0,
            0,
            0,
        )
        return {
            "products_processed": 0,
            "products_with_competitors": 0,
            "products_without_competitors": 0,
            "rows_created": 0,
            "rows_updated": 0,
            "rows_skipped": 1,
            "rows_deleted": 0,
            "message": "No eligible percentile source assigned; percentile rebuild skipped.",
        }
    return recalculate_competitor_percentiles(db=db, price_format_id=price_format_id)


def _regular_competitor_identities_for_price_lists(*, db: Session, price_list_ids: list[int]) -> set[str]:
    if not price_list_ids:
        return set()
    rows = db.execute(select(CompetitorPriceList).where(CompetitorPriceList.id.in_(price_list_ids))).scalars().all()
    return {
        identity
        for row in rows
        for identity in (regular_competitor_identity(row),)
        if identity and not _is_emit_price_list(row)
    }


def _regular_delete_identities(competitor_identities: set[str]) -> set[str]:
    out = {str(item or "").strip().casefold() for item in competitor_identities if str(item or "").strip()}
    for identity in list(out):
        out.update(regular_competitor_alias_obsolete_identities(identity))
    return out


def recalculate_regular_competitor_percentiles(
    *,
    db: Session,
    competitor_identities: set[str] | None = None,
) -> dict[str, Any]:
    identities = {str(item or "").strip().casefold() for item in (competitor_identities or set()) if str(item or "").strip()}
    if (db.get_bind().dialect.name or "").lower() == "postgresql":
        summary = _recalculate_regular_competitor_percentiles_postgresql(db=db, competitor_identities=identities)
    else:
        summary = _recalculate_regular_competitor_percentiles_python(db=db, competitor_identities=identities)
    refresh_regular_percentile_source_summaries(db=db, competitor_identities=identities)
    return summary


def _recalculate_regular_competitor_percentiles_python(
    *,
    db: Session,
    competitor_identities: set[str],
) -> dict[str, Any]:
    started_at = time.perf_counter()
    product_rows = db.execute(select(Product.id, Product.code, Product.provisor_goods_id)).all()
    product_id_by_goods_id: dict[int, int] = {}
    product_id_by_code: dict[str, int] = {}
    for product_id, code, goods_id in sorted(product_rows, key=lambda row: int(row[0])):
        if goods_id is not None:
            product_id_by_goods_id.setdefault(int(goods_id), int(product_id))
        product_code = str(code or "").strip()
        if product_code:
            product_id_by_code.setdefault(product_code, int(product_id))

    rows = (
        db.execute(
            select(CompetitorPriceList, CompetitorPriceListItem)
            .join(CompetitorPriceListItem, CompetitorPriceListItem.price_list_id == CompetitorPriceList.id)
            .where(CompetitorPriceListItem.distributor_price.is_not(None))
            .order_by(CompetitorPriceList.id.asc(), CompetitorPriceListItem.id.asc())
        )
        .all()
    )
    grouped: dict[tuple[str, int], list[Decimal]] = defaultdict(list)
    source_counts: dict[tuple[str, int], set[int]] = defaultdict(set)
    display_names: dict[str, str] = {}
    alias_identities_by_canonical: dict[str, set[str]] = defaultdict(set)
    source_rows = 0
    for price_list, item in rows:
        if not _regular_price_list_is_usable(price_list):
            continue
        identity = regular_competitor_identity(price_list)
        if competitor_identities and identity not in competitor_identities:
            continue
        for old_identity in {_stored_regular_competitor_identity(price_list), _legacy_regular_competitor_identity(price_list)}:
            if old_identity and old_identity != identity:
                alias_identities_by_canonical[identity].add(old_identity)
        product_id = int(item.product_id or 0)
        if not product_id and item.provisor_goods_id is not None:
            product_id = int(product_id_by_goods_id.get(int(item.provisor_goods_id)) or 0)
        if not product_id:
            matched_sku = str(item.matched_sku or "").strip()
            distributor_goods_id = str(item.distributor_goods_id or "").strip()
            product_id = int(product_id_by_code.get(matched_sku) or product_id_by_code.get(distributor_goods_id) or 0)
        price = _as_decimal(item.distributor_price)
        if not identity or not product_id or price is None:
            continue
        source_rows += 1
        display_names.setdefault(identity, _competitor_name(price_list))
        grouped[(identity, product_id)].append(price)
        source_counts[(identity, product_id)].add(int(price_list.id))

    if not competitor_identities:
        competitor_identities = {identity for identity, _product_id in grouped}
    canonical_rows_deleted = 0
    if competitor_identities:
        canonical_rows_deleted = int(
            db.execute(
                delete(RegularCompetitorPricePercentile).where(
                    RegularCompetitorPricePercentile.competitor_identity.in_(sorted(competitor_identities))
                )
            ).rowcount
            or 0
        )

    now = now_kz_naive()
    mappings: list[dict[str, Any]] = []
    for (identity, product_id), values in sorted(grouped.items()):
        if competitor_identities and identity not in competitor_identities:
            continue
        sample_count = len(values)
        source_count = len(source_counts.get((identity, product_id), set()))
        min_price = min(values)
        max_price = max(values)
        for pct in PERCENTILES:
            mappings.append(
                {
                    "competitor_identity": identity,
                    "competitor_name": regular_competitor_display_name(identity, display_names.get(identity, identity)),
                    "product_id": product_id,
                    "percentile": pct,
                    "value": float(_percentile(values, pct)),
                    "sample_count": sample_count,
                    "source_count": source_count,
                    "min_price": float(min_price),
                    "max_price": float(max_price),
                    "algorithm_version": REGULAR_PERCENTILE_ALGORITHM_VERSION,
                    "calculated_at": now,
                }
            )
    if mappings:
        db.bulk_insert_mappings(RegularCompetitorPricePercentile, mappings)
    rows_written_by_identity = defaultdict(int)
    for row in mappings:
        rows_written_by_identity[str(row["competitor_identity"])] += 1
    alias_rows_deleted_by_identity: dict[str, int] = {}
    for identity, alias_identities in sorted(alias_identities_by_canonical.items()):
        if not rows_written_by_identity.get(identity):
            continue
        if not alias_identities:
            continue
        alias_rows_deleted_by_identity[identity] = int(
            db.execute(
                delete(RegularCompetitorPricePercentile).where(
                    RegularCompetitorPricePercentile.competitor_identity.in_(sorted(alias_identities))
                )
            ).rowcount
            or 0
        )
    total_alias_rows_deleted = sum(alias_rows_deleted_by_identity.values())
    summary = {
        "regularCompetitorsProcessed": len({row["competitor_identity"] for row in mappings}),
        "regularSourceRowsSelected": source_rows,
        "regularMatchedRows": source_rows,
        "regularProductsGrouped": len(grouped),
        "regularPercentileRowsDeleted": canonical_rows_deleted + total_alias_rows_deleted,
        "regularPercentileRowsWritten": len(mappings),
        "regularPercentileElapsedSec": round(time.perf_counter() - started_at, 3),
        "regularEngine": "python",
        "totalAliasIdentitiesDeleted": sum(1 for aliases in alias_identities_by_canonical.values() for _alias in aliases),
        "totalAliasRowsDeleted": total_alias_rows_deleted,
    }
    for identity in sorted({row["competitor_identity"] for row in mappings} | competitor_identities):
        physical_price_lists = sorted(
            {
                price_list_id
                for grouped_identity, _product_id in grouped
                if grouped_identity == identity
                for price_list_id in source_counts.get((grouped_identity, _product_id), set())
            }
        )
        logger.info(
            "[REGULAR_PERCENTILE_CANONICAL_REBUILD] canonical_identity=%s canonical_rows_written=%s "
            "alias_identities_found=%s alias_rows_deleted_for_this_identity=%s elapsed_sec=%s "
            "physical_price_lists=%s source_rows=%s matched_rows=%s products_grouped=%s",
            identity,
            rows_written_by_identity.get(identity, 0),
            sorted(alias_identities_by_canonical.get(identity, set())),
            alias_rows_deleted_by_identity.get(identity, 0),
            summary["regularPercentileElapsedSec"],
            len(physical_price_lists),
            source_rows,
            source_rows,
            len({product_id for grouped_identity, product_id in grouped if grouped_identity == identity}),
        )
    logger.info(
        "[REGULAR_PERCENTILE_CANONICAL_REBUILD] total_alias_identities_deleted=%s total_alias_rows_deleted=%s",
        summary["totalAliasIdentitiesDeleted"],
        summary["totalAliasRowsDeleted"],
    )
    logger.info("[REGULAR_PERCENTILE_REBUILD] completed summary=%s", json.dumps(summary, ensure_ascii=False))
    return summary


def _recalculate_regular_competitor_percentiles_postgresql(
    *,
    db: Session,
    competitor_identities: set[str],
) -> dict[str, Any]:
    started_at = time.perf_counter()
    requested_identities = {str(item or "").strip().casefold() for item in competitor_identities if str(item or "").strip()}
    db.execute(text("DROP TABLE IF EXISTS tmp_regular_percentile_identities"))
    db.execute(text("CREATE TEMP TABLE tmp_regular_percentile_identities (identity TEXT PRIMARY KEY) ON COMMIT DROP"))
    if requested_identities:
        db.execute(
            text("INSERT INTO tmp_regular_percentile_identities (identity) VALUES (:identity)"),
            [{"identity": item} for item in sorted(requested_identities)],
        )
    db.execute(text("DROP TABLE IF EXISTS tmp_regular_percentile_sources"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE tmp_regular_percentile_sources (
                price_list_id BIGINT PRIMARY KEY,
                competitor_identity TEXT NOT NULL,
                competitor_name TEXT NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    candidate_price_lists = db.execute(select(CompetitorPriceList).order_by(CompetitorPriceList.id.asc())).scalars().all()
    source_mappings = []
    alias_mappings: list[dict[str, str]] = []
    for price_list in candidate_price_lists:
        if not _regular_price_list_is_usable(price_list):
            continue
        identity = regular_competitor_identity(price_list)
        if not identity or (requested_identities and identity not in requested_identities):
            continue
        for old_identity in {_stored_regular_competitor_identity(price_list), _legacy_regular_competitor_identity(price_list)}:
            if old_identity and old_identity != identity:
                alias_mappings.append({"canonical_identity": identity, "alias_identity": old_identity})
        source_mappings.append(
            {
                "price_list_id": int(price_list.id),
                "competitor_identity": identity,
                "competitor_name": regular_competitor_display_name(identity, _competitor_name(price_list)),
            }
        )
    if source_mappings:
        db.execute(
            text(
                """
                INSERT INTO tmp_regular_percentile_sources (
                    price_list_id,
                    competitor_identity,
                    competitor_name
                )
                VALUES (
                    :price_list_id,
                    :competitor_identity,
                    :competitor_name
                )
                """
            ),
            source_mappings,
        )
    db.execute(text("DROP TABLE IF EXISTS tmp_regular_percentile_alias_identities"))
    db.execute(
        text(
            """
            CREATE TEMP TABLE tmp_regular_percentile_alias_identities (
                canonical_identity TEXT NOT NULL,
                alias_identity TEXT NOT NULL,
                PRIMARY KEY (canonical_identity, alias_identity)
            ) ON COMMIT DROP
            """
        )
    )
    if alias_mappings:
        db.execute(
            text(
                """
                INSERT INTO tmp_regular_percentile_alias_identities (
                    canonical_identity,
                    alias_identity
                )
                VALUES (
                    :canonical_identity,
                    :alias_identity
                )
                ON CONFLICT DO NOTHING
                """
            ),
            alias_mappings,
        )
    selected_identities = {str(row["competitor_identity"]) for row in source_mappings}
    canonical_rows_deleted = int(
        db.execute(
            text(
                """
                DELETE FROM regular_competitor_price_percentiles
                WHERE (SELECT count(*) FROM tmp_regular_percentile_identities) = 0
                   OR competitor_identity IN (SELECT identity FROM tmp_regular_percentile_identities)
                """
            )
        ).rowcount
        or 0
    )
    result = db.execute(
        text(
            f"""
            WITH matched_prices AS (
                SELECT
                    src.competitor_identity AS competitor_identity,
                    min(src.competitor_name) AS competitor_name,
                    coalesce(i.product_id, p_goods.id, p_sku.id, p_distributor.id) AS product_id,
                    i.price_list_id,
                    i.distributor_price::numeric AS distributor_price
                FROM competitor_price_lists cpl
                JOIN tmp_regular_percentile_sources src ON src.price_list_id = cpl.id
                JOIN competitor_price_list_items i ON i.price_list_id = cpl.id
                LEFT JOIN LATERAL (
                    SELECT p.id
                    FROM products p
                    WHERE i.product_id IS NULL
                      AND i.provisor_goods_id IS NOT NULL
                      AND p.provisor_goods_id = i.provisor_goods_id
                    ORDER BY p.id
                    LIMIT 1
                ) p_goods ON TRUE
                LEFT JOIN LATERAL (
                    SELECT p.id
                    FROM products p
                    WHERE i.product_id IS NULL
                      AND p_goods.id IS NULL
                      AND nullif(i.matched_sku, '') IS NOT NULL
                      AND p.code = nullif(i.matched_sku, '')
                    ORDER BY p.id
                    LIMIT 1
                ) p_sku ON TRUE
                LEFT JOIN LATERAL (
                    SELECT p.id
                    FROM products p
                    WHERE i.product_id IS NULL
                      AND p_goods.id IS NULL
                      AND p_sku.id IS NULL
                      AND nullif(i.distributor_goods_id, '') IS NOT NULL
                      AND p.code = nullif(i.distributor_goods_id, '')
                    ORDER BY p.id
                    LIMIT 1
                ) p_distributor ON TRUE
                WHERE i.distributor_price IS NOT NULL
                  AND i.distributor_price > 0
                  AND coalesce(i.product_id, p_goods.id, p_sku.id, p_distributor.id) IS NOT NULL
                GROUP BY src.competitor_identity, coalesce(i.product_id, p_goods.id, p_sku.id, p_distributor.id), i.id, i.price_list_id, i.distributor_price
            ),
            calculated_arrays AS (
                SELECT
                    competitor_identity,
                    min(competitor_name) AS competitor_name,
                    product_id,
                    count(*)::integer AS sample_count,
                    count(DISTINCT price_list_id)::integer AS source_count,
                    min(distributor_price) AS min_price,
                    max(distributor_price) AS max_price,
                    percentile_cont(ARRAY[0.10, 0.20, 0.30, 0.40, 0.60])
                        WITHIN GROUP (ORDER BY distributor_price) AS percentile_values
                FROM matched_prices
                GROUP BY competitor_identity, product_id
            )
            INSERT INTO regular_competitor_price_percentiles (
                competitor_identity,
                competitor_name,
                product_id,
                percentile,
                value,
                sample_count,
                source_count,
                min_price,
                max_price,
                algorithm_version,
                calculated_at
            )
            SELECT
                ca.competitor_identity,
                ca.competitor_name,
                ca.product_id,
                u.percentile,
                u.value,
                ca.sample_count,
                ca.source_count,
                ca.min_price,
                ca.max_price,
                :algorithm_version,
                :calculated_at
            FROM calculated_arrays ca
            CROSS JOIN LATERAL unnest(
                ARRAY[10, 20, 30, 40, 60]::integer[],
                ca.percentile_values
            ) AS u(percentile, value)
            """
        ),
        {
            "algorithm_version": REGULAR_PERCENTILE_ALGORITHM_VERSION,
            "calculated_at": now_kz_naive(),
        },
    )
    written = int(result.rowcount or 0)
    alias_delete_counts = db.execute(
        text(
            """
            WITH canonical_written AS (
                SELECT competitor_identity
                FROM regular_competitor_price_percentiles
                WHERE competitor_identity IN (
                    SELECT canonical_identity
                    FROM tmp_regular_percentile_alias_identities
                )
                GROUP BY competitor_identity
            )
            SELECT a.canonical_identity, count(r.id) AS rows_to_delete
            FROM tmp_regular_percentile_alias_identities a
            JOIN canonical_written cw ON cw.competitor_identity = a.canonical_identity
            JOIN regular_competitor_price_percentiles r ON r.competitor_identity = a.alias_identity
            GROUP BY a.canonical_identity
            """
        )
    ).all()
    db.execute(
        text(
            """
            WITH canonical_written AS (
                SELECT competitor_identity
                FROM regular_competitor_price_percentiles
                WHERE competitor_identity IN (
                    SELECT canonical_identity
                    FROM tmp_regular_percentile_alias_identities
                )
                GROUP BY competitor_identity
            )
            DELETE FROM regular_competitor_price_percentiles r
            USING tmp_regular_percentile_alias_identities a
            JOIN canonical_written cw ON cw.competitor_identity = a.canonical_identity
            WHERE r.competitor_identity = a.alias_identity
            """
        )
    )
    alias_rows_deleted_by_identity: dict[str, int] = defaultdict(int)
    alias_identities_found_by_identity: dict[str, set[str]] = defaultdict(set)
    for canonical_identity, alias_identity in db.execute(
        text("SELECT canonical_identity, alias_identity FROM tmp_regular_percentile_alias_identities")
    ).all():
        alias_identities_found_by_identity[str(canonical_identity)].add(str(alias_identity))
    for canonical_identity, deleted_rows in alias_delete_counts:
        alias_rows_deleted_by_identity[str(canonical_identity)] += int(deleted_rows or 0)
    total_alias_rows_deleted = sum(alias_rows_deleted_by_identity.values())
    stat_rows = db.execute(
        text(
            """
            SELECT
                src.competitor_identity,
                count(DISTINCT src.price_list_id) AS physical_price_lists,
                count(i.id) AS source_rows,
                count(i.id) FILTER (
                    WHERE i.distributor_price IS NOT NULL
                      AND i.distributor_price > 0
                      AND coalesce(i.product_id, p_goods.id, p_sku.id, p_distributor.id) IS NOT NULL
                ) AS matched_rows,
                count(DISTINCT coalesce(i.product_id, p_goods.id, p_sku.id, p_distributor.id)) FILTER (
                    WHERE i.distributor_price IS NOT NULL
                      AND i.distributor_price > 0
                      AND coalesce(i.product_id, p_goods.id, p_sku.id, p_distributor.id) IS NOT NULL
                ) AS products_grouped
            FROM tmp_regular_percentile_sources src
            JOIN competitor_price_list_items i ON i.price_list_id = src.price_list_id
            LEFT JOIN LATERAL (
                SELECT p.id
                FROM products p
                WHERE i.product_id IS NULL
                  AND i.provisor_goods_id IS NOT NULL
                  AND p.provisor_goods_id = i.provisor_goods_id
                ORDER BY p.id
                LIMIT 1
            ) p_goods ON TRUE
            LEFT JOIN LATERAL (
                SELECT p.id
                FROM products p
                WHERE i.product_id IS NULL
                  AND p_goods.id IS NULL
                  AND nullif(i.matched_sku, '') IS NOT NULL
                  AND p.code = nullif(i.matched_sku, '')
                ORDER BY p.id
                LIMIT 1
            ) p_sku ON TRUE
            LEFT JOIN LATERAL (
                SELECT p.id
                FROM products p
                WHERE i.product_id IS NULL
                  AND p_goods.id IS NULL
                  AND p_sku.id IS NULL
                  AND nullif(i.distributor_goods_id, '') IS NOT NULL
                  AND p.code = nullif(i.distributor_goods_id, '')
                ORDER BY p.id
                LIMIT 1
            ) p_distributor ON TRUE
            GROUP BY src.competitor_identity
            """
        )
    ).all()
    rows_written_by_identity = dict(
        db.execute(
            text(
                """
                SELECT competitor_identity, count(*) AS rows_written
                FROM regular_competitor_price_percentiles
                WHERE competitor_identity IN (SELECT competitor_identity FROM tmp_regular_percentile_sources)
                GROUP BY competitor_identity
                """
            )
        ).all()
    )
    summary = {
        "regularCompetitorsProcessed": len(selected_identities),
        "regularPercentileRowsDeleted": canonical_rows_deleted + total_alias_rows_deleted,
        "regularPercentileRowsWritten": written,
        "regularPercentileElapsedSec": round(time.perf_counter() - started_at, 3),
        "regularEngine": "postgresql",
        "totalAliasIdentitiesDeleted": sum(len(items) for items in alias_identities_found_by_identity.values()),
        "totalAliasRowsDeleted": total_alias_rows_deleted,
    }
    for row in stat_rows:
        logger.info(
            "[REGULAR_PERCENTILE_CANONICAL_REBUILD] canonical_identity=%s canonical_rows_written=%s "
            "alias_identities_found=%s alias_rows_deleted_for_this_identity=%s elapsed_sec=%s "
            "physical_price_lists=%s source_rows=%s matched_rows=%s products_grouped=%s",
            row.competitor_identity,
            int(rows_written_by_identity.get(row.competitor_identity, 0) or 0),
            sorted(alias_identities_found_by_identity.get(str(row.competitor_identity), set())),
            alias_rows_deleted_by_identity.get(str(row.competitor_identity), 0),
            summary["regularPercentileElapsedSec"],
            int(row.physical_price_lists or 0),
            int(row.source_rows or 0),
            int(row.matched_rows or 0),
            int(row.products_grouped or 0),
        )
    logger.info(
        "[REGULAR_PERCENTILE_CANONICAL_REBUILD] total_alias_identities_deleted=%s total_alias_rows_deleted=%s",
        summary["totalAliasIdentitiesDeleted"],
        summary["totalAliasRowsDeleted"],
    )
    logger.info("[REGULAR_PERCENTILE_REBUILD] completed summary=%s", json.dumps(summary, ensure_ascii=False))
    return summary


def recalculate_percentiles_for_price_lists(
    *,
    db: Session,
    competitor_price_list_ids: list[int],
) -> dict[str, Any]:
    started_at = time.perf_counter()
    ids = sorted({int(item) for item in competitor_price_list_ids if int(item) > 0})
    if not ids:
        return {
            "priceFormatsProcessed": 0,
            "percentileSourcesProcessed": 0,
            "percentileRowsWritten": 0,
            "eligible_price_format_count": 0,
            "eligible_source_count": 0,
            "rows_deleted": 0,
            "rows_written": 0,
            "productsGrouped": 0,
            "percentile_total_sec": 0.0,
            "percentileElapsedSec": 0.0,
            "skipped_reason": "no_refreshed_price_list_ids",
            "summaries": {},
            "warnings": [],
        }

    regular_identities = _regular_competitor_identities_for_price_lists(db=db, price_list_ids=ids)
    regular_summary = (
        recalculate_regular_competitor_percentiles(db=db, competitor_identities=regular_identities)
        if regular_identities
        else {
            "regularCompetitorsProcessed": 0,
            "regularSourceRowsSelected": 0,
            "regularProductsGrouped": 0,
            "regularPercentileRowsDeleted": 0,
            "regularPercentileRowsWritten": 0,
        }
    )

    rows = db.execute(
        select(PriceFormatCompetitorAssignment, CompetitorPriceList, PriceFormat)
        .join(CompetitorPriceList, CompetitorPriceList.id == PriceFormatCompetitorAssignment.competitor_price_list_id)
        .join(PriceFormat, PriceFormat.id == PriceFormatCompetitorAssignment.price_format_id)
        .where(PriceFormatCompetitorAssignment.competitor_price_list_id.in_(ids))
        .where(PriceFormatCompetitorAssignment.is_active.is_(True))
    ).all()
    by_format: dict[int, list[int]] = {}
    warnings: list[dict[str, Any]] = []
    for assignment, price_list, pf in rows:
        if not _is_emit_price_list(price_list):
            continue
        mode = effective_percentile_mode(price_list, assignment.percentile_mode)
        source_key = canonical_competitor_source_key(price_list)
        if mode != MULTI_PRICE_PERCENTILE_MODE:
            warnings.append(
                {
                    "code": "percentile_mode_disabled",
                    "priceFormatId": int(pf.id),
                    "priceListId": int(price_list.id),
                    "sourceKey": source_key,
                }
            )
            continue
        if not source_key:
            warnings.append(
                {
                    "code": "missing_canonical_source_key",
                    "priceFormatId": int(pf.id),
                    "priceListId": int(price_list.id),
                }
            )
            continue
        by_format.setdefault(int(pf.id), []).append(int(price_list.id))

    summaries: dict[str, Any] = {}
    for price_format_id, scoped_ids in sorted(by_format.items()):
        summary = recalculate_competitor_percentiles(
            db=db,
            price_format_id=price_format_id,
            source_price_list_ids=sorted(set(scoped_ids)),
        )
        pf = db.get(PriceFormat, price_format_id)
        summaries[str(pf.code if pf is not None else price_format_id)] = {
            "price_format_id": price_format_id,
            "source_price_list_ids": sorted(set(scoped_ids)),
            **summary,
        }

    unique_source_count = len({item for ids_for_format in by_format.values() for item in ids_for_format})
    rows_deleted = sum(int(item.get("rows_deleted") or 0) for item in summaries.values())
    rows_written = sum(int(item.get("rows_created") or 0) for item in summaries.values())
    percentile_total_sec = round(time.perf_counter() - started_at, 6)
    return {
        "priceFormatsProcessed": len(by_format),
        "percentileSourcesProcessed": unique_source_count,
        "percentileRowsWritten": rows_written,
        "eligible_price_format_count": len(by_format),
        "eligible_source_count": unique_source_count,
        "rows_deleted": rows_deleted,
        "rows_written": rows_written,
        "productsGrouped": sum(int(item.get("products_with_competitors") or 0) for item in summaries.values()),
        "percentile_total_sec": percentile_total_sec,
        "percentileElapsedSec": round(percentile_total_sec, 3),
        "skipped_reason": "" if by_format else "no_eligible_multi_price_per_sku_assignments",
        "summaries": summaries,
        "warnings": warnings,
        "regularPercentiles": regular_summary,
    }


def backfill_blank_percentile_source_keys(*, db: Session, apply: bool = False) -> dict[str, Any]:
    groups = db.execute(
        select(
            CompetitorPricePercentile.price_format_id,
            CompetitorPricePercentile.branch_name,
            CompetitorPricePercentile.competitor_name,
            func.count(CompetitorPricePercentile.id),
        )
        .where(func.coalesce(CompetitorPricePercentile.source_key, "") == "")
        .group_by(
            CompetitorPricePercentile.price_format_id,
            CompetitorPricePercentile.branch_name,
            CompetitorPricePercentile.competitor_name,
        )
    ).all()
    updated_groups = 0
    updated_rows = 0
    ambiguous_groups: list[dict[str, Any]] = []
    for price_format_id, branch_name, competitor_name, rows_count in groups:
        candidates = db.execute(
            select(CompetitorPriceList)
            .join(
                PriceFormatCompetitorAssignment,
                PriceFormatCompetitorAssignment.competitor_price_list_id == CompetitorPriceList.id,
            )
            .where(PriceFormatCompetitorAssignment.price_format_id == price_format_id)
            .where(PriceFormatCompetitorAssignment.is_active.is_(True))
            .where(CompetitorPriceList.branch_name == branch_name)
            .where(CompetitorPriceList.competitor_name == competitor_name)
        ).scalars().all()
        keys = sorted({canonical_competitor_source_key(row) for row in candidates if canonical_competitor_source_key(row)})
        if len(keys) != 1:
            ambiguous_groups.append(
                {
                    "price_format_id": int(price_format_id),
                    "branch_name": branch_name,
                    "competitor_name": competitor_name,
                    "rows": int(rows_count or 0),
                    "candidate_source_keys": keys,
                }
            )
            continue
        if apply:
            result = db.execute(
                text(
                    """
                    UPDATE competitor_price_percentiles
                    SET source_key = :source_key
                    WHERE price_format_id = :price_format_id
                      AND branch_name = :branch_name
                      AND competitor_name = :competitor_name
                      AND coalesce(source_key, '') = ''
                    """
                ),
                {
                    "source_key": keys[0],
                    "price_format_id": price_format_id,
                    "branch_name": branch_name,
                    "competitor_name": competitor_name,
                },
            )
            updated_rows += int(result.rowcount or 0)
        else:
            updated_rows += int(rows_count or 0)
        updated_groups += 1
    return {
        "apply": apply,
        "updated_groups": updated_groups,
        "updated_rows": updated_rows,
        "ambiguous_groups": len(ambiguous_groups),
        "ambiguous": ambiguous_groups[:100],
    }
