from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Job, PriceFormat
from ..timezone import local_iso, now_kz_naive

logger = logging.getLogger(__name__)

ACTIVE_JOB_STATUSES = ("pending", "running")
STALE_JOB_SECONDS = 60


def _json_loads(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "type": job.type,
        "status": job.status,
        "format_code": job.format_code,
        "price_format_id": job.price_format_id,
        "account_id": job.account_id,
        "progress": int(job.progress or 0),
        "message": job.message,
        "logs": _json_loads(job.logs, []),
        "result": _json_loads(job.result_json, {}),
        "error": job.error or None,
        "created_at": local_iso(job.created_at) if job.created_at else "",
        "started_at": local_iso(job.started_at) if job.started_at else "",
        "updated_at": local_iso(job.updated_at) if job.updated_at else "",
        "finished_at": local_iso(job.finished_at) if job.finished_at else "",
    }


def _job_heartbeat_age_seconds(job: Job, now: datetime) -> float:
    marker = job.updated_at or job.started_at or job.created_at
    if marker is None:
        return STALE_JOB_SECONDS + 1
    return (now - marker).total_seconds()


def _mark_stale_job(db: Session, job: Job, now: datetime, age_seconds: float) -> None:
    message = "Задача зависла и была помечена как ошибка"
    job.status = "error"
    job.message = message
    job.error = f"stale background job: no heartbeat for {int(age_seconds)}s"
    job.finished_at = now
    job.updated_at = now
    append_job_log(
        job,
        level="error",
        message=message,
        meta={"reason": "stale_job", "staleSeconds": int(age_seconds)},
    )
    db.commit()
    logger.warning(
        "Marked stale background job as error: id=%s type=%s format_code=%s age_seconds=%s",
        job.id,
        job.type,
        job.format_code,
        int(age_seconds),
    )


def get_active_job(*, db: Session, job_type: str, format_code: str, account_id: int | None = None) -> Job | None:
    stmt = (
        select(Job)
        .where(Job.type == job_type)
        .where(Job.format_code == format_code)
        .where(Job.status.in_(ACTIVE_JOB_STATUSES))
        .order_by(Job.created_at.desc())
    )
    if account_id is not None:
        stmt = stmt.where(Job.account_id == account_id)

    now = now_kz_naive()
    for row in db.execute(stmt).scalars().all():
        age_seconds = _job_heartbeat_age_seconds(row, now)
        if age_seconds > STALE_JOB_SECONDS:
            _mark_stale_job(db, row, now, age_seconds)
            continue
        return row
    return None


def create_job(
    *,
    db: Session,
    job_type: str,
    format_code: str,
    price_format_id: int | None = None,
    account_id: int | None = None,
    message: str = "Создана задача",
) -> Job:
    if price_format_id is None and format_code:
        pf = db.execute(select(PriceFormat).where(PriceFormat.code == format_code)).scalars().first()
        price_format_id = int(pf.id) if pf else None
    now = now_kz_naive()
    job = Job(
        id=uuid.uuid4().hex,
        type=job_type,
        status="pending",
        format_code=format_code,
        price_format_id=price_format_id,
        account_id=account_id,
        progress=0,
        message=message,
        logs=json.dumps([], ensure_ascii=False),
        result_json=json.dumps({}, ensure_ascii=False),
        error="",
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def append_job_log(job: Job, *, level: str, message: str, meta: dict[str, Any] | None = None) -> None:
    logs = _json_loads(job.logs, [])
    logs.append(
        {
            "timestamp": local_iso(now_kz_naive()),
            "level": level,
            "message": message,
            "meta": meta or {},
        }
    )
    job.logs = json.dumps(logs[-500:], ensure_ascii=False)


def update_job(
    db: Session,
    job: Job,
    *,
    status: str | None = None,
    progress: int | None = None,
    message: str | None = None,
    result: Any | None = None,
    error: str | None = None,
    log_level: str | None = None,
    log_meta: dict[str, Any] | None = None,
) -> None:
    now = now_kz_naive()
    if status is not None:
        job.status = status
        if status == "running" and job.started_at is None:
            job.started_at = now
        if status in {"success", "error", "cancelled"}:
            job.finished_at = now
    if progress is not None:
        job.progress = max(0, min(100, int(progress)))
    if message is not None:
        job.message = message[:512]
    if result is not None:
        job.result_json = json.dumps(result, ensure_ascii=False, default=str)
    if error is not None:
        job.error = str(error)
    if log_level and message:
        append_job_log(job, level=log_level, message=message, meta=log_meta)
    job.updated_at = now
    db.commit()


async def run_job(
    *,
    job_id: str,
    operation: Callable[[Session, Job], Any | Awaitable[Any]],
) -> None:
    db = SessionLocal()
    try:
        db.expire_all()
        job = db.get(Job, job_id)
        if job is None:
            logger.error("Background job not found: %s", job_id)
            return
        update_job(db, job, status="running", progress=max(1, int(job.progress or 0)), message="Задача запущена", log_level="info")
        result = operation(db, job)
        if inspect.isawaitable(result):
            result = await result
        db.expire_all()
        job = db.get(Job, job_id)
        if job is not None and job.status not in {"error", "cancelled"}:
            final_message = job.message or "Готово"
            update_job(db, job, status="success", progress=100, message=final_message, result=result, log_level="info")
    except asyncio.CancelledError:
        db.rollback()
        db.expire_all()
        job = db.get(Job, job_id)
        if job is not None:
            update_job(db, job, status="cancelled", message="Задача отменена", error="cancelled", log_level="warning")
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Background job failed: %s", job_id)
        job = db.get(Job, job_id)
        if job is not None:
            update_job(db, job, status="error", message="Ошибка выполнения задачи", error=str(e), log_level="error")
    finally:
        db.close()


def schedule_job(job_id: str, operation: Callable[[Session, Job], Any | Awaitable[Any]]) -> None:
    asyncio.create_task(run_job(job_id=job_id, operation=operation))
