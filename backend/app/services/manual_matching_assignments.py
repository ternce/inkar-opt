from __future__ import annotations

import os

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AppUser, ManualMatchingAssignment, Product
from ..timezone import now_kz_naive
from .competitors.code_mappings import global_unmapped_product_condition


def configured_worker_ids() -> tuple[int, int] | None:
    value = os.getenv("MANUAL_MATCHING_WORKER_IDS", "").strip()
    if not value:
        return None
    try:
        ids = tuple(int(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise ValueError("MANUAL_MATCHING_WORKER_IDS must contain two integer user IDs") from exc
    if len(ids) != 2 or ids[0] == ids[1] or min(ids) <= 0:
        raise ValueError("MANUAL_MATCHING_WORKER_IDS must contain two distinct positive user IDs")
    return ids


def global_unmapped_condition():
    return global_unmapped_product_condition()


def is_global_unmapped(db: Session, product_id: int) -> bool:
    return db.scalar(select(Product.id).where(Product.id == product_id, global_unmapped_condition())) is not None


def _validate_workers(db: Session, worker_ids: tuple[int, int]) -> tuple[int, int]:
    if len(worker_ids) != 2 or worker_ids[0] == worker_ids[1] or min(worker_ids) <= 0:
        raise ValueError("two distinct positive worker user IDs are required")
    ids = tuple(sorted(worker_ids))
    users = db.scalars(select(AppUser).where(AppUser.id.in_(ids)).order_by(AppUser.id).with_for_update()).all()
    if len(users) != 2 or any(not user.is_active or user.role not in {"pricing_manager", "pricing_lead"} for user in users):
        raise ValueError("both workers must be active pricing_manager or pricing_lead users")
    return ids


def reconcile_manual_matching_assignments(db: Session, worker_ids: tuple[int, int]) -> dict[str, int]:
    """Explicit, idempotent allocation. Call after a successful workflow or via admin operation."""
    ids = _validate_workers(db, worker_ids)
    # The same two AppUser row locks serialize allocators on PostgreSQL.
    eligible = set(db.scalars(select(Product.id).where(global_unmapped_condition())).all())
    existing = db.scalars(select(ManualMatchingAssignment).order_by(ManualMatchingAssignment.product_id).with_for_update()).all()
    now = now_kz_naive()
    reopened = completed = created = 0
    counts = {worker_id: 0 for worker_id in ids}
    assigned_product_ids: set[int] = set()
    for row in existing:
        assigned_product_ids.add(row.product_id)
        should_be_active = row.product_id in eligible
        if should_be_active and row.status != "active":
            row.status = "active"
            row.completed_at = None
            row.completed_by_user_id = None
            row.updated_at = now
            reopened += 1
        elif not should_be_active and row.status != "completed":
            row.status = "completed"
            row.completed_at = now
            row.updated_at = now
            completed += 1
        if should_be_active and row.assigned_user_id in counts:
            counts[row.assigned_user_id] += 1
    for product_id in sorted(eligible - assigned_product_ids):
        owner_id = min(ids, key=lambda user_id: (counts[user_id], user_id))
        db.add(ManualMatchingAssignment(
            product_id=product_id, assigned_user_id=owner_id, assigned_at=now,
            status="active", created_at=now, updated_at=now,
        ))
        counts[owner_id] += 1
        created += 1
    db.commit()
    return {"created": created, "reopened": reopened, "completed": completed,
            "active_user_a": counts[ids[0]], "active_user_b": counts[ids[1]]}


def lock_manual_matching_task(
    db: Session, product_id: int, user: AppUser, *, require_unmapped: bool = True,
) -> ManualMatchingAssignment | None:
    row = db.scalar(select(ManualMatchingAssignment).where(
        ManualMatchingAssignment.product_id == product_id,
    ).with_for_update())
    db.scalar(select(Product.id).where(Product.id == product_id).with_for_update())
    if user.role != "admin":
        if row is None:
            raise HTTPException(status_code=409, detail="manual matching task is not assigned")
        if row.assigned_user_id != user.id:
            raise HTTPException(status_code=403, detail="manual matching task belongs to another user")
    if require_unmapped and (row is not None and row.status != "active" or not is_global_unmapped(db, product_id)):
        raise HTTPException(status_code=409, detail="manual matching task is no longer active")
    return row


def require_manual_task_read(db: Session, product_id: int, user: AppUser) -> None:
    if user.role == "admin":
        return
    row = db.scalar(select(ManualMatchingAssignment).where(
        ManualMatchingAssignment.product_id == product_id,
    ))
    if row is None or row.assigned_user_id != user.id or row.status != "active" or not is_global_unmapped(db, product_id):
        raise HTTPException(status_code=403, detail="manual matching task is not in your active queue")


def complete_manual_matching_task(row: ManualMatchingAssignment | None, user: AppUser) -> None:
    if row is None:
        return
    row.status = "completed"
    row.completed_at = now_kz_naive()
    row.completed_by_user_id = user.id
    row.updated_at = now_kz_naive()


def reopen_manual_matching_task(row: ManualMatchingAssignment | None) -> None:
    if row is None:
        return
    row.status = "active"
    row.completed_at = None
    row.completed_by_user_id = None
    row.updated_at = now_kz_naive()


def manual_matching_counts(db: Session, user: AppUser) -> dict:
    active_condition = global_unmapped_condition()
    active_assignments = select(ManualMatchingAssignment).join(Product).where(
        ManualMatchingAssignment.status == "active", active_condition,
    ).subquery()
    total_active = int(db.scalar(select(func.count(Product.id)).where(active_condition)) or 0)
    per_user = dict(db.execute(select(
        active_assignments.c.assigned_user_id, func.count(active_assignments.c.id),
    ).group_by(active_assignments.c.assigned_user_id)).all())
    total_completed = int(db.scalar(select(func.count(ManualMatchingAssignment.id)).where(
        ManualMatchingAssignment.status == "completed",
    )) or 0)
    my_completed = int(db.scalar(select(func.count(ManualMatchingAssignment.id)).where(
        ManualMatchingAssignment.status == "completed", ManualMatchingAssignment.assigned_user_id == user.id,
    )) or 0)
    return {
        "my_active": int(per_user.get(user.id, 0)), "my_completed": my_completed,
        "total_active": total_active, "total_completed": total_completed,
        "unassigned_active": total_active - sum(per_user.values()),
        "per_user_active": per_user if user.role == "admin" else {},
    }
