from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import (
    CompetitorPriceList,
    CompetitorPriceListItem,
    CompetitorPricePercentile,
    CompetitorPricePercentileSourceSummary,
    RegularCompetitorPricePercentile,
    RegularCompetitorPricePercentileSourceSummary,
)
from ..timezone import now_kz_naive


def _ids(values: Iterable[int | None]) -> list[int]:
    return [int(value) for value in values if value is not None]


def matched_positive_item_filter():
    return (
        CompetitorPriceListItem.distributor_price.is_not(None),
        CompetitorPriceListItem.distributor_price > 0,
        (
            (CompetitorPriceListItem.product_id.is_not(None))
            | (CompetitorPriceListItem.provisor_goods_id.is_not(None))
            | (CompetitorPriceListItem.matched_sku != "")
            | (CompetitorPriceListItem.distributor_goods_id != "")
        ),
    )


def live_price_list_item_counts(*, db: Session, price_list_ids: Iterable[int]) -> dict[int, tuple[int, int]]:
    ids = _ids(price_list_ids)
    if not ids:
        return {}
    item_counts = dict(
        db.execute(
            select(CompetitorPriceListItem.price_list_id, func.count())
            .where(CompetitorPriceListItem.price_list_id.in_(ids))
            .group_by(CompetitorPriceListItem.price_list_id)
        ).all()
    )
    matched_counts = dict(
        db.execute(
            select(CompetitorPriceListItem.price_list_id, func.count())
            .where(CompetitorPriceListItem.price_list_id.in_(ids))
            .where(*matched_positive_item_filter())
            .group_by(CompetitorPriceListItem.price_list_id)
        ).all()
    )
    return {
        price_list_id: (
            int(item_counts.get(price_list_id, 0) or 0),
            int(matched_counts.get(price_list_id, 0) or 0),
        )
        for price_list_id in ids
    }


def refresh_price_list_item_counters(*, db: Session, price_list_ids: Iterable[int]) -> dict[int, tuple[int, int]]:
    ids = _ids(price_list_ids)
    counts = live_price_list_item_counts(db=db, price_list_ids=ids)
    for price_list_id in ids:
        item_count, matched_count = counts.get(price_list_id, (0, 0))
        row = db.get(CompetitorPriceList, price_list_id)
        if row is not None:
            row.items_count = item_count
            row.matched_positive_items_count = matched_count
    return counts


def backfill_price_list_item_counters(*, db: Session, batch_size: int = 500) -> int:
    ids = [int(value) for value in db.execute(select(CompetitorPriceList.id).order_by(CompetitorPriceList.id.asc())).scalars()]
    updated = 0
    for index in range(0, len(ids), max(1, batch_size)):
        batch = ids[index : index + max(1, batch_size)]
        refresh_price_list_item_counters(db=db, price_list_ids=batch)
        updated += len(batch)
    return updated


def live_emit_percentile_source_summary_rows(*, db: Session, price_format_id: int | None = None) -> list[dict]:
    stmt = (
        select(
            CompetitorPricePercentile.price_format_id,
            CompetitorPricePercentile.source_type,
            CompetitorPricePercentile.source_key,
            CompetitorPricePercentile.competitor_price_list_id,
            CompetitorPricePercentile.branch_name,
            CompetitorPricePercentile.competitor_name,
            CompetitorPricePercentile.percentile_scope,
            CompetitorPricePercentile.percentile,
            func.count(func.distinct(CompetitorPricePercentile.product_id)).label("sku_count"),
            func.sum(CompetitorPricePercentile.source_count).label("source_count"),
            func.max(CompetitorPricePercentile.updated_at).label("generated_at"),
        )
        .group_by(
            CompetitorPricePercentile.price_format_id,
            CompetitorPricePercentile.source_type,
            CompetitorPricePercentile.source_key,
            CompetitorPricePercentile.competitor_price_list_id,
            CompetitorPricePercentile.branch_name,
            CompetitorPricePercentile.competitor_name,
            CompetitorPricePercentile.percentile_scope,
            CompetitorPricePercentile.percentile,
        )
    )
    if price_format_id is not None:
        stmt = stmt.where(CompetitorPricePercentile.price_format_id == int(price_format_id))
    return [
        {
            "price_format_id": int(row.price_format_id),
            "source_type": row.source_type or "",
            "source_key": row.source_key or "",
            "competitor_price_list_id": int(row.competitor_price_list_id) if row.competitor_price_list_id is not None else None,
            "branch_name": row.branch_name or "",
            "competitor_name": row.competitor_name or "",
            "percentile_scope": row.percentile_scope or "",
            "percentile": int(row.percentile),
            "sku_count": int(row.sku_count or 0),
            "source_count": int(row.source_count or 0),
            "generated_at": row.generated_at,
            "updated_at": now_kz_naive(),
        }
        for row in db.execute(stmt).all()
    ]


