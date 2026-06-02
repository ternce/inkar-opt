from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ....models import CompetitorPricePercentile, PriceFormat


def list_percentile_sources(*, db: Session, price_format_code: str | None = None) -> list[dict]:
    """Group stored percentile rows into source-like UI records.

    The percentile engine is already partially present in
    competitor_price_percentiles. Stage 1 only exposes it as a management view.
    """

    stmt = (
        select(
            CompetitorPricePercentile.price_format_id,
            CompetitorPricePercentile.branch_name,
            CompetitorPricePercentile.competitor_name,
            CompetitorPricePercentile.percentile,
            func.count(func.distinct(CompetitorPricePercentile.product_id)).label("sku_count"),
            func.sum(CompetitorPricePercentile.source_count).label("source_count"),
            func.max(CompetitorPricePercentile.updated_at).label("generated_at"),
        )
        .group_by(
            CompetitorPricePercentile.price_format_id,
            CompetitorPricePercentile.branch_name,
            CompetitorPricePercentile.competitor_name,
            CompetitorPricePercentile.percentile,
        )
    )
    if price_format_code:
        pf = db.execute(select(PriceFormat).where(PriceFormat.code == price_format_code.strip())).scalars().first()
        if pf is None:
            return []
        stmt = stmt.where(CompetitorPricePercentile.price_format_id == pf.id)

    rows = db.execute(stmt.order_by(CompetitorPricePercentile.branch_name.asc(), CompetitorPricePercentile.competitor_name.asc())).all()
    out: list[dict] = []
    for row in rows:
        generated_at = row.generated_at.isoformat() if row.generated_at else ""
        out.append(
            {
                "id": f"{row.price_format_id}:{row.branch_name}:{row.competitor_name}:p{row.percentile}",
                "priceFormatId": row.price_format_id,
                "region": row.branch_name or "Без филиала",
                "competitor": row.competitor_name or "",
                "percentile": int(row.percentile),
                "name": f"{row.branch_name or 'Без филиала'} — {row.competitor_name or 'Конкурент'} — P{int(row.percentile)}",
                "skuCount": int(row.sku_count or 0),
                "sourceCount": int(row.source_count or 0),
                "generatedAt": generated_at,
                "sourceType": "percentile",
            }
        )
    return out
