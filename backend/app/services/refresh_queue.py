from __future__ import annotations

import json
import logging
import os
import socket
import sys
import uuid
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import RefreshJob, RefreshLock
from .db_time import db_now
from .emit_worker import LOCK_NAME as EMIT_REFRESH_LOCK_NAME
from .emit_worker import cleanup_stale_emit_temp_files, EmitConfig
from .provisor_auto_refresh import GLOBAL_REFRESH_LOCK_NAME, REFRESH_LOCK_LEASE, REFRESH_LOCK_NAME as PROVISOR_REFRESH_LOCK_NAME

logger = logging.getLogger(__name__)

QUEUED_STATUS = "queued"
RUNNING_STATUS = "running"
PARSER_QUEUE_SOURCES = ("provisor", "emit", "vidman")
ACTIVE_QUEUE_STATUSES = ("queued", "pending", "running", "downloading", "parsing", "normalizing", "saving", "stale")
EXECUTING_STATUSES = ("pending", "running", "downloading", "parsing", "normalizing", "saving", "stale")
TERMINAL_STATUSES = ("success", "failed", "error", "interrupted", "cancelled", "skipped", "partial_success")
DEFAULT_STALE_HEARTBEAT_SECONDS = 180


def _json_loads(value: str | None, fallback: Any) -> Any:
    try:
        data = json.loads(value or "")
        return data if data is not None else fallback
    except Exception:
        return fallback


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def new_worker_owner_token() -> str:
    return uuid.uuid4().hex


