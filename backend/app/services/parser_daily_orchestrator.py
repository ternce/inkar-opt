from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import ParserDailyRun, PriceFormat, PriceSourceAccount, RefreshJob, RefreshLock
from ..timezone import now_kz_naive
from .emit_worker import EmitConfig
from .refresh_queue import (
    ACTIVE_QUEUE_STATUSES,
    TERMINAL_STATUSES,
    active_parser_refresh_job,
    create_queued_emit_refresh_job,
    create_queued_price_format_refresh_job,
    recover_stale_parser_jobs,
)

logger = logging.getLogger(__name__)

PENDING = "PENDING"
EMIT_RUNNING = "EMIT_RUNNING"
EMIT_VALIDATING = "EMIT_VALIDATING"
EMIT_SUSPICIOUS = "EMIT_SUSPICIOUS"
EMIT_RETRYING = "EMIT_RETRYING"
PLK_RUNNING = "PLK_RUNNING"
SUCCESS = "SUCCESS"
SUSPICIOUS = "SUSPICIOUS"
FAILED = "FAILED"
SKIPPED_ALREADY_RUNNING = "SKIPPED_ALREADY_RUNNING"

DAILY_RUN_STATUSES = {
    PENDING,
    EMIT_RUNNING,
    EMIT_VALIDATING,
    EMIT_SUSPICIOUS,
    EMIT_RETRYING,
    PLK_RUNNING,
    SUCCESS,
    SUSPICIOUS,
    FAILED,
    SKIPPED_ALREADY_RUNNING,
}
FINAL_STATUSES = {SUCCESS, SUSPICIOUS, FAILED, SKIPPED_ALREADY_RUNNING}
EMIT_ACTIVE_STATES = {EMIT_RUNNING, EMIT_RETRYING}


