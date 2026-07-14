from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    CompetitorPriceList,
    CompetitorPriceListItem,
    PriceFormat,
    PriceFormatCompetitorAssignment,
)
from .competitor_source_config import MULTI_PRICE_PERCENTILE_MODE, default_percentile_mode_for_source


logger = logging.getLogger(__name__)

INACTIVE_REFRESH_STATUSES = {"failed", "error", "stale"}


def source_name(row: CompetitorPriceList) -> str:
    return f"{row.source_type}:{row.source_key}"


def branch_key(value: object) -> str:
    return str(value or "").strip().casefold()


def row_branch_values(row: CompetitorPriceList) -> set[str]:
    values = {
        row.branch_id,
        row.branch_code,
        row.branch_name,
        row.region,
    }
    return {branch_key(value) for value in values if branch_key(value)}


def price_format_branch_matches(row: CompetitorPriceList, pf: PriceFormat, region: str | None = None) -> bool:
    target = branch_key(region or pf.branch)
    if not target:
        return True
    values = row_branch_values(row)
    if not values:
        return False
    return target in values or any(target in value or value in target for value in values)


def branch_visibility_metadata(row: CompetitorPriceList, pf: PriceFormat, region: str | None = None) -> dict[str, object]:
    target_raw = str(region or pf.branch or "").strip()
    target = branch_key(target_raw)
    values = row_branch_values(row)
    if not target:
        return {
            "visibleForFormatBranch": True,
            "branchMatchReason": "no_format_branch_filter",
            "branchMismatchReason": "",
        }
    if not values:
        return {
            "visibleForFormatBranch": False,
            "branchMatchReason": "",
            "branchMismatchReason": f"no_branch_values_for_format_branch:{target_raw}",
        }
    if target in values:
        return {
            "visibleForFormatBranch": True,
            "branchMatchReason": "exact_branch_match",
            "branchMismatchReason": "",
        }
    if any(target in value or value in target for value in values):
        return {
            "visibleForFormatBranch": True,
            "branchMatchReason": "loose_branch_text_match",
            "branchMismatchReason": "",
        }
    return {
        "visibleForFormatBranch": False,
        "branchMatchReason": "",
        "branchMismatchReason": f"branch_mismatch:{target_raw}",
    }


def visible_item_counts(db: Session, rows: list[CompetitorPriceList]) -> dict[int, int]:
    ids = [int(row.id) for row in rows]
    if not ids:
        return {}
    return dict(
        db.execute(
            select(CompetitorPriceListItem.price_list_id, func.count(CompetitorPriceListItem.id))
            .where(CompetitorPriceListItem.price_list_id.in_(ids))
            .group_by(CompetitorPriceListItem.price_list_id)
        ).all()
    )


def list_global_competitor_price_lists_for_format(
    *,
    db: Session,
    price_format: PriceFormat,
    account_id: str | None = None,
    region: str | None = None,
    branch_scoped: bool = False,
) -> list[CompetitorPriceList]:
    stmt = select(CompetitorPriceList)
    if account_id:
        stmt = stmt.where(CompetitorPriceList.account_id == str(account_id))
    rows = db.execute(stmt.order_by(CompetitorPriceList.updated_at.desc(), CompetitorPriceList.id.desc())).scalars().all()
    for row in rows:
        meta = branch_visibility_metadata(row, price_format, region)
        setattr(row, "_visible_for_format_branch", bool(meta["visibleForFormatBranch"]))
        setattr(row, "_branch_match_reason", str(meta["branchMatchReason"]))
        setattr(row, "_branch_mismatch_reason", str(meta["branchMismatchReason"]))
    if branch_scoped or region:
        rows = [row for row in rows if bool(getattr(row, "_visible_for_format_branch", False))]
    counts = visible_item_counts(db, rows)
    rows = [row for row in rows if int(counts.get(int(row.id), 0)) > 0]
    return _dedupe_global_rows(rows)


