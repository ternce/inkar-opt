from __future__ import annotations

from collections import defaultdict
import logging
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import CompetitorPriceList, CompetitorPriceListItem, CompetitorPricePercentile, Product
from ..timezone import now_kz_naive
from .competitor_assignments import get_assigned_competitor_price_lists


logger = logging.getLogger(__name__)


PERCENTILES = (10, 20, 30, 40, 60)
DEFAULT_BRANCH = "Без филиала"
REGIONAL_SCOPE = "regional"
KAZAKHSTAN_SCOPE = "kazakhstan"
KAZAKHSTAN_REGION = "Kazakhstan"


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


def recalculate_competitor_percentiles(*, db: Session, price_format_id: int) -> dict[str, int]:
    existing_rows = int(
        db.execute(
            select(func.count(CompetitorPricePercentile.id))
            .where(CompetitorPricePercentile.price_format_id == price_format_id)
        ).scalar_one()
        or 0
    )
    db.execute(delete(CompetitorPricePercentile).where(CompetitorPricePercentile.price_format_id == price_format_id))

    selected = get_assigned_competitor_price_lists(db=db, price_format_id=price_format_id)
    selected_ids = [int(item.price_list.id) for item in selected]
    product_ids = [int(product_id) for product_id in db.execute(select(Product.id)).scalars().all()]
    rows = (
        db.execute(
            select(CompetitorPriceList, CompetitorPriceListItem)
            .join(CompetitorPriceListItem, CompetitorPriceListItem.price_list_id == CompetitorPriceList.id)
            .where(CompetitorPriceList.id.in_(selected_ids))
            .where(CompetitorPriceListItem.product_id.is_not(None))
            .where(CompetitorPriceListItem.distributor_price.is_not(None))
            .order_by(
                CompetitorPriceList.id.asc(),
                CompetitorPriceListItem.product_id.asc(),
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
    # same account/SKU, the latest parsed row (largest item id) wins. Equal
    # prices from different accounts are deliberately preserved.
    latest_account_prices: dict[tuple[int, str, str, int], Decimal] = {}
    for price_list, item in rows:
        product_id = int(item.product_id or 0)
        price = _as_decimal(item.distributor_price)
        if not product_id or price is None:
            continue
        branch = _branch_name(price_list)
        competitor = _competitor_name(price_list)
        source_groups.add((branch, competitor))
        latest_account_prices[(product_id, branch, competitor, int(price_list.id))] = price

    grouped: dict[tuple[int, str, str], list[Decimal]] = defaultdict(list)
    for key, price in latest_account_prices.items():
        product_id, branch, competitor, _price_list_id = key
        grouped[(product_id, branch, competitor)].append(price)

    now = now_kz_naive()
    inserted = 0
    products_with_competitors: set[int] = set()
    regional_percentiles: dict[tuple[int, str, int], list[Decimal]] = defaultdict(list)

    for branch, competitor in sorted(source_groups):
        for product_id in product_ids:
            values = grouped.get((product_id, branch, competitor), [])
            source_count = len(values)
            if values:
                products_with_competitors.add(product_id)
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
                        updated_at=now,
                    )
                )
                inserted += 1

    for competitor in sorted({competitor for _branch, competitor in source_groups}):
        for product_id in product_ids:
            for pct in PERCENTILES:
                values = regional_percentiles.get((product_id, competitor, pct), [])
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
        "rows_deleted": existing_rows,
    }
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
