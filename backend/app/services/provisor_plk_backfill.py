from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    CompetitorPrice,
    CompetitorPriceList,
    CompetitorPriceListItem,
    PriceFormatCompetitorAssignment,
)


def canonical_provisor_source_key(external_price_list_id: object) -> str:
    return f"plk:{str(external_price_list_id or '').strip()}"


def _json_loads(value: str | None, fallback: Any) -> Any:
    try:
        data = json.loads(value or "")
        return fallback if data is None else data
    except Exception:
        return fallback


def _region_meta(region: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in str(region or "").split(";"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        out[key.strip()] = value.strip()
    return out


def _without_aliases(region: str | None) -> str:
    parts = [part.strip() for part in str(region or "").split(";") if part.strip()]
    return "; ".join(part for part in parts if not part.strip().startswith("aliasesJson:"))


def _alias_for(row: CompetitorPriceList) -> dict[str, object]:
    return {
        "row_id": int(row.id),
        "source_key": str(row.source_key or ""),
        "account_id": str(row.account_id or ""),
        "account_login": str(row.account_login or ""),
        "external_price_list_id": str(row.external_price_list_id or ""),
        "display_name": str(row.display_name or row.supplier or ""),
        "last_success_at": row.last_success_at.isoformat() if row.last_success_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }


def _existing_aliases(row: CompetitorPriceList) -> list[dict[str, object]]:
    marker = "aliasesJson:"
    region = str(row.region or "")
    if marker not in region:
        return []
    raw = region.split(marker, 1)[1].strip()
    data = _json_loads(raw, [])
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _merge_aliases(rows: list[CompetitorPriceList]) -> list[dict[str, object]]:
    aliases: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        for alias in [*_existing_aliases(row), _alias_for(row)]:
            key = (
                str(alias.get("account_id") or ""),
                str(alias.get("external_price_list_id") or ""),
                str(alias.get("source_key") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            aliases.append(alias)
    return aliases


def _apply_aliases(row: CompetitorPriceList, aliases: list[dict[str, object]]) -> None:
    base = _without_aliases(row.region)
    alias_text = json.dumps(aliases, ensure_ascii=False, default=str)
    row.region = f"{base}; aliasesJson:{alias_text}" if base else f"aliasesJson:{alias_text}"


def _row_rank(row: CompetitorPriceList, item_count: int) -> tuple[int, datetime, datetime, datetime, int]:
    successful = bool(row.last_success_at) or str(row.last_refresh_status or "").strip() in {
        "updated",
        "checked_unchanged",
        "success_zero_items",
    }
    return (
        1 if successful and item_count > 0 else 0,
        row.last_success_at or datetime.min,
        row.updated_at or datetime.min,
        row.last_checked_at or datetime.min,
        int(row.id or 0),
    )


@dataclass(frozen=True)
class _GroupPlan:
    external_price_list_id: str
    canonical_key: str
    winner_id: int
    loser_ids: list[int]
    row_ids: list[int]
    action: str


def _load_groups(db: Session) -> tuple[dict[int, int], list[list[CompetitorPriceList]]]:
    rows = (
        db.execute(
            select(CompetitorPriceList)
            .where(CompetitorPriceList.source_type == "provisor")
            .where(CompetitorPriceList.external_price_list_id != "")
            .order_by(CompetitorPriceList.external_price_list_id.asc(), CompetitorPriceList.id.asc())
        )
        .scalars()
        .all()
    )
    row_ids = [int(row.id) for row in rows]
    item_counts = (
        dict(
            db.execute(
                select(CompetitorPriceListItem.price_list_id, func.count(CompetitorPriceListItem.id))
                .where(CompetitorPriceListItem.price_list_id.in_(row_ids))
                .group_by(CompetitorPriceListItem.price_list_id)
            ).all()
        )
        if row_ids
        else {}
    )
    groups_by_external: dict[str, list[CompetitorPriceList]] = {}
    for row in rows:
        external_id = str(row.external_price_list_id or "").strip()
        if external_id:
            groups_by_external.setdefault(external_id, []).append(row)
    return {int(key): int(value) for key, value in item_counts.items()}, list(groups_by_external.values())


def _plan_group(group: list[CompetitorPriceList], item_counts: dict[int, int]) -> _GroupPlan | None:
    external_id = str(group[0].external_price_list_id or "").strip()
    canonical_key = canonical_provisor_source_key(external_id)
    needs_key_update = any(str(row.source_key or "") != canonical_key for row in group)
    if len(group) == 1 and not needs_key_update:
        return None
    winner = max(group, key=lambda row: _row_rank(row, int(item_counts.get(int(row.id), 0))))
    losers = [row for row in group if int(row.id) != int(winner.id)]
    return _GroupPlan(
        external_price_list_id=external_id,
        canonical_key=canonical_key,
        winner_id=int(winner.id),
        loser_ids=[int(row.id) for row in losers],
        row_ids=[int(row.id) for row in group],
        action="merge" if losers else "normalize_key",
    )


def _merge_assignment(
    *,
    db: Session,
    assignment: PriceFormatCompetitorAssignment,
    winner_id: int,
) -> str:
    existing = (
        db.execute(
            select(PriceFormatCompetitorAssignment)
            .where(PriceFormatCompetitorAssignment.price_format_id == assignment.price_format_id)
            .where(PriceFormatCompetitorAssignment.competitor_price_list_id == winner_id)
        )
        .scalars()
        .first()
    )
    if existing is None:
        assignment.competitor_price_list_id = winner_id
        assignment.updated_at = datetime.utcnow()
        return "repointed"
    existing.is_active = bool(existing.is_active or assignment.is_active)
    if assignment.is_active:
        existing.coefficient = assignment.coefficient
    if not str(existing.percentile_mode or "").strip() and str(assignment.percentile_mode or "").strip():
        existing.percentile_mode = assignment.percentile_mode
    if not str(existing.source_mode or "").strip() and str(assignment.source_mode or "").strip():
        existing.source_mode = assignment.source_mode
    existing.updated_at = datetime.utcnow()
    db.delete(assignment)
    return "merged"


def normalize_provisor_plk_rows(*, db: Session, apply: bool = False) -> dict[str, Any]:
    item_counts, groups = _load_groups(db)
    plans = [plan for group in groups if (plan := _plan_group(group, item_counts)) is not None]
    report: dict[str, Any] = {
        "apply": bool(apply),
        "groups_scanned": len(groups),
        "groups_changed": len(plans),
        "rows_merged": sum(len(plan.loser_ids) for plan in plans),
        "rows_normalized": sum(1 for plan in plans if plan.action == "normalize_key"),
        "assignments_repointed": 0,
        "assignments_merged": 0,
        "items_deleted_from_noncanonical_snapshots": 0,
        "legacy_prices_repointed": 0,
        "groups": [
            {
                "external_price_list_id": plan.external_price_list_id,
                "canonical_key": plan.canonical_key,
                "winner_id": plan.winner_id,
                "loser_ids": plan.loser_ids,
                "row_ids": plan.row_ids,
                "action": plan.action,
            }
            for plan in plans
        ],
    }
    if not apply:
        return report

    for plan in plans:
        rows = {
            int(row.id): row
            for row in db.execute(select(CompetitorPriceList).where(CompetitorPriceList.id.in_(plan.row_ids))).scalars().all()
        }
        winner = rows[plan.winner_id]
        aliases = _merge_aliases(list(rows.values()))
        old_source_names = [f"provisor:{rows[row_id].source_key}" for row_id in plan.loser_ids if row_id in rows]

        if str(winner.source_key or "") != plan.canonical_key:
            canonical_conflict = next(
                (
                    row
                    for row in rows.values()
                    if int(row.id) != int(winner.id) and str(row.source_key or "") == plan.canonical_key
                ),
                None,
            )
            if canonical_conflict is not None:
                canonical_conflict.source_key = f"merged:{canonical_conflict.source_key}:{canonical_conflict.id}"
            winner.source_key = plan.canonical_key

        _apply_aliases(winner, aliases)
        winner.updated_at = datetime.utcnow()

        assignments = (
            db.execute(
                select(PriceFormatCompetitorAssignment)
                .where(PriceFormatCompetitorAssignment.competitor_price_list_id.in_(plan.loser_ids))
            )
            .scalars()
            .all()
        )
        for assignment in assignments:
            action = _merge_assignment(db=db, assignment=assignment, winner_id=plan.winner_id)
            if action == "repointed":
                report["assignments_repointed"] += 1
            else:
                report["assignments_merged"] += 1

        if old_source_names:
            result = (
                db.query(CompetitorPrice)
                .filter(CompetitorPrice.source_name.in_(old_source_names))
                .update({CompetitorPrice.source_name: f"provisor:{plan.canonical_key}"}, synchronize_session=False)
            )
            report["legacy_prices_repointed"] += int(result or 0)

        if plan.loser_ids:
            deleted_items = (
                db.query(CompetitorPriceListItem)
                .filter(CompetitorPriceListItem.price_list_id.in_(plan.loser_ids))
                .delete(synchronize_session=False)
            )
            report["items_deleted_from_noncanonical_snapshots"] += int(deleted_items or 0)
            for loser_id in plan.loser_ids:
                loser = rows.get(loser_id)
                if loser is not None:
                    db.delete(loser)

    db.commit()
    return report
