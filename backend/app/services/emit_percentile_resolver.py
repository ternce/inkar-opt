from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import CompetitorPriceList, CompetitorPricePercentile, PriceFormatCompetitorAssignment
from .competitor_source_config import (
    MULTI_PRICE_PERCENTILE_MODE,
    canonical_competitor_source_key,
    emit_display_aliases_from_source_key,
    effective_percentile_mode,
)

GLOBAL_EMIT_PERCENTILE_STORAGE_PRICE_FORMAT_ID = int(
    os.getenv("GLOBAL_EMIT_PERCENTILE_STORAGE_PRICE_FORMAT_ID", "4")
)

REGIONAL_SCOPE = "regional"
KAZAKHSTAN_SCOPE = "kazakhstan"
KAZAKHSTAN_REGION = "Kazakhstan"


@dataclass(frozen=True)
class EmitPercentileGroup:
    price_list_id: int
    branch_name: str
    competitor_name: str
    source_key: str
    source_type: str


def global_emit_percentile_storage_price_format_id() -> int:
    return GLOBAL_EMIT_PERCENTILE_STORAGE_PRICE_FORMAT_ID


def is_global_emit_percentile_storage_price_format(price_format_id: int | None) -> bool:
    return int(price_format_id or 0) == global_emit_percentile_storage_price_format_id()


def is_emit_percentile_source_key(value: object) -> bool:
    return str(value or "").strip().startswith("emit:")


def _emit_storage_value_aliases(source_key: str, value: object) -> set[str]:
    text = str(value or "").strip()
    if not text:
        return {""}
    aliases = emit_display_aliases_from_source_key(source_key)
    if text in aliases:
        return {text, *aliases}
    return {text}


def _emit_storage_value_filter(column, *, source_key: str, value: object):
    values = sorted(_emit_storage_value_aliases(source_key, value))
    if len(values) == 1:
        return column == values[0]
    return column.in_(values)


def assigned_emit_percentile_groups(*, db: Session, target_price_format_id: int) -> list[EmitPercentileGroup]:
    rows = (
        db.execute(
            select(CompetitorPriceList, PriceFormatCompetitorAssignment)
            .join(
                PriceFormatCompetitorAssignment,
                PriceFormatCompetitorAssignment.competitor_price_list_id == CompetitorPriceList.id,
            )
            .where(PriceFormatCompetitorAssignment.price_format_id == int(target_price_format_id))
            .where(PriceFormatCompetitorAssignment.is_active.is_(True))
            .order_by(CompetitorPriceList.id.asc())
        )
        .all()
    )
    groups_by_key: dict[tuple[str, str, str], EmitPercentileGroup] = {}
    for price_list, assignment in rows:
        source_key = canonical_competitor_source_key(price_list)
        if not is_emit_percentile_source_key(source_key):
            continue
        if effective_percentile_mode(price_list, assignment.percentile_mode) != MULTI_PRICE_PERCENTILE_MODE:
            continue
        branch_name = str(price_list.branch_name or price_list.region or "").strip()
        competitor_name = str(price_list.competitor_name or price_list.supplier or price_list.display_name or "").strip()
        key = (branch_name, competitor_name, source_key)
        candidate = EmitPercentileGroup(
            price_list_id=int(price_list.id),
            branch_name=branch_name,
            competitor_name=competitor_name,
            source_key=source_key,
            source_type=str(price_list.source_type or "").strip(),
        )
        existing = groups_by_key.get(key)
        if existing is None or candidate.price_list_id < existing.price_list_id:
            groups_by_key[key] = candidate
    return sorted(groups_by_key.values(), key=lambda row: (row.branch_name, row.competitor_name, row.source_key))


def assigned_emit_percentile_group_keys(*, db: Session, target_price_format_id: int) -> set[tuple[str, str, str]]:
    return {
        (group.branch_name, group.competitor_name, group.source_key)
        for group in assigned_emit_percentile_groups(db=db, target_price_format_id=target_price_format_id)
    }


def emit_percentile_scope_filter(groups: list[EmitPercentileGroup] | tuple[EmitPercentileGroup, ...] | set[EmitPercentileGroup]):
    group_list = list(groups)
    if not group_list:
        return None
    regional_filters = [
        (
            (
                (
                    (func.coalesce(CompetitorPricePercentile.source_key, "") == group.source_key)
                    & _emit_storage_value_filter(
                        CompetitorPricePercentile.branch_name,
                        source_key=group.source_key,
                        value=group.branch_name,
                    )
                    & _emit_storage_value_filter(
                        CompetitorPricePercentile.competitor_name,
                        source_key=group.source_key,
                        value=group.competitor_name,
                    )
                )
                | (
                    (func.coalesce(CompetitorPricePercentile.source_key, "") == "")
                    & _emit_storage_value_filter(
                        CompetitorPricePercentile.branch_name,
                        source_key=group.source_key,
                        value=group.branch_name,
                    )
                    & _emit_storage_value_filter(
                        CompetitorPricePercentile.competitor_name,
                        source_key=group.source_key,
                        value=group.competitor_name,
                    )
                )
            )
            & (CompetitorPricePercentile.percentile_scope == REGIONAL_SCOPE)
        )
        for group in group_list
    ]
    kazakhstan_filters = [
        (
            (CompetitorPricePercentile.branch_name == KAZAKHSTAN_REGION)
            & (CompetitorPricePercentile.competitor_name == competitor_name)
            & (CompetitorPricePercentile.percentile_scope == KAZAKHSTAN_SCOPE)
        )
        for competitor_name in sorted({group.competitor_name for group in group_list if group.competitor_name})
    ]
    return or_(*(regional_filters + kazakhstan_filters))


