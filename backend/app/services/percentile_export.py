from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CompetitorPricePercentile
from .competitors.percentiles.sources import (
    PERCENTILE_SOURCE_COMPETITOR,
    PERCENTILE_SOURCE_EMIT,
    percentile_source_id,
)


def percentile_export_source_names(row: CompetitorPricePercentile) -> tuple[str, str]:
    """Return the selectable export identities represented by a percentile row."""

    emit_source = percentile_source_id(
        percentile_source=PERCENTILE_SOURCE_EMIT,
        price_format_id=row.price_format_id,
        scope=row.percentile_scope,
        source_key=row.source_key,
        region=row.branch_name,
        competitor=row.competitor_name,
        percentile=row.percentile,
    )
    competitor_source = percentile_source_id(
        percentile_source=PERCENTILE_SOURCE_COMPETITOR,
        price_format_id=row.price_format_id,
        scope="global",
        source_key=row.source_key,
        region="",
        competitor=row.competitor_name,
        percentile=row.percentile,
    )
    return f"percentile:{emit_source}", f"percentile:{competitor_source}"


def load_percentile_export_prices(
    *,
    db: Session,
    price_format_id: int,
    product_ids: list[int],
    selected_source_names: set[str],
) -> dict[int, dict[str, Decimal]]:
    if not product_ids or not selected_source_names:
        return {}

    rows = (
        db.execute(
            select(CompetitorPricePercentile)
            .where(CompetitorPricePercentile.price_format_id == price_format_id)
            .where(CompetitorPricePercentile.product_id.in_(product_ids))
            .where(CompetitorPricePercentile.value.is_not(None))
            .order_by(
                CompetitorPricePercentile.product_id.asc(),
                CompetitorPricePercentile.source_key.asc(),
                CompetitorPricePercentile.percentile_scope.asc(),
                CompetitorPricePercentile.percentile.asc(),
                CompetitorPricePercentile.id.asc(),
            )
        )
        .scalars()
        .all()
    )

    out: dict[int, dict[str, Decimal]] = {}
    for row in rows:
        if row.value is None:
            continue
        try:
            value = Decimal(str(row.value))
        except Exception:
            continue
        for source_name in percentile_export_source_names(row):
            if source_name not in selected_source_names:
                continue
            out.setdefault(int(row.product_id), {})[source_name] = value
    return out