def _loads(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def current_business_date(settings: Settings | None = None, *, now: datetime | None = None) -> date:
    cfg = settings or get_settings()
    timezone = ZoneInfo(cfg.app_timezone)
    if now is None:
        local_now = datetime.now(timezone)
    elif now.tzinfo is None:
        local_now = now.replace(tzinfo=timezone)
    else:
        local_now = now.astimezone(timezone)
    return local_now.date()


def daily_run_to_dict(row: ParserDailyRun | None, db: Session | None = None) -> dict[str, Any]:
    if row is None:
        return {"status": "IDLE"}
    emit_status = None
    plk_status = None
    if db is not None:
        emit = db.get(RefreshJob, row.emit_job_id) if row.emit_job_id else None
        plk = db.get(RefreshJob, row.plk_job_id) if row.plk_job_id else None
        emit_status = emit.status if emit else None
        plk_status = plk.status if plk else None
    return {
        "id": row.id,
        "businessDate": row.business_date.isoformat(),
        "status": row.status,
        "startedAt": row.started_at.isoformat() if row.started_at else None,
        "finishedAt": row.finished_at.isoformat() if row.finished_at else None,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
        "emitJobId": row.emit_job_id,
        "emitStatus": emit_status,
        "emitAttemptCount": int(row.emit_attempt_count or 0),
        "plkJobId": row.plk_job_id,
        "plkStatus": plk_status,
        "suspiciousReason": row.suspicious_reason or "",
        "errorMessage": row.error_message or "",
        "requestedBy": row.requested_by or "",
        "triggerSource": row.trigger_source or "",
        "notificationStatus": row.notification_status,
        "metadata": _loads(row.metadata_json),
    }


def create_or_get_daily_run(
    db: Session,
    *,
    business_date: date,
    requested_by: str,
    trigger_source: str = "manual",
) -> tuple[ParserDailyRun, bool]:
    existing = db.execute(
        select(ParserDailyRun).where(ParserDailyRun.business_date == business_date)
    ).scalars().first()
    if existing is not None:
        return existing, False
    now = now_kz_naive()
    row = ParserDailyRun(
        business_date=business_date,
        status=PENDING,
        created_at=now,
        updated_at=now,
        requested_by=requested_by,
        trigger_source=trigger_source,
        metadata_json="{}",
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
        db.commit()
        db.refresh(row)
        return row, True
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(ParserDailyRun).where(ParserDailyRun.business_date == business_date)
        ).scalars().one()
        return existing, False


def configured_morning_accounts(db: Session) -> list[PriceSourceAccount]:
    return list(
        db.execute(
            select(PriceSourceAccount)
            .where(PriceSourceAccount.source_type == "provisor")
            .where(PriceSourceAccount.is_active.is_(True))
            .where(PriceSourceAccount.run_in_morning_pipeline.is_(True))
            .order_by(PriceSourceAccount.id.asc())
        ).scalars().all()
    )


@dataclass(frozen=True)
class EmitValidation:
    healthy: bool
    suspicious: bool
    retryable: bool
    reason: str
    details: dict[str, Any]


def _filial_result(metadata: dict[str, Any], filial_id: int) -> dict[str, Any] | None:
    for item in metadata.get("filials") or []:
        if isinstance(item, dict) and int(item.get("filial_id") or 0) == filial_id:
            return item
    return None


def _row_count(item: dict[str, Any]) -> int:
    return int(item.get("final_rows_saved") or item.get("final_production_row_count") or 0)


def _structurally_healthy_filial(item: dict[str, Any], minimum: int) -> tuple[bool, str]:
    if not item.get("ok"):
        return False, str(item.get("error") or "filial_failed")
    saved = _row_count(item)
    if saved < minimum:
        return False, f"rows_below_minimum:{saved}<{minimum}"
    for field in ("final_inserted_row_count", "final_production_row_count"):
        value = item.get(field)
        if value is not None and int(value or 0) != saved:
            return False, f"row_count_mismatch:{field}={value},final_rows_saved={saved}"
    return True, ""


def historical_filial_counts(
    db: Session,
    *,
    filial_id: int,
    exclude_job_id: int | None,
    window: int,
    minimum: int,
) -> list[int]:
    stmt = (
        select(RefreshJob)
        .where(RefreshJob.source_type == "emit")
        .where(RefreshJob.status == "success")
        .order_by(RefreshJob.finished_at.desc().nullslast(), RefreshJob.id.desc())
    )
    if exclude_job_id is not None:
        stmt = stmt.where(RefreshJob.id != exclude_job_id)
    counts: list[int] = []
    for job in db.execute(stmt).scalars().all():
        item = _filial_result(_loads(job.metadata_json), filial_id)
        if item is None:
            continue
        healthy, _ = _structurally_healthy_filial(item, minimum)
        if not healthy:
            continue
        counts.append(_row_count(item))
        if len(counts) >= window:
            break
    return counts


def _retryable_error(text: str) -> bool:
    value = str(text or "").casefold()
    non_retryable = (
        "invalid credential", "unauthorized", "forbidden", "http 401", "http 403",
        "authentication", "auth_error", "invalid password", "unsupported", "schema",
        "configuration", "integrityerror", "constraint",
    )
    if any(marker in value for marker in non_retryable):
        return False
    retryable = (
        "timeout", "timed out", "connection reset", "connection aborted", "http 429",
        "http 500", "http 502", "http 503", "http 504", "incomplete", "stale",
        "connection refused", "server disconnected",
    )
    return any(marker in value for marker in retryable)


def validate_emit_job(db: Session, job: RefreshJob, *, settings: Settings | None = None) -> EmitValidation:
    cfg = settings or get_settings()
    emit_cfg = EmitConfig.from_settings(cfg)
    metadata = _loads(job.metadata_json)
    expected = [int(x) for x in (metadata.get("requested_filials") or metadata.get("filial_ids") or emit_cfg.filial_ids)]
    expected = list(dict.fromkeys(x for x in expected if x > 0))
    if job.status != "success":
        reason = f"emit_status:{job.status}"
        error_text = " ".join(
            [job.error_message or ""]
            + [str(item.get("error") or "") for item in metadata.get("filials") or [] if isinstance(item, dict)]
        )
        return EmitValidation(
            healthy=False,
            suspicious=job.status == "partial_success",
            retryable=_retryable_error(error_text),
            reason=reason if not error_text.strip() else f"{reason}:{error_text.strip()[:400]}",
            details={"expectedFilials": expected, "baselineConfidence": "not_evaluated"},
        )
    completed = {
        int(item.get("filial_id") or 0)
        for item in metadata.get("filials") or []
        if isinstance(item, dict) and item.get("ok")
    }
    if set(expected) != completed:
        return EmitValidation(False, True, True, f"requested_completed_mismatch:expected={expected},completed={sorted(completed)}", {"expectedFilials": expected, "completedFilials": sorted(completed)})

    baseline_details: dict[str, Any] = {}
    for filial_id in expected:
        item = _filial_result(metadata, filial_id)
        if item is None:
            return EmitValidation(False, True, True, f"missing_filial:{filial_id}", baseline_details)
        structurally_healthy, reason = _structurally_healthy_filial(item, emit_cfg.min_final_rows)
        if not structurally_healthy:
            retryable = reason.startswith("rows_below_minimum") or _retryable_error(reason)
            return EmitValidation(False, True, retryable, f"filial={filial_id}:{reason}", baseline_details)
        count = _row_count(item)
        history = historical_filial_counts(
            db,
            filial_id=filial_id,
            exclude_job_id=int(job.id),
            window=cfg.parser_daily_baseline_window,
            minimum=emit_cfg.min_final_rows,
        )
        detail: dict[str, Any] = {"current": count, "samples": history}
        if len(history) < cfg.parser_daily_baseline_min_samples:
            detail["confidence"] = "insufficient"
        else:
            baseline = float(statistics.median(history))
            threshold = baseline * cfg.parser_daily_baseline_ratio
            detail.update({"confidence": "sufficient", "median": baseline, "threshold": threshold})
            if count < threshold:
                baseline_details[str(filial_id)] = detail
                return EmitValidation(False, True, True, f"filial={filial_id}:relative_row_drop:{count}<{threshold:.2f}", baseline_details)
        baseline_details[str(filial_id)] = detail
    return EmitValidation(True, False, False, "", {"baselines": baseline_details})


def _owner_locks_present(db: Session, job: RefreshJob) -> bool:
    token = str(_loads(job.metadata_json).get("owner_token") or "")
    if not token:
        return False
    return db.execute(select(RefreshLock.name).where(RefreshLock.owner_token == token).limit(1)).scalar() is not None


def _metadata_update(run: ParserDailyRun, **values: Any) -> None:
    metadata = _loads(run.metadata_json)
    metadata.update(values)
    run.metadata_json = _dumps(metadata)


def _finish(run: ParserDailyRun, *, status: str, error: str = "", suspicious: str = "") -> None:
    if status not in FINAL_STATUSES:
        raise ValueError(f"invalid final daily-run status: {status}")
    now = now_kz_naive()
    run.status = status
    run.finished_at = now
    run.updated_at = now
    run.error_message = error
    run.suspicious_reason = suspicious


def _enqueue_emit(db: Session, run: ParserDailyRun, cfg: Settings, *, retry: bool) -> None:
    filial_ids = list(EmitConfig.from_settings(cfg).filial_ids)
    job = create_queued_emit_refresh_job(
        db,
        mode="all",
        filial_ids=filial_ids,
        requested_by=f"daily_run:{run.id}",
        daily_run_id=int(run.id),
        commit=False,
    )
    run.emit_job_id = int(job.id)
    run.emit_attempt_count = int(run.emit_attempt_count or 0) + 1
    run.status = EMIT_RETRYING if retry else EMIT_RUNNING
    run.started_at = run.started_at or now_kz_naive()
    run.updated_at = now_kz_naive()
    _metadata_update(run, expectedFilialIds=filial_ids)


def _enqueue_plk(db: Session, run: ParserDailyRun) -> bool:
    accounts = configured_morning_accounts(db)
    price_format = db.execute(select(PriceFormat).order_by(PriceFormat.id.asc()).limit(1)).scalars().first()
    if not accounts or price_format is None:
        _finish(run, status=FAILED, error="No active morning Provisor accounts or price format configured.")
        return False
    account_ids = [int(row.id) for row in accounts]
    job = create_queued_price_format_refresh_job(
        db,
        format_code=str(price_format.code),
        refresh_source="provisor",
        payload={
            "source": "provisor",
            "sourceType": "provisor",
            "forceRefresh": True,
            "accountIds": account_ids,
            "requestedBy": f"daily_run:{run.id}",
        },
        requested_by=f"daily_run:{run.id}",
        daily_run_id=int(run.id),
        commit=False,
    )
    run.plk_job_id = int(job.id)
    run.status = PLK_RUNNING
    run.updated_at = now_kz_naive()
    _metadata_update(run, morningAccountIds=account_ids, priceFormatCode=str(price_format.code))
    return True


def advance_daily_run(db: Session, run_id: int, *, settings: Settings | None = None) -> ParserDailyRun | None:
    cfg = settings or get_settings()
    for _ in range(5):
        stmt = select(ParserDailyRun).where(ParserDailyRun.id == run_id)
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        run = db.execute(stmt).scalars().first()
        if run is None or run.status in FINAL_STATUSES:
            return run
        if run.status == PENDING:
            blocker = active_parser_refresh_job(db)
            if blocker is not None and blocker.daily_run_id != run.id:
                _finish(run, status=SKIPPED_ALREADY_RUNNING, error=f"Parser job {blocker.id} is already active.")
            else:
                _enqueue_emit(db, run, cfg, retry=False)
            db.commit()
            continue
        if run.status in EMIT_ACTIVE_STATES:
            job = db.get(RefreshJob, run.emit_job_id) if run.emit_job_id else None
            if job is None:
                _finish(run, status=FAILED, error="Linked Emit refresh job is missing.")
                db.commit()
                continue
            if job.status in ACTIVE_QUEUE_STATUSES:
                return run
            if job.status not in TERMINAL_STATUSES:
                _finish(run, status=FAILED, error=f"Unsupported Emit job state: {job.status}")
                db.commit()
                continue
            run.status = EMIT_VALIDATING
            run.updated_at = now_kz_naive()
            db.commit()
            continue
        if run.status in {EMIT_VALIDATING, EMIT_SUSPICIOUS}:
            job = db.get(RefreshJob, run.emit_job_id) if run.emit_job_id else None
            if job is None:
                _finish(run, status=FAILED, error="Linked Emit refresh job is missing during validation.")
                db.commit()
                continue
            validation = validate_emit_job(db, job, settings=cfg)
            _metadata_update(run, emitValidation=validation.details)
            if validation.healthy:
                run.suspicious_reason = ""
                _enqueue_plk(db, run)
                db.commit()
                continue
            run.suspicious_reason = validation.reason
            if validation.retryable and int(run.emit_attempt_count or 0) < 2:
                if _owner_locks_present(db, job):
                    run.status = EMIT_SUSPICIOUS
                    run.updated_at = now_kz_naive()
                    db.commit()
                    return run
                _enqueue_emit(db, run, cfg, retry=True)
                db.commit()
                continue
            _finish(
                run,
                status=SUSPICIOUS if validation.suspicious else FAILED,
                error="" if validation.suspicious else validation.reason,
                suspicious=validation.reason if validation.suspicious else "",
            )
            db.commit()
            continue
        if run.status == PLK_RUNNING:
            job = db.get(RefreshJob, run.plk_job_id) if run.plk_job_id else None
            if job is None:
                _finish(run, status=FAILED, error="Linked PLK refresh job is missing.")
                db.commit()
                continue
            if job.status in ACTIVE_QUEUE_STATUSES:
                return run
            if job.status == "success" and int(job.failed_count or 0) == 0:
                _finish(run, status=SUCCESS)
            else:
                detail = job.error_message or f"PLK refresh ended with status={job.status}, failed_count={int(job.failed_count or 0)}"
                _finish(run, status=FAILED, error=detail)
            db.commit()
            continue
        _finish(run, status=FAILED, error=f"Unsupported daily-run state: {run.status}")
        db.commit()
    return db.get(ParserDailyRun, run_id)


def advance_pending_daily_runs(db: Session, *, settings: Settings | None = None) -> list[int]:
    cfg = settings or get_settings()
    recover_stale_parser_jobs(
        db,
        stale_heartbeat_seconds=cfg.parser_worker_stale_heartbeat_seconds,
        emit_config=EmitConfig.from_settings(cfg),
    )
    ids = list(
        db.execute(
            select(ParserDailyRun.id)
            .where(ParserDailyRun.status.not_in(FINAL_STATUSES))
            .order_by(ParserDailyRun.id.asc())
        ).scalars().all()
    )
    for run_id in ids:
        advance_daily_run(db, int(run_id), settings=cfg)
    return [int(item) for item in ids]
