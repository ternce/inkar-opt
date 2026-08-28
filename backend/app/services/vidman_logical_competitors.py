from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    PriceFormat,
    VidmanLogicalCompetitor,
    VidmanLogicalCompetitorSource,
)
from .regions import canonical_supported_city_name


PRIMARY = "PRIMARY"
FALLBACK = "FALLBACK"
VALID_ROLES = frozenset({PRIMARY, FALLBACK})
NO_COLLISION = "NO_COLLISION"
SAME_PHYSICAL_COMPETITOR = "SAME_PHYSICAL_COMPETITOR"
POSSIBLE_COLLISION = "POSSIBLE_COLLISION"
UNRESOLVED = "UNRESOLVED"
DIFFERENT_COMPETITOR = "DIFFERENT_COMPETITOR"


@dataclass(frozen=True)
class LogicalAssignmentResult:
    logical_competitor_id: int | None
    source_id: int | None
    dry_run: bool
    created_logical: bool
    created_source: bool
    action: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_competitor_id": self.logical_competitor_id,
            "source_id": self.source_id,
            "dry_run": self.dry_run,
            "created_logical": self.created_logical,
            "created_source": self.created_source,
            "action": self.action,
            "warnings": list(self.warnings),
        }


def _role(value: object) -> str:
    out = str(value or "").strip().upper()
    if out not in VALID_ROLES:
        raise ValueError(f"role must be one of {sorted(VALID_ROLES)}")
    return out


def _price_format_id(db: Session, *, price_format_id: int | None = None, price_format_code: str = "") -> int | None:
    if price_format_id is not None:
        return int(price_format_id)
    code = str(price_format_code or "").strip()
    if not code:
        return None
    found = db.scalar(select(PriceFormat.id).where(PriceFormat.code == code))
    if found is None:
        raise ValueError(f"price format {code!r} was not found")
    return int(found)


def ensure_logical_competitor(
    *,
    db: Session,
    name: str,
    region: str,
    price_format_id: int | None,
    active: bool = True,
    collision_status: str = UNRESOLVED,
    collision_notes: str = "",
) -> tuple[VidmanLogicalCompetitor, bool]:
    logical = db.execute(
        select(VidmanLogicalCompetitor)
        .where(VidmanLogicalCompetitor.name == name)
        .where(VidmanLogicalCompetitor.region == region)
        .where(VidmanLogicalCompetitor.price_format_id.is_(None) if price_format_id is None else VidmanLogicalCompetitor.price_format_id == price_format_id)
    ).scalar_one_or_none()
    created = logical is None
    if logical is None:
        logical = VidmanLogicalCompetitor(name=name, region=region, price_format_id=price_format_id)
        db.add(logical)
        db.flush()
    logical.active = bool(active)
    logical.collision_status = str(collision_status or UNRESOLVED)
    logical.collision_notes = str(collision_notes or "")
    logical.updated_at = datetime.utcnow()
    return logical, created


def assign_source_to_logical_competitor(
    *,
    db: Session,
    logical_name: str,
    account_id: int,
    main_id: int,
    role: str,
    region: str = "",
    price_format_id: int | None = None,
    price_format_code: str = "",
    priority: int | None = None,
    active: bool = True,
    approved_manually: bool = True,
    collision_status: str = UNRESOLVED,
    collision_notes: str = "",
    apply: bool = False,
) -> LogicalAssignmentResult:
    normalized_role = _role(role)
    resolved_format_id = _price_format_id(db, price_format_id=price_format_id, price_format_code=price_format_code)
    logical_name = str(logical_name or "").strip()
    region = str(region or "").strip()
    if not logical_name:
        raise ValueError("logical_name is required")
    if not region:
        raise ValueError("region is required")
    region = canonical_supported_city_name(region)
    if not region:
        raise ValueError("region must be one of supported regions")
    if resolved_format_id is None:
        raise ValueError("price_format_id or price_format_code is required")
    priority_value = int(priority if priority is not None else (0 if normalized_role == PRIMARY else 100))

    existing_source = db.execute(
        select(VidmanLogicalCompetitorSource)
        .where(VidmanLogicalCompetitorSource.account_id == account_id)
        .where(VidmanLogicalCompetitorSource.main_id == main_id)
    ).scalar_one_or_none()

    existing_logical = None
    if existing_source is not None:
        existing_logical = db.get(VidmanLogicalCompetitor, existing_source.logical_competitor_id)
        if existing_logical is not None and (
            existing_logical.name != logical_name
            or existing_logical.region != region
            or int(existing_logical.price_format_id or 0) != int(resolved_format_id or 0)
        ):
            raise ValueError("source already belongs to another logical competitor")

    logical, created_logical = ensure_logical_competitor(
        db=db,
        name=logical_name,
        region=region,
        price_format_id=resolved_format_id,
        active=True,
        collision_status=collision_status,
        collision_notes=collision_notes,
    )

    if normalized_role == PRIMARY and active:
        primary = db.execute(
            select(VidmanLogicalCompetitorSource)
            .where(VidmanLogicalCompetitorSource.logical_competitor_id == logical.id)
            .where(VidmanLogicalCompetitorSource.role == PRIMARY)
            .where(VidmanLogicalCompetitorSource.active.is_(True))
            .where(
                (VidmanLogicalCompetitorSource.account_id != account_id)
                | (VidmanLogicalCompetitorSource.main_id != main_id)
            )
        ).scalar_one_or_none()
        if primary is not None:
            raise ValueError("logical competitor already has an active PRIMARY source")

    source = existing_source
    created_source = source is None
    if source is None:
        source = VidmanLogicalCompetitorSource(
            logical_competitor_id=int(logical.id),
            account_id=int(account_id),
            main_id=int(main_id),
        )
    source.logical_competitor_id = int(logical.id)
    source.role = normalized_role
    source.priority = priority_value
    source.active = bool(active)
    source.approved_manually = bool(approved_manually)
    source.approved_at = datetime.utcnow() if approved_manually and source.approved_at is None else source.approved_at
    source.updated_at = datetime.utcnow()

    if not apply:
        db.rollback()
        return LogicalAssignmentResult(
            logical_competitor_id=None,
            source_id=None,
            dry_run=True,
            created_logical=created_logical,
            created_source=created_source,
            action="would_assign",
        )

    if created_source:
        db.add(source)
    db.flush()
    return LogicalAssignmentResult(
        logical_competitor_id=int(logical.id),
        source_id=int(source.id),
        dry_run=False,
        created_logical=created_logical,
        created_source=created_source,
        action="assigned",
    )


