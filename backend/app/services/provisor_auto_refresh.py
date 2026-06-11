from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    CompetitorPriceList,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    PriceSourceAccount,
    RefreshJob,
    RefreshLock,
)

logger = logging.getLogger(__name__)

SOURCE_TYPE = "provisor"
STALE_AFTER = timedelta(minutes=10)
REFRESH_LOCK_NAME = "provisor_auto_refresh"
SCHEDULER_LOCK_NAME = "provisor_auto_refresh_scheduler"
REFRESH_LOCK_LEASE = timedelta(hours=12)
SCHEDULER_LOCK_LEASE = timedelta(seconds=90)


def _json_loads(value: str | None, fallback: Any) -> Any:
    try:
        data = json.loads(value or "")
        return data if data is not None else fallback
    except Exception:
        return fallback


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def new_owner_token() -> str:
    return uuid.uuid4().hex


def _metadata_with_token(metadata: dict[str, Any] | None, token: str) -> str:
    data = dict(metadata or {})
    data["owner_token"] = token
    return _json_dumps(data)


def normalize_mode(mode: str | None) -> str:
    value = str(mode or "selected").strip().lower()
    if value not in {"selected", "all"}:
        raise ValueError("PROVISOR_AUTO_REFRESH_MODE must be selected or all")
    return value


def _try_insert_lock(
    db: Session,
    *,
    name: str,
    lock_type: str,
    owner_token: str,
    lease: timedelta,
    metadata: dict[str, Any] | None = None,
) -> bool:
    now = datetime.utcnow()
    row = RefreshLock(
        name=name,
        lock_type=lock_type,
        owner_token=owner_token,
        acquired_at=now,
        heartbeat_at=now,
        lease_until=now + lease,
        metadata_json=_json_dumps(metadata or {}),
    )
    db.add(row)
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def try_acquire_lock(
    db: Session,
    *,
    name: str,
    lock_type: str,
    owner_token: str,
    lease: timedelta,
    metadata: dict[str, Any] | None = None,
) -> bool:
    if _try_insert_lock(
        db,
        name=name,
        lock_type=lock_type,
        owner_token=owner_token,
        lease=lease,
        metadata=metadata,
    ):
        return True
    now = datetime.utcnow()
    result = db.execute(
        update(RefreshLock)
        .where(RefreshLock.name == name)
        .where(RefreshLock.lease_until < now)
        .values(
            owner_token=owner_token,
            lock_type=lock_type,
            acquired_at=now,
            heartbeat_at=now,
            lease_until=now + lease,
            metadata_json=_json_dumps(metadata or {}),
        )
    )
    db.commit()
    return int(result.rowcount or 0) == 1


def renew_lock(db: Session, *, name: str, owner_token: str, lease: timedelta) -> bool:
    now = datetime.utcnow()
    result = db.execute(
        update(RefreshLock)
        .where(RefreshLock.name == name)
        .where(RefreshLock.owner_token == owner_token)
        .values(heartbeat_at=now, lease_until=now + lease)
    )
    db.commit()
    return int(result.rowcount or 0) == 1


def release_lock(db: Session, *, name: str, owner_token: str) -> bool:
    row = db.get(RefreshLock, name)
    if row is None or row.owner_token != owner_token:
        return False
    db.delete(row)
    db.commit()
    return True


def try_acquire_refresh_lock(db: Session, *, owner_token: str, requested_by: str) -> bool:
    return try_acquire_lock(
        db,
        name=REFRESH_LOCK_NAME,
        lock_type="refresh",
        owner_token=owner_token,
        lease=REFRESH_LOCK_LEASE,
        metadata={"requested_by": requested_by},
    )


def try_acquire_scheduler_lock(db: Session, *, owner_token: str) -> bool:
    return try_acquire_lock(
        db,
        name=SCHEDULER_LOCK_NAME,
        lock_type="scheduler",
        owner_token=owner_token,
        lease=SCHEDULER_LOCK_LEASE,
    )