def _dedupe_global_rows(rows: list[CompetitorPriceList]) -> list[CompetitorPriceList]:
    best: dict[tuple[str, ...], CompetitorPriceList] = {}
    for row in rows:
        key = (
            str(row.source_type or ""),
            str(row.source_key or ""),
            str(row.account_id or ""),
            str(row.external_price_list_id or ""),
            branch_key(row.branch_id or row.branch_code or row.branch_name or row.region),
            branch_key(row.competitor_name or row.supplier or row.display_name),
        )
        current = best.get(key)
        if current is None or _row_sort_key(row) > _row_sort_key(current):
            best[key] = row
    return sorted(best.values(), key=_row_sort_key, reverse=True)


def _row_sort_key(row: CompetitorPriceList) -> tuple[str, datetime, int]:
    return (str(row.source_updated_at or ""), row.updated_at or datetime.min, int(row.id or 0))


@dataclass(frozen=True)
class AssignedCompetitorPriceList:
    price_list: CompetitorPriceList
    assignment: PriceFormatCompetitorAssignment


def get_assigned_competitor_price_lists(
    *,
    db: Session,
    price_format_id: int,
    active_only: bool = True,
) -> list[AssignedCompetitorPriceList]:
    stmt = (
        select(CompetitorPriceList, PriceFormatCompetitorAssignment)
        .join(
            PriceFormatCompetitorAssignment,
            PriceFormatCompetitorAssignment.competitor_price_list_id == CompetitorPriceList.id,
        )
        .where(PriceFormatCompetitorAssignment.price_format_id == price_format_id)
    )
    if active_only:
        stmt = stmt.where(PriceFormatCompetitorAssignment.is_active.is_(True))
    rows = db.execute(stmt.order_by(CompetitorPriceList.updated_at.desc(), CompetitorPriceList.id.desc())).all()
    return [AssignedCompetitorPriceList(price_list=row, assignment=assignment) for row, assignment in rows]


def get_all_assigned_competitor_price_lists(*, db: Session) -> list[AssignedCompetitorPriceList]:
    rows = (
        db.execute(
            select(CompetitorPriceList, PriceFormatCompetitorAssignment)
            .join(
                PriceFormatCompetitorAssignment,
                PriceFormatCompetitorAssignment.competitor_price_list_id == CompetitorPriceList.id,
            )
            .where(PriceFormatCompetitorAssignment.is_active.is_(True))
            .order_by(CompetitorPriceList.updated_at.desc(), CompetitorPriceList.id.desc())
        )
        .all()
    )
    return [AssignedCompetitorPriceList(price_list=row, assignment=assignment) for row, assignment in rows]


def get_assignment(
    *,
    db: Session,
    price_format_id: int,
    competitor_price_list_id: int,
) -> PriceFormatCompetitorAssignment | None:
    return (
        db.execute(
            select(PriceFormatCompetitorAssignment)
            .where(PriceFormatCompetitorAssignment.price_format_id == price_format_id)
            .where(PriceFormatCompetitorAssignment.competitor_price_list_id == competitor_price_list_id)
        )
        .scalars()
        .first()
    )