def list_logical_competitors(*, db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        select(VidmanLogicalCompetitor, VidmanLogicalCompetitorSource, PriceFormat)
        .outerjoin(VidmanLogicalCompetitorSource, VidmanLogicalCompetitorSource.logical_competitor_id == VidmanLogicalCompetitor.id)
        .outerjoin(PriceFormat, PriceFormat.id == VidmanLogicalCompetitor.price_format_id)
        .order_by(VidmanLogicalCompetitor.id.asc(), VidmanLogicalCompetitorSource.priority.asc())
    ).all()
    out: dict[int, dict[str, Any]] = {}
    for logical, source, pf in rows:
        item = out.setdefault(
            int(logical.id),
            {
                "id": int(logical.id),
                "name": logical.name,
                "region": logical.region,
                "price_format_id": logical.price_format_id,
                "price_format_code": pf.code if pf is not None else "",
                "active": logical.active,
                "collision_status": logical.collision_status,
                "collision_notes": logical.collision_notes,
                "sources": [],
            },
        )
        if source is not None:
            item["sources"].append(
                {
                    "id": int(source.id),
                    "account_id": int(source.account_id),
                    "main_id": int(source.main_id),
                    "role": source.role,
                    "priority": int(source.priority),
                    "approved_manually": bool(source.approved_manually),
                    "active": bool(source.active),
                }
            )
    return list(out.values())


def selected_logical_sources(
    *,
    sources: list[dict[str, Any]],
    snapshot_ready: dict[tuple[int, int], bool],
    allow_fallback: bool = True,
) -> dict[int, dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for source in sources:
        if not source.get("active"):
            continue
        grouped.setdefault(int(source["logical_competitor_id"]), []).append(source)
    selected: dict[int, dict[str, Any]] = {}
    for logical_id, candidates in grouped.items():
        primaries = sorted(
            [row for row in candidates if row.get("role") == PRIMARY],
            key=lambda row: int(row.get("priority") or 0),
        )
        fallbacks = sorted(
            [row for row in candidates if row.get("role") == FALLBACK],
            key=lambda row: int(row.get("priority") or 0),
        )
        primary = next((row for row in primaries if snapshot_ready.get((int(row["account_id"]), int(row["main_id"])), False)), None)
        if primary is not None:
            selected[logical_id] = primary
            continue
        if allow_fallback:
            fallback = next((row for row in fallbacks if snapshot_ready.get((int(row["account_id"]), int(row["main_id"])), False)), None)
            if fallback is not None:
                selected[logical_id] = fallback
    return selected


def primary_count_for_logical(*, db: Session, logical_competitor_id: int) -> int:
    return int(
        db.scalar(
            select(func.count(VidmanLogicalCompetitorSource.id))
            .where(VidmanLogicalCompetitorSource.logical_competitor_id == logical_competitor_id)
            .where(VidmanLogicalCompetitorSource.role == PRIMARY)
            .where(VidmanLogicalCompetitorSource.active.is_(True))
        )
        or 0
    )
