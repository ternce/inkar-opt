from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import (
    CompetitorPriceListItem,
    CompetitorPricePercentile,
    Job,
    PriceFormat,
    PriceFormatPercentilePreparation,
)
from ..timezone import local_iso, now_kz_naive
from .competitor_percentiles import eligible_percentile_assignments, recalculate_competitor_percentiles
from .jobs import job_to_dict, update_job


logger = logging.getLogger(__name__)

JOB_TYPE = "percentile_preparation"
ACTIVE_JOB_STATUSES = {"pending", "running"}
ACTIVE_PREPARATION_STATUSES = {"pending", "processing"}
TERMINAL_STATUSES = {"ready", "failed", "not_configured"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _status_row(db: Session, price_format_id: int) -> PriceFormatPercentilePreparation:
    row = db.get(PriceFormatPercentilePreparation, price_format_id)
    if row is None:
        row = PriceFormatPercentilePreparation(price_format_id=price_format_id, updated_at=now_kz_naive())
        db.add(row)
        db.flush()
    return row


def _active_job(db: Session, price_format_id: int) -> Job | None:
    return (
        db.execute(
            select(Job)
            .where(Job.type == JOB_TYPE)
            .where(Job.price_format_id == price_format_id)
            .where(Job.status.in_(tuple(ACTIVE_JOB_STATUSES)))
            .order_by(Job.created_at.desc())
        )
        .scalars()
        .first()
    )


def percentile_configuration(db: Session, price_format_id: int) -> dict[str, Any]:
    selected = eligible_percentile_assignments(db=db, price_format_id=price_format_id, require_matched_prices=False)
    sources: list[dict[str, Any]] = []
    for item in selected:
        price_list = item.price_list
        assignment = item.assignment
        sources.append(
            {
                "assignmentId": int(assignment.id),
                "priceListId": int(price_list.id),
                "sourceType": str(price_list.source_type or ""),
                "sourceKey": str(price_list.source_key or ""),
                "branchName": str(price_list.branch_name or price_list.region or ""),
                "competitorName": str(price_list.competitor_name or price_list.supplier or ""),
                "percentileMode": str(assignment.percentile_mode or ""),
                "coefficient": str(assignment.coefficient or ""),
                "priceCoefficient": str(price_list.price_coefficient or ""),
                "lastSuccessAt": str(price_list.last_success_at or ""),
                "syncBatchId": str(price_list.sync_batch_id or ""),
            }
        )
    sources.sort(key=lambda row: (row["sourceKey"], row["priceListId"], row["assignmentId"]))
    source_refreshed_at = max([item.price_list.last_success_at for item in selected if item.price_list.last_success_at], default=None)
    source_refresh_id = "|".join(sorted({str(item.price_list.sync_batch_id or "") for item in selected if item.price_list.sync_batch_id}))
    return {
        "configured": bool(sources),
        "sources": sources,
        "fingerprint": _hash(sources),
        "sourceRefreshId": source_refresh_id,
        "sourceRefreshedAt": source_refreshed_at,
    }


def has_raw_percentile_data(db: Session, price_format_id: int) -> bool:
    selected = eligible_percentile_assignments(db=db, price_format_id=price_format_id, require_matched_prices=False)
    ids = [int(item.price_list.id) for item in selected]
    if not ids:
        return False
    count = int(
        db.execute(
            select(func.count(CompetitorPriceListItem.id))
            .where(CompetitorPriceListItem.price_list_id.in_(ids))
            .where(CompetitorPriceListItem.distributor_price.is_not(None))
            .where(CompetitorPriceListItem.distributor_price > 0)
        ).scalar()
        or 0
    )
    return count > 0


def percentile_preparation_to_dict(db: Session, price_format_id: int) -> dict[str, Any]:
    row = _status_row(db, price_format_id)
    rows_count = int(
        db.execute(
            select(func.count(CompetitorPricePercentile.id)).where(CompetitorPricePercentile.price_format_id == price_format_id)
        ).scalar()
        or 0
    )
    return {
        "priceFormatId": price_format_id,
        "status": row.status or "not_configured",
        "sourceRefreshId": row.source_refresh_id or "",
        "sourceRefreshedAt": local_iso(row.source_refreshed_at) if row.source_refreshed_at else "",
        "startedAt": local_iso(row.started_at) if row.started_at else "",
        "completedAt": local_iso(row.completed_at) if row.completed_at else "",
        "failedAt": local_iso(row.failed_at) if row.failed_at else "",
        "lastError": row.last_error or "",
        "message": _status_message(row.status or "not_configured", row.last_error or ""),
        "rowsCount": rows_count,
        "configurationFingerprint": row.configuration_fingerprint or "",
        "jobId": row.job_id or "",
    }


def _create_job(db: Session, pf: PriceFormat, config: dict[str, Any]) -> Job:
    now = now_kz_naive()
    job = Job(
        id=uuid.uuid4().hex,
        type=JOB_TYPE,
        status="pending",
        format_code=pf.code,
        price_format_id=int(pf.id),
        progress=0,
        message="Подготовка персентилей ожидает запуска",
        logs=json.dumps([], ensure_ascii=False),
        result_json=_json(
            {
                "configurationFingerprint": config["fingerprint"],
                "sourceRefreshId": config["sourceRefreshId"],
                "sourceRefreshedAt": local_iso(config["sourceRefreshedAt"]) if config["sourceRefreshedAt"] else "",
            }
        ),
        error="",
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.flush()
    return job


def _status_message(status: str, last_error: str = "") -> str:
    if status == "ready":
        return "Подготовка персентилей завершена"
    if status == "pending":
        return "Подготовка персентилей ожидает запуска"
    if status == "processing":
        return "Подготовка персентилей выполняется"
    if status == "failed":
        return last_error or "Подготовка персентилей завершилась ошибкой"
    if status == "stale":
        return last_error or "Настройки или источник изменились, нужна повторная подготовка"
    return "Для выбранного формата настройки персентилей ещё не заданы"


def enqueue_percentile_preparation(
    *,
    db: Session,
    price_format_id: int,
    reason: str = "",
    start_worker: bool = True,
) -> dict[str, Any]:
    pf = db.get(PriceFormat, price_format_id)
    if pf is None:
        raise ValueError("price format not found")
    row = _status_row(db, price_format_id)
    config = percentile_configuration(db, price_format_id)
    now = now_kz_naive()

    if not config["configured"]:
        row.status = "not_configured"
        row.last_error = ""
        row.configuration_fingerprint = config["fingerprint"]
        row.source_refresh_id = config["sourceRefreshId"]
        row.source_refreshed_at = config["sourceRefreshedAt"]
        row.updated_at = now
        db.commit()
        return percentile_preparation_to_dict(db, price_format_id)

    active = _active_job(db, price_format_id)
    if active is not None:
        row.status = "processing" if active.status == "running" else "pending"
        row.job_id = active.id
        row.updated_at = now
        db.commit()
        return percentile_preparation_to_dict(db, price_format_id)

    if not has_raw_percentile_data(db, price_format_id):
        row.status = "failed"
        row.failed_at = now
        row.last_error = "Persisted Emit percentile source data is unavailable"
        row.configuration_fingerprint = config["fingerprint"]
        row.source_refresh_id = config["sourceRefreshId"]
        row.source_refreshed_at = config["sourceRefreshedAt"]
        row.updated_at = now
        db.commit()
        return percentile_preparation_to_dict(db, price_format_id)

    job = _create_job(db, pf, config)
    row.status = "pending"
    row.started_at = None
    row.completed_at = None
    row.failed_at = None
    row.last_error = ""
    row.source_refresh_id = config["sourceRefreshId"]
    row.source_refreshed_at = config["sourceRefreshedAt"]
    row.configuration_fingerprint = config["fingerprint"]
    row.job_id = job.id
    row.updated_at = now
    db.commit()
    if start_worker:
        start_percentile_preparation_worker(job.id)
    logger.info("[PERCENTILE_PREP] queued price_format_id=%s job_id=%s reason=%s", price_format_id, job.id, reason)
    return percentile_preparation_to_dict(db, price_format_id)


def _job_payload(job: Job) -> dict[str, Any]:
    try:
        return json.loads(job.result_json or "{}")
    except Exception:
        return {}


def run_percentile_preparation_job(job_id: str) -> dict[str, Any] | None:
    db = SessionLocal()
    started = time.perf_counter()
    try:
        job = db.get(Job, job_id)
        if job is None or job.price_format_id is None:
            return None
        if job.status not in ACTIVE_JOB_STATUSES:
            return job_to_dict(job)
        price_format_id = int(job.price_format_id)
        row = _status_row(db, price_format_id)
        expected = _job_payload(job)
        config = percentile_configuration(db, price_format_id)
        if expected.get("configurationFingerprint") and expected.get("configurationFingerprint") != config["fingerprint"]:
            job.status = "error"
            job.error = "configuration changed before preparation started"
            job.finished_at = now_kz_naive()
            row.status = "stale"
            row.last_error = job.error
            row.updated_at = now_kz_naive()
            db.commit()
            enqueue_percentile_preparation(db=db, price_format_id=price_format_id, reason="configuration_changed_before_start")
            return job_to_dict(job)
        update_job(db, job, status="running", progress=5, message="Персентили рассчитываются", log_level="info")
        row = _status_row(db, price_format_id)
        row.status = "processing"
        row.started_at = now_kz_naive()
        row.last_error = ""
        row.updated_at = row.started_at
        db.commit()

        summary = recalculate_competitor_percentiles(db=db, price_format_id=price_format_id)
        current = percentile_configuration(db, price_format_id)
        if current["fingerprint"] != config["fingerprint"] or current["sourceRefreshId"] != config["sourceRefreshId"]:
            db.rollback()
            job = db.get(Job, job_id)
            row = _status_row(db, price_format_id)
            if job is not None:
                job.status = "error"
                job.error = "configuration or source changed during preparation"
                job.finished_at = now_kz_naive()
                job.updated_at = job.finished_at
            row.status = "stale"
            row.last_error = "Configuration or source changed during preparation"
            row.updated_at = now_kz_naive()
            db.commit()
            enqueue_percentile_preparation(db=db, price_format_id=price_format_id, reason="configuration_changed_during_preparation")
            return job_to_dict(job) if job is not None else None

        rows_count = int(
            db.execute(
                select(func.count(CompetitorPricePercentile.id)).where(CompetitorPricePercentile.price_format_id == price_format_id)
            ).scalar()
            or 0
        )
        job = db.get(Job, job_id)
        row = _status_row(db, price_format_id)
        now = now_kz_naive()
        row.status = "ready"
        row.completed_at = now
        row.failed_at = None
        row.last_error = ""
        row.rows_count = rows_count
        row.configuration_fingerprint = current["fingerprint"]
        row.source_refresh_id = current["sourceRefreshId"]
        row.source_refreshed_at = current["sourceRefreshedAt"]
        row.updated_at = now
        if job is not None:
            job.status = "success"
            job.progress = 100
            job.message = "Персентили готовы"
            job.result_json = _json({**summary, "rowsCount": rows_count, "elapsedSeconds": round(time.perf_counter() - started, 3)})
            job.finished_at = now
            job.updated_at = now
        db.commit()
        logger.info(
            "[PERCENTILE_PREP] ready price_format_id=%s rows=%s elapsed_sec=%s",
            price_format_id,
            rows_count,
            round(time.perf_counter() - started, 3),
        )
        return job_to_dict(job) if job is not None else None
    except Exception as exc:
        db.rollback()
        job = db.get(Job, job_id)
        if job is not None and job.price_format_id is not None:
            row = _status_row(db, int(job.price_format_id))
            now = now_kz_naive()
            row.status = "failed"
            row.failed_at = now
            row.last_error = str(exc)
            row.updated_at = now
            job.status = "error"
            job.error = str(exc)
            job.message = "Ошибка подготовки персентилей"
            job.finished_at = now
            job.updated_at = now
            db.commit()
        logger.exception("[PERCENTILE_PREP] failed job_id=%s", job_id)
        return job_to_dict(job) if job is not None else None
    finally:
        db.close()


def start_percentile_preparation_worker(job_id: str) -> None:
    thread = threading.Thread(target=run_percentile_preparation_job, args=(job_id,), daemon=True)
    thread.start()


def resume_pending_percentile_preparations(*, db: Session, start_worker: bool = True) -> int:
    rows = (
        db.execute(
            select(Job)
            .where(Job.type == JOB_TYPE)
            .where(Job.status.in_(("pending", "running")))
            .order_by(Job.created_at.asc())
        )
        .scalars()
        .all()
    )
    count = 0
    for job in rows:
        job.status = "pending"
        job.updated_at = now_kz_naive()
        if job.price_format_id is not None:
            row = _status_row(db, int(job.price_format_id))
            row.status = "pending"
            row.job_id = job.id
            row.updated_at = now_kz_naive()
        count += 1
    db.commit()
    if start_worker:
        for job in rows:
            start_percentile_preparation_worker(job.id)
    return count


def retry_waiting_percentile_preparations(*, db: Session, start_worker: bool = True) -> int:
    rows = db.execute(select(PriceFormatPercentilePreparation).where(PriceFormatPercentilePreparation.status.in_(("failed", "pending", "stale", "not_configured")))).scalars().all()
    count = 0
    for row in rows:
        status = enqueue_percentile_preparation(db=db, price_format_id=int(row.price_format_id), reason="source_refresh_completed", start_worker=start_worker)
        if status.get("status") in {"pending", "processing", "ready"}:
            count += 1
    return count


def ensure_percentile_ready_for_generation(db: Session, pf: PriceFormat) -> None:
    mode = str(pf.competitor_price_mode or "regular").strip().lower()
    if mode not in {"percentile", "mixed"}:
        return
    status = percentile_preparation_to_dict(db, int(pf.id))
    if status["status"] == "ready" and int(status.get("rowsCount") or 0) > 0:
        return
    raise ValueError(f"percentile preparation is not ready: {status['status']} {status.get('lastError') or ''}".strip())
