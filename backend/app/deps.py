from __future__ import annotations

from collections.abc import Generator
import os

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import AppUser, UserBranchAssignment
from .services.references.types import BRANCHES


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


ROLE_ADMIN = "admin"
ROLE_PRICING_MANAGER = "pricing_manager"
ROLE_PRICING_LEAD = "pricing_lead"
ROLE_VIEWER = "viewer"
WRITE_ROLES = {ROLE_ADMIN, ROLE_PRICING_MANAGER, ROLE_PRICING_LEAD}
ALL_BRANCH_ROLES = {ROLE_ADMIN}


def _branch_name(branch_id: str) -> str:
    return next((str(row["name"]) for row in BRANCHES if str(row["id"]) == str(branch_id)), str(branch_id or ""))


def _ensure_dev_user(db: Session, username: str) -> AppUser:
    username = (username or "dev-admin").strip() or "dev-admin"
    row = db.execute(select(AppUser).where(AppUser.username == username)).scalars().first()
    if row is not None:
        return row
    role = os.getenv("DEV_CURRENT_ROLE", ROLE_ADMIN if username == "dev-admin" else ROLE_VIEWER).strip() or ROLE_ADMIN
    row = AppUser(username=username, display_name=username, role=role, is_active=True)
    db.add(row)
    db.flush()
    raw_branches = os.getenv("DEV_CURRENT_BRANCH_IDS", "").strip()
    for branch_id in [part.strip() for part in raw_branches.split(",") if part.strip()]:
        db.add(UserBranchAssignment(user_id=row.id, branch_id=branch_id, branch_name=_branch_name(branch_id)))
    db.commit()
    db.refresh(row)
    return row


def get_current_user(
    db: Session = Depends(get_db),
    x_dev_user: str | None = Header(None, alias="X-Dev-User"),
) -> AppUser:
    username = x_dev_user or os.getenv("DEV_CURRENT_USER") or "dev-admin"
    user = _ensure_dev_user(db, username)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="user is inactive")
    return user


def current_user_to_dict(user: AppUser) -> dict:
    branches = [{"branchId": row.branch_id, "branchName": row.branch_name} for row in user.branches]
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name or user.username,
        "role": user.role,
        "isReadOnly": user.role == ROLE_VIEWER,
        "branches": branches,
        "canSeeAllBranches": can_see_all_branches(user),
        "canWrite": can_write(user),
    }


def can_write(user: AppUser) -> bool:
    return user.role in WRITE_ROLES


def can_see_all_branches(user: AppUser) -> bool:
    if user.role in ALL_BRANCH_ROLES:
        return True
    if user.role == ROLE_PRICING_LEAD and not user.branches:
        return True
    return False


def assigned_branch_ids(user: AppUser) -> set[str]:
    return {str(row.branch_id or "").strip() for row in user.branches if str(row.branch_id or "").strip()}


def assigned_branch_names(user: AppUser) -> set[str]:
    return {str(row.branch_name or "").strip().casefold() for row in user.branches if str(row.branch_name or "").strip()}


def user_can_access_branch(user: AppUser, branch_id: str = "", branch_name: str = "") -> bool:
    if can_see_all_branches(user):
        return True
    ids = assigned_branch_ids(user)
    names = assigned_branch_names(user)
    if not ids and not names:
        return False
    branch_id = str(branch_id or "").strip()
    branch_name = str(branch_name or "").strip().casefold()
    return bool((branch_id and branch_id in ids) or (branch_name and branch_name in names))


def require_write_access(current_user: AppUser = Depends(get_current_user)) -> AppUser:
    if not can_write(current_user):
        raise HTTPException(status_code=403, detail="viewer is read-only")
    return current_user