def worker_identity(worker_id: str | None = None) -> dict[str, Any]:
    return {
        "worker_owner_id": worker_id or os.getenv("PARSER_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}",
        "worker_id": worker_id or os.getenv("PARSER_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}",
        "worker_host": socket.gethostname(),
        "worker_pid": os.getpid(),
        "container_instance_id": os.getenv("CONTAINER_INSTANCE_ID") or os.getenv("HOSTNAME") or "",
        "worker_executable": sys.executable,
    }


def _metadata_with_queue_fields(metadata: dict[str, Any] | None, *, queue_kind: str, requested_by: str) -> str:
    now = datetime.utcnow()
    data = dict(metadata or {})
    data.update(
        {
            "queue_kind": queue_kind,
            "queued_at": now.isoformat(),
            "requested_by": requested_by,
        }
    )
    return _json_dumps(data)


def create_queued_provisor_auto_refresh_job(db: Session, *, mode: str, requested_by: str) -> RefreshJob:
    now = db_now(db)
    job = RefreshJob(
        source_type="provisor",
        mode=mode,
        status=QUEUED_STATUS,
        started_at=now,
        heartbeat_at=now,
        requested_by=requested_by,
        message="Provisor refresh queued.",
        metadata_json=_metadata_with_queue_fields({}, queue_kind="provisor_auto", requested_by=requested_by),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def create_queued_price_format_refresh_job(
    db: Session,
    *,
    format_code: str,
    refresh_source: str,
    payload: dict[str, Any],
    requested_by: str,
) -> RefreshJob:
    now = db_now(db)
    source_type = "provisor" if refresh_source in {"provisor", "all"} else refresh_source
    metadata = {
        "queue_kind": "price_format_refresh",
        "format_code": format_code,
        "refresh_source": refresh_source,
        "payload": payload or {},
    }
    job = RefreshJob(
        source_type=source_type,
        mode=refresh_source,
        status=QUEUED_STATUS,
        started_at=now,
        heartbeat_at=now,
        requested_by=requested_by,
        message="Competitor price-list refresh queued.",
        metadata_json=_metadata_with_queue_fields(metadata, queue_kind="price_format_refresh", requested_by=requested_by),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def create_queued_emit_refresh_job(
    db: Session,
    *,
    mode: str,
    filial_ids: list[int],
    requested_by: str,
    price_format_code: str | None = None,
) -> RefreshJob:
    now = db_now(db)
    job = RefreshJob(
        source_type="emit",
        mode=mode,
        status=QUEUED_STATUS,
        started_at=now,
        heartbeat_at=now,
        requested_by=requested_by,
        total_plk=len(filial_ids),
        message="Emit refresh queued.",
        metadata_json=_metadata_with_queue_fields(
            {
                "filial_ids": list(dict.fromkeys(int(x) for x in filial_ids if int(x) > 0)),
                "price_format_code": price_format_code or "",
            },
            queue_kind="emit_refresh",
            requested_by=requested_by,
        ),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def active_parser_refresh_job(db: Session, *, sources: Iterable[str] = PARSER_QUEUE_SOURCES) -> RefreshJob | None:
    return (
        db.execute(
            select(RefreshJob)
            .where(RefreshJob.source_type.in_(tuple(sources)))
            .where(RefreshJob.status.in_(ACTIVE_QUEUE_STATUSES))
            .order_by(RefreshJob.started_at.desc().nullslast(), RefreshJob.id.desc())
        )
        .scalars()
        .first()
    )


def active_price_format_refresh_job(db: Session, *, format_code: str, refresh_source: str) -> RefreshJob | None:
    source_type = "provisor" if refresh_source in {"provisor", "all"} else refresh_source
    rows = (
        db.execute(
            select(RefreshJob)
            .where(RefreshJob.source_type == source_type)
            .where(RefreshJob.status.in_(ACTIVE_QUEUE_STATUSES))
            .order_by(RefreshJob.id.desc())
        )
        .scalars()
        .all()
    )
    for row in rows:
        metadata = _json_loads(row.metadata_json, {})
        if not isinstance(metadata, dict) or metadata.get("queue_kind") != "price_format_refresh":
            continue
        if str(metadata.get("format_code") or "") == format_code and str(metadata.get("refresh_source") or "") == refresh_source:
            return row
    return None


def _source_lock_name(source_type: str) -> str | None:
    if source_type == "provisor":
        return PROVISOR_REFRESH_LOCK_NAME
    if source_type == "emit":
        return EMIT_REFRESH_LOCK_NAME
    return None


def _try_acquire_lock_in_transaction(
    db: Session,
    *,
    name: str,
    lock_type: str,
    owner_token: str,
    metadata: dict[str, Any],
) -> bool:
    now = db_now(db)
    lease_until = now + REFRESH_LOCK_LEASE
    existing = db.get(RefreshLock, name)
    if existing is None:
        try:
            with db.begin_nested():
                db.add(
                    RefreshLock(
                        name=name,
                        lock_type=lock_type,
                        owner_token=owner_token,
                        acquired_at=now,
                        heartbeat_at=now,
                        lease_until=lease_until,
                        metadata_json=_json_dumps(metadata),
                    )
                )
                db.flush()
            return True
        except IntegrityError:
            pass
    result = db.execute(
        update(RefreshLock)
        .where(RefreshLock.name == name)
        .where(RefreshLock.lease_until < now)
        .values(
            lock_type=lock_type,
            owner_token=owner_token,
            acquired_at=now,
            heartbeat_at=now,
            lease_until=lease_until,
            metadata_json=_json_dumps(metadata),
        )
    )
    db.flush()
    return int(result.rowcount or 0) == 1


def claim_next_queued_refresh_job(
    db: Session,
    *,
    worker_id: str,
    sources: Iterable[str] = PARSER_QUEUE_SOURCES,
) -> tuple[RefreshJob | None, str | None]:
    now = db_now(db)
    worker_meta = worker_identity(worker_id)
    stmt = (
        select(RefreshJob)
        .where(RefreshJob.source_type.in_(tuple(sources)))
        .where(RefreshJob.status == QUEUED_STATUS)
        .order_by(RefreshJob.id.asc())
        .limit(1)
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    job = db.execute(stmt).scalars().first()
    if job is None:
        return None, None

    owner_token = new_worker_owner_token()
    lock_metadata = {
        **worker_meta,
        "job_id": job.id,
        "source_type": job.source_type,
        "requested_by": job.requested_by,
    }
    if not _try_acquire_lock_in_transaction(
        db,
        name=GLOBAL_REFRESH_LOCK_NAME,
        lock_type="refresh",
        owner_token=owner_token,
        metadata=lock_metadata,
    ):
        db.rollback()
        return None, None
    source_lock = _source_lock_name(str(job.source_type or ""))
    if source_lock and not _try_acquire_lock_in_transaction(
        db,
        name=source_lock,
        lock_type="refresh",
        owner_token=owner_token,
        metadata=lock_metadata,
    ):
        db.rollback()
        return None, None

    result = db.execute(
        update(RefreshJob)
        .where(RefreshJob.id == job.id)
        .where(RefreshJob.status == QUEUED_STATUS)
        .values(
            status=RUNNING_STATUS,
            started_at=now,
            heartbeat_at=now,
            message="Refresh job claimed by parser worker.",
            metadata_json=_json_dumps(
                {
                    **(_json_loads(job.metadata_json, {}) if isinstance(_json_loads(job.metadata_json, {}), dict) else {}),
                    **worker_meta,
                    "owner_token": owner_token,
                    "claimed_at": now.isoformat(),
                    "current_stage": "claimed",
                }
            ),
        )
    )
    if int(result.rowcount or 0) != 1:
        db.rollback()
        return None, None
    db.commit()
    claimed = db.get(RefreshJob, job.id)
    if claimed is not None:
        db.refresh(claimed)
    logger.info("[REFRESH_QUEUE] claimed job_id=%s source=%s worker=%s", job.id, job.source_type, worker_id)
    return claimed, owner_token


def _job_owner_token(job: RefreshJob) -> str:
    metadata = _json_loads(job.metadata_json, {})
    return str(metadata.get("owner_token") or "") if isinstance(metadata, dict) else ""


def _job_has_worker_owner(job: RefreshJob) -> bool:
    metadata = _json_loads(job.metadata_json, {})
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get("owner_token") and (metadata.get("worker_owner_id") or metadata.get("worker_id")))


def _is_stale_executing_job(job: RefreshJob, *, now: datetime, stale_after: timedelta) -> bool:
    if job.status in TERMINAL_STATUSES or job.status == QUEUED_STATUS:
        return False
    if job.status not in EXECUTING_STATUSES:
        return False
    if not _job_has_worker_owner(job):
        return False
    if job.heartbeat_at is None:
        return True
    return job.heartbeat_at < now - stale_after


def _lock_names_for_source(source_type: str) -> list[str]:
    out = [GLOBAL_REFRESH_LOCK_NAME]
    lock_name = _source_lock_name(source_type)
    if lock_name:
        out.append(lock_name)
    return out


def release_stale_owned_locks(db: Session, *, source_type: str, owner_token: str, now: datetime) -> list[str]:
    released: list[str] = []
    for lock_name in _lock_names_for_source(source_type):
        lock = db.get(RefreshLock, lock_name)
        if lock is None:
            continue
        token_matches = bool(owner_token and lock.owner_token == owner_token)
        expired = bool(lock.lease_until and lock.lease_until < now)
        if not token_matches and not expired:
            logger.info(
                "[REFRESH_LOCK_RECOVERY] lock=%s action=preserved reason=foreign_unexpired owner=%s",
                lock_name,
                lock.owner_token,
            )
            continue
        db.delete(lock)
        db.flush()
        released.append(lock_name)
        logger.warning(
            "[REFRESH_LOCK_RECOVERY] lock=%s action=released reason=%s",
            lock_name,
            "owner_token_match" if token_matches else "lease_expired",
        )
    return released


def recover_stale_parser_jobs(
    db: Session,
    *,
    stale_heartbeat_seconds: int = DEFAULT_STALE_HEARTBEAT_SECONDS,
    emit_config: EmitConfig | None = None,
) -> list[RefreshJob]:
    now = db_now(db)
    stale_after = timedelta(seconds=max(30, int(stale_heartbeat_seconds or DEFAULT_STALE_HEARTBEAT_SECONDS)))
    stmt = (
        select(RefreshJob)
        .where(RefreshJob.source_type.in_(PARSER_QUEUE_SOURCES))
        .where(RefreshJob.status.in_(EXECUTING_STATUSES))
        .order_by(RefreshJob.id.asc())
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    rows = db.execute(stmt).scalars().all()
    recovered: list[RefreshJob] = []
    for row in rows:
        if not _is_stale_executing_job(row, now=now, stale_after=stale_after):
            continue
        old_status = row.status
        old_heartbeat = row.heartbeat_at
        old_metadata_json = row.metadata_json
        metadata = _json_loads(row.metadata_json, {})
        if not isinstance(metadata, dict):
            metadata = {}
        owner_token = str(metadata.get("owner_token") or "")
        stale_age_seconds = int((now - row.heartbeat_at).total_seconds()) if row.heartbeat_at else int(stale_after.total_seconds())
        metadata_before_cleanup = dict(metadata)
        metadata.update(
            {
                "stale_reason": "worker_crash_recovered",
                "recovered_at": now.isoformat(),
                "stale_age_seconds": stale_age_seconds,
                "previous_status": old_status,
                "current_stage": "interrupted",
            }
        )
        result = db.execute(
            update(RefreshJob)
            .where(RefreshJob.id == row.id)
            .where(RefreshJob.status == old_status)
            .where(RefreshJob.heartbeat_at == old_heartbeat)
            .where(RefreshJob.metadata_json == old_metadata_json)
            .values(
                status="interrupted",
                finished_at=now,
                heartbeat_at=now,
                message="Refresh interrupted: worker heartbeat stale.",
                error_message=f"worker_heartbeat_stale: no heartbeat for {stale_age_seconds} seconds",
                metadata_json=_json_dumps(metadata),
            )
        )
        if int(result.rowcount or 0) != 1:
            db.rollback()
            logger.info("[REFRESH_RECOVERY] job_id=%s action=skip reason=race_recheck_failed", row.id)
            continue
        locks_released = release_stale_owned_locks(db, source_type=str(row.source_type or ""), owner_token=owner_token, now=now)
        temp_cleanup = {"files_deleted": 0, "files_failed": []}
        if row.source_type == "emit" and owner_token:
            temp_cleanup = cleanup_stale_emit_temp_files(emit_config or EmitConfig(), metadata_before_cleanup)
        metadata["locks_released"] = locks_released
        metadata["temp_cleanup"] = temp_cleanup
        db.execute(
            update(RefreshJob)
            .where(RefreshJob.id == row.id)
            .where(RefreshJob.status == "interrupted")
            .values(metadata_json=_json_dumps(metadata))
        )
        recovered.append(row)
        logger.warning(
            "[REFRESH_RECOVERY] job_id=%s source=%s reason=worker_heartbeat_stale locks_released=%s temp_files_deleted=%s",
            row.id,
            row.source_type,
            locks_released,
            int((temp_cleanup or {}).get("files_deleted") or 0),
        )
    if recovered:
        db.commit()
        for row in recovered:
            db.refresh(row)
    else:
        db.rollback()
    return recovered


def recover_expired_orphan_locks(db: Session) -> list[str]:
    now = db_now(db)
    rows = db.execute(select(RefreshLock).where(RefreshLock.lease_until < now)).scalars().all()
    released: list[str] = []
    for row in rows:
        active_owner = (
            db.execute(
                select(RefreshJob.id)
                .where(RefreshJob.status.in_(EXECUTING_STATUSES))
                .where(RefreshJob.metadata_json.contains(row.owner_token))
                .limit(1)
            )
            .scalars()
            .first()
        )
        if active_owner is not None:
            continue
        released.append(row.name)
        db.delete(row)
        logger.warning("[REFRESH_LOCK_RECOVERY] lock=%s action=released reason=lease_expired_orphan", row.name)
    if released:
        db.commit()
    else:
        db.rollback()
    return released
