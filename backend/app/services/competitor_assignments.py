from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    CompetitorPriceList,
    CompetitorPriceListItem,
    PriceFormat,
    PriceFormatCompetitorAssignment,
)


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
        row = PriceFormatCompetitorAssignment(
            price_format_id=price_format_id,
            competitor_price_list_id=competitor_price_list_id,
            coefficient=coefficient,
            is_active=is_active,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.coefficient = coefficient
        row.is_active = is_active
        row.updated_at = now
    return row


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
