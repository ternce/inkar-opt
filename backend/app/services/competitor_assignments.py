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


@dataclass
class EmitAssignmentPropagationResult:
    created_assignment_ids: list[int]
    reused_assignment_ids: list[int]
    reactivated_assignment_ids: list[int]
    skipped_incompatible_assignment_ids: list[int]
    affected_price_format_ids: list[int]

    @property
    def created_count(self) -> int:
        return len(self.created_assignment_ids)

    @property
    def reused_count(self) -> int:
        return len(self.reused_assignment_ids)

    @property
    def reactivated_count(self) -> int:
        return len(self.reactivated_assignment_ids)

    @property
    def skipped_incompatible_count(self) -> int:
        return len(self.skipped_incompatible_assignment_ids)

    def to_dict(self) -> dict[str, object]:
        return {
            "created_assignment_ids": self.created_assignment_ids,
            "created_count": self.created_count,
            "reused_assignment_ids": self.reused_assignment_ids,
            "reused_count": self.reused_count,
            "reactivated_assignment_ids": self.reactivated_assignment_ids,
            "reactivated_count": self.reactivated_count,
            "skipped_incompatible_assignment_ids": self.skipped_incompatible_assignment_ids,
            "skipped_incompatible_count": self.skipped_incompatible_count,
            "affected_price_format_ids": self.affected_price_format_ids,
        }


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


def _all_active_price_formats(db: Session) -> list[PriceFormat]:
    # PriceFormat has no active/deleted/archive flag in the current model.
    return db.execute(select(PriceFormat).order_by(PriceFormat.id.asc())).scalars().all()


def propagate_emit_assignments_to_price_formats(
    *,
    db: Session,
    emit_price_list_ids: list[int] | None = None,
    price_format_ids: list[int] | None = None,
) -> EmitAssignmentPropagationResult:
    target_price_list_ids = {int(item) for item in (emit_price_list_ids or []) if int(item) > 0}
    pf_filter_ids = {int(item) for item in (price_format_ids or []) if int(item) > 0}

    price_formats = _all_active_price_formats(db)
    if pf_filter_ids:
        price_formats = [pf for pf in price_formats if int(pf.id) in pf_filter_ids]
    price_format_ids_ordered = [int(pf.id) for pf in price_formats]

    stmt = select(CompetitorPriceList)
    if target_price_list_ids:
        stmt = stmt.where(CompetitorPriceList.id.in_(target_price_list_ids))
    rows = db.execute(stmt.order_by(CompetitorPriceList.id.asc())).scalars().all()
    emit_rows = [row for row in rows if _is_active_emit_price_list(row)]
    emit_row_ids = [int(row.id) for row in emit_rows]
    metadata_by_price_list_id: dict[int, PriceFormatCompetitorAssignment] = {}
    if emit_row_ids:
        metadata_rows = (
            db.execute(
                select(PriceFormatCompetitorAssignment)
                .where(PriceFormatCompetitorAssignment.competitor_price_list_id.in_(emit_row_ids))
                .where(PriceFormatCompetitorAssignment.is_active.is_(True))
                .order_by(PriceFormatCompetitorAssignment.updated_at.desc(), PriceFormatCompetitorAssignment.id.desc())
            )
            .scalars()
            .all()
        )
        for existing_assignment in metadata_rows:
            mode = str(existing_assignment.percentile_mode or "").strip()
            if mode and mode != MULTI_PRICE_PERCENTILE_MODE:
                continue
            metadata_by_price_list_id.setdefault(int(existing_assignment.competitor_price_list_id), existing_assignment)

    created: list[int] = []
    reused: list[int] = []
    reactivated: list[int] = []
    skipped: list[int] = []
    affected: set[int] = set()
    now = datetime.utcnow()

    logger.info(
        "[EMIT_ASSIGNMENT_PROPAGATION] action=start price_format_ids=%s price_list_ids=%s",
        price_format_ids_ordered,
        [int(row.id) for row in emit_rows],
    )

    for pf in price_formats:
        for price_list in emit_rows:
            assignment = get_assignment(
                db=db,
                price_format_id=int(pf.id),
                competitor_price_list_id=int(price_list.id),
            )
            if assignment is None:
                template = metadata_by_price_list_id.get(int(price_list.id))
                assignment = PriceFormatCompetitorAssignment(
                    price_format_id=int(pf.id),
                    competitor_price_list_id=int(price_list.id),
                    is_active=True,
                    coefficient=float(template.coefficient) if template is not None else 1.0,
                    percentile_mode=MULTI_PRICE_PERCENTILE_MODE,
                    source_mode=str(template.source_mode or "").strip() if template is not None else "",
                    created_at=now,
                    updated_at=now,
                )
                db.add(assignment)
                db.flush()
                created.append(int(assignment.id))
                affected.add(int(pf.id))
                continue

            mode = str(assignment.percentile_mode or "").strip()
            if mode and mode != MULTI_PRICE_PERCENTILE_MODE:
                skipped.append(int(assignment.id))
                logger.warning(
                    "[EMIT_ASSIGNMENT_PROPAGATION] action=skip reason=incompatible_percentile_mode "
                    "price_format_id=%s price_list_id=%s assignment_id=%s percentile_mode=%s",
                    int(pf.id),
                    int(price_list.id),
                    int(assignment.id),
                    mode,
                )
                continue

            if not assignment.is_active:
                assignment.is_active = True
                assignment.percentile_mode = MULTI_PRICE_PERCENTILE_MODE
                assignment.updated_at = now
                reactivated.append(int(assignment.id))
                affected.add(int(pf.id))
                continue

            if not mode:
                assignment.percentile_mode = MULTI_PRICE_PERCENTILE_MODE
                assignment.updated_at = now
                affected.add(int(pf.id))
            reused.append(int(assignment.id))
            affected.add(int(pf.id))

    result = EmitAssignmentPropagationResult(
        created_assignment_ids=created,
        reused_assignment_ids=reused,
        reactivated_assignment_ids=reactivated,
        skipped_incompatible_assignment_ids=skipped,
        affected_price_format_ids=sorted(affected),
    )
    logger.info(
        "[EMIT_ASSIGNMENT_PROPAGATION] action=end price_format_ids=%s price_list_ids=%s created=%s reused=%s "
        "reactivated=%s skipped_incompatible=%s affected_price_format_ids=%s",
        price_format_ids_ordered,
        [int(row.id) for row in emit_rows],
        result.created_count,
        result.reused_count,
        result.reactivated_count,
        result.skipped_incompatible_count,
        result.affected_price_format_ids,
    )
    return result


def propagate_emit_assignments_to_new_price_format(*, db: Session, price_format_id: int) -> int:
    result = propagate_emit_assignments_to_price_formats(db=db, price_format_ids=[int(price_format_id)])
    return result.created_count


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