def refresh_emit_percentile_source_summaries(*, db: Session, price_format_id: int | None = None) -> int:
    if price_format_id is None:
        db.execute(delete(CompetitorPricePercentileSourceSummary))
    else:
        db.execute(
            delete(CompetitorPricePercentileSourceSummary).where(
                CompetitorPricePercentileSourceSummary.price_format_id == int(price_format_id)
            )
        )
    rows = live_emit_percentile_source_summary_rows(db=db, price_format_id=price_format_id)
    if rows:
        db.bulk_insert_mappings(CompetitorPricePercentileSourceSummary, rows)
    return len(rows)


def live_regular_percentile_source_summary_rows(*, db: Session, competitor_identities: set[str] | None = None) -> list[dict]:
    stmt = (
        select(
            RegularCompetitorPricePercentile.competitor_identity,
            func.min(RegularCompetitorPricePercentile.competitor_name).label("competitor_name"),
            RegularCompetitorPricePercentile.percentile,
            func.count(func.distinct(RegularCompetitorPricePercentile.product_id)).label("sku_count"),
            func.sum(RegularCompetitorPricePercentile.source_count).label("source_count"),
            func.max(RegularCompetitorPricePercentile.calculated_at).label("generated_at"),
        )
        .group_by(
            RegularCompetitorPricePercentile.competitor_identity,
            RegularCompetitorPricePercentile.percentile,
        )
    )
    identities = {str(item or "").strip() for item in (competitor_identities or set()) if str(item or "").strip()}
    if identities:
        stmt = stmt.where(RegularCompetitorPricePercentile.competitor_identity.in_(identities))
    return [
        {
            "competitor_identity": row.competitor_identity or "",
            "competitor_name": row.competitor_name or "",
            "percentile": int(row.percentile),
            "sku_count": int(row.sku_count or 0),
            "source_count": int(row.source_count or 0),
            "generated_at": row.generated_at,
            "updated_at": now_kz_naive(),
        }
        for row in db.execute(stmt).all()
    ]


def refresh_regular_percentile_source_summaries(
    *,
    db: Session,
    competitor_identities: set[str] | None = None,
) -> int:
    identities = {str(item or "").strip() for item in (competitor_identities or set()) if str(item or "").strip()}
    if identities:
        db.execute(
            delete(RegularCompetitorPricePercentileSourceSummary).where(
                RegularCompetitorPricePercentileSourceSummary.competitor_identity.in_(identities)
            )
        )
    else:
        db.execute(delete(RegularCompetitorPricePercentileSourceSummary))
    rows = live_regular_percentile_source_summary_rows(db=db, competitor_identities=identities)
    if rows:
        db.bulk_insert_mappings(RegularCompetitorPricePercentileSourceSummary, rows)
    return len(rows)


def backfill_competitor_assignment_read_models(*, db: Session) -> dict[str, int]:
    return {
        "price_list_counters": backfill_price_list_item_counters(db=db),
        "emit_percentile_source_summaries": refresh_emit_percentile_source_summaries(db=db),
        "regular_percentile_source_summaries": refresh_regular_percentile_source_summaries(db=db),
    }
