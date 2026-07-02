from __future__ import annotations

from collections import defaultdict
import logging
import os
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, or_, select
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


def _percentile(values: list[Decimal], percentile: int) -> Decimal:
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    pos = (Decimal(percentile) / Decimal(100)) * Decimal(len(ordered) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = pos - Decimal(lower)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


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


def emit_percentile_group_keys(*, db: Session, price_format_id: int) -> set[tuple[str, str]]:
    return {
        (_branch_name(item.price_list), _competitor_name(item.price_list))
        for item in emit_percentile_assignments(db=db, price_format_id=price_format_id)
    }


def _trace_sku() -> str:
    return str(os.getenv("EMIT_TRACE_SKU", DEFAULT_TRACE_SKU) or "").strip()


def recalculate_competitor_percentiles(*, db: Session, price_format_id: int) -> dict[str, Any]:
    selected = emit_percentile_assignments(db=db, price_format_id=price_format_id)
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
            (CompetitorPricePercentile.branch_name == _branch_name(item.price_list))
            & (CompetitorPricePercentile.competitor_name == _competitor_name(item.price_list))
            & (CompetitorPricePercentile.percentile_scope == REGIONAL_SCOPE)
        )
        for item in selected
    ]
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
    product_id_by_goods_id = {
        int(goods_id): int(product_id)
        for product_id, _code, goods_id in product_rows
        if goods_id is not None
    }
    product_id_by_code = {
        str(code or "").strip(): int(product_id)
        for product_id, code, _goods_id in product_rows
        if str(code or "").strip()
    }
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

    source_groups: set[tuple[str, str]] = set()
    for item in selected:
        source_groups.add((_branch_name(item.price_list), _competitor_name(item.price_list)))

    # Active assignments are the account set. For duplicate rows inside the
    # same account/SKU, Emit percentile sources keep every valid parsed row.
    multi_price_groups: dict[tuple[int, str, str, int], list[Decimal]] = defaultdict(list)
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
        source_groups.add((branch, competitor))
        key = (product_id, branch, competitor, int(price_list.id))
        multi_price_groups[key].append(price)

    grouped: dict[tuple[int, str, str], list[Decimal]] = defaultdict(list)
    source_count_by_group: dict[tuple[int, str, str], set[int]] = defaultdict(set)
    for key, prices in multi_price_groups.items():
        product_id, branch, competitor, price_list_id = key
        grouped[(product_id, branch, competitor)].extend(prices)
        source_count_by_group[(product_id, branch, competitor)].add(price_list_id)

    now = now_kz_naive()
    inserted = 0
    products_with_competitors: set[int] = set()
    regional_percentiles: dict[tuple[int, str, int], list[Decimal]] = defaultdict(list)

    for branch, competitor in sorted(source_groups):
        for product_id in product_ids:
            values = grouped.get((product_id, branch, competitor), [])
            source_count = len(source_count_by_group.get((product_id, branch, competitor), set()))
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

    for competitor in sorted({competitor for _branch, competitor in source_groups}):
        for product_id in product_ids:
            for pct in PERCENTILES:
                values = regional_percentiles.get((product_id, competitor, pct), [])
                status = _status_for_values(values)
                db.add(
                    CompetitorPricePercentile(
                        price_format_id=price_format_id,
                        product_id=product_id,
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