def upsert_assignment(
    *,
    db: Session,
    price_format_id: int,
    competitor_price_list_id: int,
    coefficient: float = 1.0,
    is_active: bool = True,
) -> PriceFormatCompetitorAssignment:
    row = get_assignment(db=db, price_format_id=price_format_id, competitor_price_list_id=competitor_price_list_id)
    now = datetime.utcnow()
    if row is None:
        price_list = db.get(CompetitorPriceList, competitor_price_list_id)
        row = PriceFormatCompetitorAssignment(
            price_format_id=price_format_id,
            competitor_price_list_id=competitor_price_list_id,
            coefficient=coefficient,
            is_active=is_active,
            percentile_mode=default_percentile_mode_for_source(price_list) if price_list is not None else "",
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.coefficient = coefficient
        row.is_active = is_active
        row.updated_at = now
    return row


def _is_active_emit_price_list(row: CompetitorPriceList) -> bool:
    if default_percentile_mode_for_source(row) != MULTI_PRICE_PERCENTILE_MODE:
        return False
    status = str(row.last_refresh_status or "").strip().casefold()
    return status not in INACTIVE_REFRESH_STATUSES


def propagate_emit_assignments_to_new_price_format(*, db: Session, price_format_id: int) -> int:
    price_format_id = int(price_format_id)
    price_format = db.get(PriceFormat, price_format_id)
    if price_format is None:
        return 0
    existing_ids = {
        int(row.competitor_price_list_id)
        for row in db.execute(
            select(PriceFormatCompetitorAssignment.competitor_price_list_id)
            .where(PriceFormatCompetitorAssignment.price_format_id == price_format_id)
        )
    }
    emit_rows = list_global_competitor_price_lists_for_format(
        db=db,
        price_format=price_format,
    )
    emit_rows = [row for row in emit_rows if _is_active_emit_price_list(row)]
    target_ids = [int(row.id) for row in emit_rows if int(row.id) not in existing_ids]
    if not target_ids:
        return 0

    metadata_by_price_list_id: dict[int, PriceFormatCompetitorAssignment] = {}
    metadata_rows = (
        db.execute(
            select(PriceFormatCompetitorAssignment)
            .where(PriceFormatCompetitorAssignment.competitor_price_list_id.in_(target_ids))
            .where(PriceFormatCompetitorAssignment.is_active.is_(True))
            .order_by(PriceFormatCompetitorAssignment.updated_at.desc(), PriceFormatCompetitorAssignment.id.desc())
        )
        .scalars()
        .all()
    )
    for assignment in metadata_rows:
        metadata_by_price_list_id.setdefault(int(assignment.competitor_price_list_id), assignment)

    now = datetime.utcnow()
    by_id = {int(row.id): row for row in emit_rows}
    created = 0
    for price_list_id in target_ids:
        template = metadata_by_price_list_id.get(price_list_id)
        price_list = by_id.get(price_list_id)
        db.add(
            PriceFormatCompetitorAssignment(
                price_format_id=price_format_id,
                competitor_price_list_id=price_list_id,
                is_active=True,
                coefficient=float(template.coefficient) if template is not None else 1.0,
                percentile_mode=(
                    str(template.percentile_mode or "").strip()
                    if template is not None and str(template.percentile_mode or "").strip()
                    else default_percentile_mode_for_source(price_list) if price_list is not None else MULTI_PRICE_PERCENTILE_MODE
                ),
                source_mode=str(template.source_mode or "").strip() if template is not None else "",
                created_at=now,
                updated_at=now,
            )
        )
        created += 1
    logger.info(
        "[EMIT_ASSIGNMENT_PROPAGATION] price_format_id=%s emit_price_list_ids=%s assignments_created=%s",
        price_format_id,
        target_ids,
        created,
    )
    return created


def set_competitor_assignments(
    *,
    db: Session,
    price_format: PriceFormat,
    selected_ids: list[int],
    coefficients: dict[int, float] | None = None,
) -> None:
    coefficients = coefficients or {}
    selected_set = {int(item) for item in selected_ids}
    candidates = list_global_competitor_price_lists_for_format(db=db, price_format=price_format)
    selectable_ids = {int(row.id) for row in candidates}
    selected_set = selected_set & selectable_ids
    existing = {
        int(row.competitor_price_list_id): row
        for row in db.execute(
            select(PriceFormatCompetitorAssignment).where(PriceFormatCompetitorAssignment.price_format_id == price_format.id)
        ).scalars()
    }
    now = datetime.utcnow()
    for source_id, assignment in existing.items():
        assignment.is_active = source_id in selected_set
        if source_id in coefficients:
            assignment.coefficient = float(coefficients[source_id])
        assignment.updated_at = now
    for source_id in selected_set - set(existing):
        upsert_assignment(
            db=db,
            price_format_id=int(price_format.id),
            competitor_price_list_id=source_id,
            coefficient=float(coefficients.get(source_id, 1.0)),
            is_active=True,
        )
    for source_id in selected_set & set(existing):
        assignment = existing[source_id]
        if not (assignment.percentile_mode or "").strip():
            price_list = db.get(CompetitorPriceList, source_id)
            if price_list is not None:
                assignment.percentile_mode = default_percentile_mode_for_source(price_list)
