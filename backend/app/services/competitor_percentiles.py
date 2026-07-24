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

from ..models import CompetitorPriceList, CompetitorPriceListItem, CompetitorPricePercentile, Product
from ..timezone import now_kz_naive
from .competitor_assignments import get_assigned_competitor_price_lists
from .competitor_source_config import MULTI_PRICE_PERCENTILE_MODE, effective_percentile_mode


logger = logging.getLogger(__name__)


PERCENTILES = (10, 20, 30, 40, 60)
DEFAULT_BRANCH = "Без филиала"
REGIONAL_SCOPE = "regional"
KAZAKHSTAN_SCOPE = "kazakhstan"
KAZAKHSTAN_REGION = "Kazakhstan"
STATUS_CALCULATED = "Calculated"
STATUS_ONE_PRICE = "Calculated from one price"
STATUS_NO_DATA = "No data"
DEFAULT_TRACE_SKU = "163571"


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


def _status_for_values(values: list[Decimal]) -> str:
    if len(values) == 1:
        return STATUS_ONE_PRICE
    if values:
        return STATUS_CALCULATED
    return STATUS_NO_DATA


def emit_percentile_assignments(*, db: Session, price_format_id: int):
    return [
        item
        for item in get_assigned_competitor_price_lists(db=db, price_format_id=price_format_id)
        if effective_percentile_mode(item.price_list, item.assignment.percentile_mode) == MULTI_PRICE_PERCENTILE_MODE
    ]


def _source_key(price_list: CompetitorPriceList) -> str:
    return str(price_list.source_key or "").strip()


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
        return _recalculate_competitor_percentiles_postgresql(
            db=db,
            price_format_id=price_format_id,
            source_price_list_ids=source_price_list_ids,
        )
    return _recalculate_competitor_percentiles_python(
        db=db,
        price_format_id=price_format_id,
        source_price_list_ids=source_price_list_ids,
    )


def _selected_source_rows(
    *,
    db: Session,
    price_format_id: int,
    source_price_list_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    selected = emit_percentile_assignments(db=db, price_format_id=price_format_id)
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
        "No Emit percentile source assigned; percentile rebuild skipped.",
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
        "message": "No Emit percentile source assigned; percentile rebuild skipped.",
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
    selected = emit_percentile_assignments(db=db, price_format_id=price_format_id)
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
            "No Emit percentile source assigned; percentile rebuild skipped.",
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
            "message": "No Emit percentile source assigned; percentile rebuild skipped.",
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
    if not emit_percentile_assignments(db=db, price_format_id=price_format_id):
        logger.info(
            "[PERCENTILE_MUTATION] action=skip reason=%s price_format_id=%s source_price_list_id=%s "
            "source_type=%s percentile_mode=%s rows_before=%s rows_deleted=%s rows_inserted=%s",
            "No Emit percentile source assigned; percentile rebuild skipped.",
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
            "message": "No Emit percentile source assigned; percentile rebuild skipped.",
        }
    return recalculate_competitor_percentiles(db=db, price_format_id=price_format_id)