def global_emit_percentile_base_stmt(
    *,
    db: Session,
    target_price_format_id: int,
    require_value: bool = False,
):
    groups = assigned_emit_percentile_groups(db=db, target_price_format_id=target_price_format_id)
    scoped_filter = emit_percentile_scope_filter(groups)
    if scoped_filter is None:
        return None
    stmt = (
        select(CompetitorPricePercentile)
        .where(CompetitorPricePercentile.price_format_id == global_emit_percentile_storage_price_format_id())
        .where(scoped_filter)
    )
    if require_value:
        stmt = stmt.where(CompetitorPricePercentile.value.is_not(None))
    return stmt


def load_global_emit_percentile_rows(
    *,
    db: Session,
    target_price_format_id: int,
    product_ids: list[int] | set[int] | None = None,
    product_id: int | None = None,
    percentile: int | None = None,
    require_value: bool = True,
) -> list[CompetitorPricePercentile]:
    stmt = global_emit_percentile_base_stmt(
        db=db,
        target_price_format_id=target_price_format_id,
        require_value=require_value,
    )
    if stmt is None:
        return []
    if product_id is not None:
        stmt = stmt.where(CompetitorPricePercentile.product_id == int(product_id))
    elif product_ids is not None:
        ids = sorted({int(item) for item in product_ids if int(item) > 0})
        if not ids:
            return []
        stmt = stmt.where(CompetitorPricePercentile.product_id.in_(ids))
    if percentile is not None:
        stmt = stmt.where(CompetitorPricePercentile.percentile == int(percentile))
    return db.execute(stmt).scalars().all()


def global_emit_percentile_rows_count(
    *,
    db: Session,
    target_price_format_id: int,
    require_value: bool = True,
) -> int:
    stmt = global_emit_percentile_base_stmt(
        db=db,
        target_price_format_id=target_price_format_id,
        require_value=require_value,
    )
    if stmt is None:
        return 0
    return int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


def has_global_emit_percentile_rows(*, db: Session, target_price_format_id: int) -> bool:
    stmt = global_emit_percentile_base_stmt(
        db=db,
        target_price_format_id=target_price_format_id,
        require_value=True,
    )
    if stmt is None:
        return False
    return db.execute(stmt.with_only_columns(CompetitorPricePercentile.id).limit(1)).scalar() is not None


def emit_row_matches_assigned_group(row: CompetitorPricePercentile, groups: set[tuple[str, str, str]]) -> bool:
    source_key = str(getattr(row, "source_key", "") or "")
    branch = str(row.branch_name or "")
    competitor = str(row.competitor_name or "")
    if row.percentile_scope == KAZAKHSTAN_SCOPE:
        return branch == KAZAKHSTAN_REGION and any(
            competitor in _emit_storage_value_aliases(active_source, active_competitor)
            for _branch, active_competitor, active_source in groups
        )
    return any(
        active_source == source_key
        and branch in _emit_storage_value_aliases(active_source, active_branch)
        and competitor in _emit_storage_value_aliases(active_source, active_competitor)
        for active_branch, active_competitor, active_source in groups
    ) or (
        not source_key
        and any(
            branch in _emit_storage_value_aliases(active_source, active_branch)
            and competitor in _emit_storage_value_aliases(active_source, active_competitor)
            for active_branch, active_competitor, active_source in groups
        )
    )


def emit_source_summary_mapping(row: Any, *, target_price_format_id: int) -> dict[str, Any]:
    return {
        "price_format_id": int(target_price_format_id),
        "source_type": row.source_type or "",
        "source_key": row.source_key or "",
        "competitor_price_list_id": row.competitor_price_list_id,
        "branch_name": row.branch_name or "",
        "competitor_name": row.competitor_name or "",
        "percentile_scope": row.percentile_scope or REGIONAL_SCOPE,
        "percentile": int(row.percentile),
        "sku_count": int(row.sku_count or 0),
        "source_count": int(row.source_count or 0),
        "generated_at": row.generated_at,
    }