def renew_scheduler_lock(db: Session, *, owner_token: str) -> bool:
    return renew_lock(db, name=SCHEDULER_LOCK_NAME, owner_token=owner_token, lease=SCHEDULER_LOCK_LEASE)


def release_scheduler_lock(db: Session, *, owner_token: str) -> bool:
    return release_lock(db, name=SCHEDULER_LOCK_NAME, owner_token=owner_token)


def release_refresh_lock(db: Session, *, owner_token: str) -> bool:
    return release_lock(db, name=REFRESH_LOCK_NAME, owner_token=owner_token)


def renew_refresh_lock(db: Session, *, owner_token: str) -> bool:
    return renew_lock(db, name=REFRESH_LOCK_NAME, owner_token=owner_token, lease=REFRESH_LOCK_LEASE)


def refresh_job_to_status(job: RefreshJob | None) -> dict[str, Any]:
    if job is None:
        return {
            "status": "idle",
            "source_type": SOURCE_TYPE,
            "mode": "selected",
            "started_at": None,
            "finished_at": None,
            "heartbeat_at": None,
            "processed_accounts": 0,
            "total_accounts": 0,
            "processed_plk": 0,
            "total_plk": 0,
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "message": "",
            "last_error": None,
        }
    status = str(job.status or "idle")
    if status == "skipped":
        status = "idle"
    return {
        "status": status,
        "source_type": job.source_type,
        "mode": job.mode,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "heartbeat_at": job.heartbeat_at.isoformat() if job.heartbeat_at else None,
        "processed_accounts": int(job.processed_accounts or 0),
        "total_accounts": int(job.total_accounts or 0),
        "processed_plk": int(job.processed_plk or 0),
        "total_plk": int(job.total_plk or 0),
        "success_count": int(job.success_count or 0),
        "failed_count": int(job.failed_count or 0),
        "skipped_count": int(job.skipped_count or 0),
        "message": job.message or "",
        "last_error": job.error_message or None,
    }


