from __future__ import annotations

from datetime import timedelta
import hashlib
import logging

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...models import CompetitorMatchJob
from ...timezone import now_kz_naive


logger = logging.getLogger(__name__)


class MatchingJobConflict(RuntimeError):
    pass


def matching_job_idempotency_key(*, source_version_id: int, dependency_fingerprint: str) -> str:
    payload = f"shadow-v1:{int(source_version_id)}:{dependency_fingerprint}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def enqueue_matching_job(
    *,
    db: Session,
    source_version_id: int,
    dependency_fingerprint: str,
    match_snapshot_id: int | None = None,
    max_attempts: int = 3,
) -> tuple[CompetitorMatchJob, bool]:
    key = matching_job_idempotency_key(
        source_version_id=source_version_id,
        dependency_fingerprint=dependency_fingerprint,
    )
    existing = db.scalar(select(CompetitorMatchJob).where(CompetitorMatchJob.idempotency_key == key))
    if existing is not None:
        return existing, True
    try:
        with db.begin_nested():
            row = CompetitorMatchJob(
                idempotency_key=key,
                source_version_id=int(source_version_id),
                match_snapshot_id=match_snapshot_id,
                dependency_fingerprint=str(dependency_fingerprint),
                status="pending",
                max_attempts=max(1, int(max_attempts)),
                available_at=now_kz_naive(),
            )
            db.add(row)
            db.flush()
        return row, False
    except IntegrityError:
        existing = db.scalar(
            select(CompetitorMatchJob).where(CompetitorMatchJob.idempotency_key == key)
        )
        if existing is None:
            raise
        return existing, True


def recover_stale_matching_jobs(*, db: Session) -> int:
    now = now_kz_naive()
    result = db.execute(
        update(CompetitorMatchJob)
        .where(CompetitorMatchJob.status == "running")
        .where(CompetitorMatchJob.lease_expires_at.is_not(None))
        .where(CompetitorMatchJob.lease_expires_at < now)
        .values(
            status=case(
                (
                    CompetitorMatchJob.attempt_count >= CompetitorMatchJob.max_attempts,
                    "failed",
                ),
                else_="pending",
            ),
            available_at=now,
            claimed_by="",
            heartbeat_at=None,
            lease_expires_at=None,
            finished_at=case(
                (
                    CompetitorMatchJob.attempt_count >= CompetitorMatchJob.max_attempts,
                    now,
                ),
                else_=None,
            ),
            error="worker lease expired",
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    count = int(result.rowcount or 0)
    if count:
        logger.warning("[MATCH_CACHE_JOB_RECOVERY] recovered=%s", count)
    return count


def claim_next_matching_job(
    *, db: Session, worker_id: str, lease_seconds: int = 300
) -> CompetitorMatchJob | None:
    now = now_kz_naive()
    row = db.scalar(
        select(CompetitorMatchJob)
        .where(CompetitorMatchJob.status == "pending")
        .where(CompetitorMatchJob.available_at <= now)
        .where(CompetitorMatchJob.attempt_count < CompetitorMatchJob.max_attempts)
        .order_by(CompetitorMatchJob.available_at.asc(), CompetitorMatchJob.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return None
    row.status = "running"
    row.attempt_count = int(row.attempt_count) + 1
    row.claimed_by = str(worker_id)
    row.started_at = row.started_at or now
    row.heartbeat_at = now
    row.lease_expires_at = now + timedelta(seconds=max(10, int(lease_seconds)))
    row.error = ""
    row.updated_at = now
    db.flush()
    return row


def heartbeat_matching_job(
    *, db: Session, job_id: int, worker_id: str, lease_seconds: int = 300
) -> None:
    now = now_kz_naive()
    result = db.execute(
        update(CompetitorMatchJob)
        .where(CompetitorMatchJob.id == int(job_id))
        .where(CompetitorMatchJob.status == "running")
        .where(CompetitorMatchJob.claimed_by == str(worker_id))
        .values(
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=max(10, int(lease_seconds))),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        raise MatchingJobConflict(f"matching job {job_id} is not owned by {worker_id}")


def complete_matching_job(
    *, db: Session, job_id: int, worker_id: str, match_snapshot_id: int
) -> None:
    now = now_kz_naive()
    result = db.execute(
        update(CompetitorMatchJob)
        .where(CompetitorMatchJob.id == int(job_id))
        .where(CompetitorMatchJob.status == "running")
        .where(CompetitorMatchJob.claimed_by == str(worker_id))
        .values(
            status="succeeded",
            match_snapshot_id=int(match_snapshot_id),
            finished_at=now,
            heartbeat_at=now,
            lease_expires_at=None,
            error="",
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        raise MatchingJobConflict(f"matching job {job_id} completion lost ownership")


def fail_matching_job(
    *,
    db: Session,
    job_id: int,
    worker_id: str,
    error: str,
    retryable: bool = True,
    base_retry_seconds: int = 30,
) -> str:
    row = db.scalar(
        select(CompetitorMatchJob)
        .where(CompetitorMatchJob.id == int(job_id))
        .with_for_update()
    )
    if row is None or row.status != "running" or row.claimed_by != str(worker_id):
        raise MatchingJobConflict(f"matching job {job_id} failure lost ownership")
    now = now_kz_naive()
    should_retry = retryable and int(row.attempt_count) < int(row.max_attempts)
    row.status = "pending" if should_retry else "failed"
    row.available_at = now + timedelta(
        seconds=max(1, int(base_retry_seconds)) * (2 ** max(0, int(row.attempt_count) - 1))
    )
    row.claimed_by = ""
    row.heartbeat_at = None
    row.lease_expires_at = None
    row.finished_at = None if should_retry else now
    row.error = str(error or "")[:4000]
    row.updated_at = now
    db.flush()
    logger.warning(
        "[MATCH_CACHE_JOB_FAILURE] job_id=%s retry=%s attempt=%s max_attempts=%s error=%s",
        job_id,
        should_retry,
        row.attempt_count,
        row.max_attempts,
        row.error[:500],
    )
    return row.status
