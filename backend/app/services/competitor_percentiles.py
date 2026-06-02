from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import CompetitorPriceList, CompetitorPriceListItem, CompetitorPricePercentile
from .competitor_assignments import get_assigned_competitor_price_lists


PERCENTILES = (10, 20, 30, 40, 50, 60, 70, 80, 90)
DEFAULT_BRANCH = "Без филиала"


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


def recalculate_competitor_percentiles(*, db: Session, price_format_id: int) -> int:
    db.execute(delete(CompetitorPricePercentile).where(CompetitorPricePercentile.price_format_id == price_format_id))

    selected = get_assigned_competitor_price_lists(db=db, price_format_id=price_format_id)
    selected_ids = [int(item.price_list.id) for item in selected]
    rows = (
        db.execute(
            select(CompetitorPriceList, CompetitorPriceListItem)
            .join(CompetitorPriceListItem, CompetitorPriceListItem.price_list_id == CompetitorPriceList.id)
            .where(CompetitorPriceList.id.in_(selected_ids))
            .where(CompetitorPriceListItem.product_id.is_not(None))
            .where(CompetitorPriceListItem.distributor_price.is_not(None))
        )
        .all()
        if selected_ids
        else []
    )

    grouped: dict[tuple[int, str, str], list[Decimal]] = defaultdict(list)
    for price_list, item in rows:
        if item.product_id is None:
            continue
        price = _as_decimal(item.distributor_price)
        if price is None:
            continue
        branch = (price_list.branch_name or "").strip() or DEFAULT_BRANCH
        competitor = (price_list.competitor_name or price_list.supplier or price_list.display_name or "").strip()
        if not competitor:
            competitor = price_list.source_type
        grouped[(int(item.product_id), branch, competitor)].append(price)

    now = datetime.utcnow()
    inserted = 0
    for (product_id, branch, competitor), values in grouped.items():
        if not values:
            continue
        for pct in PERCENTILES:
            db.add(
                CompetitorPricePercentile(
                    price_format_id=price_format_id,
                    product_id=product_id,
                    branch_name=branch,
                    competitor_name=competitor,
                    percentile=pct,
                    value=float(_percentile(values, pct)),
                    source_count=len(values),
                    updated_at=now,
                )
            )
            inserted += 1
    return inserted