def mark_stale_running_jobs(db: Session, *, now: datetime | None = None) -> list[RefreshJob]:
    now = now or datetime.utcnow()
    stale_before = now - STALE_AFTER
    rows = (
        db.execute(
            select(RefreshJob)
            .where(RefreshJob.source_type == SOURCE_TYPE)
            .where(RefreshJob.status == "running")
            .where(RefreshJob.heartbeat_at < stale_before)
            .order_by(RefreshJob.started_at.desc())
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = "stale"
        row.finished_at = now
        row.message = "Provisor refresh heartbeat is stale; manual recovery is required."
        row.error_message = f"No heartbeat since {row.heartbeat_at.isoformat() if row.heartbeat_at else 'unknown'}"
    if rows:
        db.commit()
        for row in rows:
            logger.warning("Marked Provisor refresh stale: job_id=%s heartbeat_at=%s", row.id, row.heartbeat_at)
    return rows


def latest_refresh_job(db: Session) -> RefreshJob | None:
    mark_stale_running_jobs(db)
    active = (
        db.execute(
            select(RefreshJob)
            .where(RefreshJob.source_type == SOURCE_TYPE)
            .where(RefreshJob.status.in_(("pending", "running", "stale")))
            .order_by(RefreshJob.started_at.desc().nullslast(), RefreshJob.id.desc())
        )
        .scalars()
        .first()
    )
    if active is not None:
        return active
    return (
        db.execute(
            select(RefreshJob)
            .where(RefreshJob.source_type == SOURCE_TYPE)
            .order_by(RefreshJob.started_at.desc().nullslast(), RefreshJob.id.desc())
        )
        .scalars()
        .first()
    )


def active_or_stale_refresh_job(db: Session) -> RefreshJob | None:
    mark_stale_running_jobs(db)
    return (
        db.execute(
            select(RefreshJob)
            .where(RefreshJob.source_type == SOURCE_TYPE)
            .where(RefreshJob.status.in_(("pending", "running", "stale")))
            .order_by(RefreshJob.started_at.desc().nullslast(), RefreshJob.id.desc())
        )
        .scalars()
        .first()
    )


def create_skipped_job(db: Session, *, mode: str, requested_by: str, message: str) -> RefreshJob:
    now = datetime.utcnow()
    job = RefreshJob(
        source_type=SOURCE_TYPE,
        mode=normalize_mode(mode),
        status="skipped",
        started_at=now,
        finished_at=now,
        heartbeat_at=now,
        requested_by=requested_by,
        message=message,
        skipped_count=1,
        metadata_json=_json_dumps({"reason": "overlap_guard"}),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def try_create_refresh_job(db: Session, *, mode: str, requested_by: str) -> tuple[RefreshJob | None, RefreshJob | None, str | None]:
    blocker = active_or_stale_refresh_job(db)
    if blocker is not None:
        return None, blocker, None
    owner_token = new_owner_token()
    if not try_acquire_refresh_lock(db, owner_token=owner_token, requested_by=requested_by):
        return None, latest_refresh_job(db), None
    now = datetime.utcnow()
    job = RefreshJob(
        source_type=SOURCE_TYPE,
        mode=normalize_mode(mode),
        status="pending",
        started_at=now,
        heartbeat_at=now,
        requested_by=requested_by,
        message="Provisor refresh queued.",
        metadata_json=_metadata_with_token({}, owner_token),
    )
    db.add(job)
    try:
        db.commit()
        db.refresh(job)
        return job, None, owner_token
    except Exception:
        db.rollback()
        release_refresh_lock(db, owner_token=owner_token)
        raise


def job_owner_token(job: RefreshJob) -> str:
    metadata = _json_loads(job.metadata_json, {})
    return str(metadata.get("owner_token") or "") if isinstance(metadata, dict) else ""


def heartbeat(db: Session, job: RefreshJob, *, message: str | None = None, owner_token: str | None = None) -> bool:
    token = owner_token or job_owner_token(job)
    if not token:
        return False
    if not renew_refresh_lock(db, owner_token=token):
        return False
    db.expire(job)
    if job.status != "running" or job_owner_token(job) != token:
        return False
    job.heartbeat_at = datetime.utcnow()
    if message is not None:
        job.message = message[:512]
    db.commit()
    return True


def start_job(db: Session, job: RefreshJob, *, total_accounts: int, total_plk: int, metadata: dict[str, Any], owner_token: str | None = None) -> bool:
    token = owner_token or job_owner_token(job)
    if not token or job.status != "pending" or job_owner_token(job) != token:
        return False
    now = datetime.utcnow()
    job.status = "running"
    job.started_at = job.started_at or now
    job.heartbeat_at = now
    job.total_accounts = total_accounts
    job.total_plk = total_plk
    job.message = "Refreshing Provisor PLK..."
    job.metadata_json = _metadata_with_token(metadata, token)
    db.commit()
    return True


def update_progress_from_result(db: Session, job: RefreshJob, result: dict[str, Any], *, owner_token: str | None = None) -> bool:
    token = owner_token or job_owner_token(job)
    db.expire(job)
    if not token or job.status != "running" or job_owner_token(job) != token:
        return False
    progress = result.get("progress") if isinstance(result, dict) else {}
    accounts = result.get("accounts") if isinstance(result, dict) else []
    processed_accounts = len(accounts) if isinstance(accounts, list) else int(job.processed_accounts or 0)
    job.processed_accounts = min(int(job.total_accounts or processed_accounts), processed_accounts)
    job.processed_plk = int((progress or {}).get("processed") or job.processed_plk or 0)
    job.success_count = int((progress or {}).get("success") or 0)
    job.failed_count = int((progress or {}).get("errors") or 0)
    job.skipped_count = int((progress or {}).get("skipped") or 0)
    job.heartbeat_at = datetime.utcnow()
    db.commit()
    return True


def finish_job(
    db: Session,
    job: RefreshJob,
    *,
    status: str,
    message: str,
    error: str = "",
    metadata: dict[str, Any] | None = None,
    owner_token: str | None = None,
    require_running: bool = False,
    allowed_statuses: set[str] | None = None,
    release_refresh: bool = False,
) -> bool:
    token = owner_token or job_owner_token(job)
    db.expire(job)
    if token and job_owner_token(job) != token:
        return False
    if require_running and job.status != "running":
        return False
    if allowed_statuses is not None and job.status not in allowed_statuses:
        return False
    now = datetime.utcnow()
    job.status = status
    job.finished_at = now
    job.heartbeat_at = now
    job.message = message[:512]
    job.error_message = error[:2048]
    if metadata is not None:
        existing = _json_loads(job.metadata_json, {})
        if isinstance(existing, dict):
            existing.update(metadata)
            if token:
                existing["owner_token"] = token
            job.metadata_json = _json_dumps(existing)
        else:
            job.metadata_json = _metadata_with_token(metadata, token) if token else _json_dumps(metadata)
    db.commit()
    if release_refresh and token:
        release_refresh_lock(db, owner_token=token)
    return True


def selected_refresh_targets(db: Session) -> dict[str, dict[str, set[str]]]:
    rows = (
        db.execute(
            select(PriceFormat.code, CompetitorPriceList.account_id, CompetitorPriceList.external_price_list_id)
            .join(
                PriceFormatCompetitorAssignment,
                PriceFormatCompetitorAssignment.competitor_price_list_id == CompetitorPriceList.id,
            )
            .join(PriceFormat, PriceFormat.id == PriceFormatCompetitorAssignment.price_format_id)
            .where(PriceFormatCompetitorAssignment.is_active.is_(True))
            .where(CompetitorPriceList.source_type == SOURCE_TYPE)
            .where(CompetitorPriceList.account_id != "")
            .where(CompetitorPriceList.external_price_list_id != "")
            .order_by(PriceFormat.code.asc(), CompetitorPriceList.account_id.asc())
        )
        .all()
    )
    targets: dict[str, dict[str, set[str]]] = {}
    seen_plk: set[tuple[str, str]] = set()
    for format_code, account_id, filial_id in rows:
        account_s = str(account_id or "").strip()
        filial_s = str(filial_id or "").strip()
        if not account_s or not filial_s or (account_s, filial_s) in seen_plk:
            continue
        seen_plk.add((account_s, filial_s))
        targets.setdefault(str(format_code), {}).setdefault(account_s, set()).add(filial_s)
    return targets


def all_refresh_targets(db: Session) -> dict[str, dict[str, set[str]]]:
    format_code = db.execute(select(PriceFormat.code).order_by(PriceFormat.id.asc())).scalars().first()
    if not format_code:
        return {}
    account_ids = (
        db.execute(
            select(PriceSourceAccount.id)
            .where(PriceSourceAccount.source_type == SOURCE_TYPE)
            .where(PriceSourceAccount.is_active.is_(True))
            .order_by(PriceSourceAccount.id.asc())
        )
        .scalars()
        .all()
    )
    return {str(format_code): {str(account_id): set() for account_id in account_ids}}


def target_counts(targets: dict[str, dict[str, set[str]]], db: Session, *, mode: str) -> tuple[int, int]:
    account_ids = {account_id for by_account in targets.values() for account_id in by_account}
    if normalize_mode(mode) == "all":
        if not account_ids:
            return 0, 0
        total_plk = int(
            db.execute(
                select(func.count(CompetitorPriceList.id))
                .where(CompetitorPriceList.source_type == SOURCE_TYPE)
                .where(CompetitorPriceList.account_id.in_(account_ids))
            ).scalar_one()
            or 0
        )
    else:
        total_plk = sum(len(filial_ids) for by_account in targets.values() for filial_ids in by_account.values())
    return len(account_ids), total_plk
