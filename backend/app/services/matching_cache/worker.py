from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import CompetitorMatchJob, CompetitorMatchSnapshot
from ...timezone import now_kz_naive
from .config import matching_cache_worker_enabled
from .dependencies import build_dependency_vector
from .dependency_tracking import assert_database_dependency_tracking
from .jobs import (
    claim_next_matching_job,
    complete_matching_job,
    fail_matching_job,
    recover_stale_matching_jobs,
)
from .shadow import ShadowSnapshotConflict, build_shadow_snapshot, get_or_create_shadow_snapshot


logger = logging.getLogger(__name__)


def run_one_matching_job(
    *, session_factory: Callable[[], Session], worker_id: str, lease_seconds: int = 300
) -> str:
    """Process at most one job. No scheduler invokes this while the worker flag is false."""

    if not matching_cache_worker_enabled():
        return "disabled"
    job_id: int | None = None
    snapshot_id: int | None = None
    with session_factory() as db:
        recover_stale_matching_jobs(db=db)
        job = claim_next_matching_job(
            db=db, worker_id=worker_id, lease_seconds=lease_seconds
        )
        if job is None:
            db.commit()
            return "idle"
        job_id = int(job.id)
        db.commit()

    try:
        with session_factory() as db:
            assert_database_dependency_tracking(db)
            job = db.scalar(
                select(CompetitorMatchJob)
                .where(CompetitorMatchJob.id == job_id)
                .where(CompetitorMatchJob.status == "running")
                .where(CompetitorMatchJob.claimed_by == worker_id)
                .with_for_update()
            )
            if job is None:
                return "lost"
            vector = build_dependency_vector(db=db)
            if vector.fingerprint != job.dependency_fingerprint:
                raise ShadowSnapshotConflict("stale dependency fingerprint at job execution")
            snapshot, _ = get_or_create_shadow_snapshot(
                db=db, source_version_id=int(job.source_version_id), vector=vector
            )
            snapshot_id = int(snapshot.id)
            job.match_snapshot_id = snapshot_id
            result = build_shadow_snapshot(
                db=db,
                snapshot_id=snapshot_id,
                expected_dependency_fingerprint=job.dependency_fingerprint,
            )
            complete_matching_job(
                db=db,
                job_id=job_id,
                worker_id=worker_id,
                match_snapshot_id=result.snapshot_id,
            )
            db.commit()
            return "succeeded"
    except Exception as exc:
        retryable = not isinstance(exc, ShadowSnapshotConflict)
        with session_factory() as db:
            job = db.get(CompetitorMatchJob, job_id)
            if job is None or job.status != "running" or job.claimed_by != worker_id:
                logger.exception("Matching job failed after ownership was lost: job_id=%s", job_id)
                return "lost"
            if snapshot_id is not None:
                snapshot = db.get(CompetitorMatchSnapshot, snapshot_id)
                if snapshot is not None and snapshot.status not in {"ready", "published"}:
                    snapshot.status = "failed"
                    snapshot.failed_at = now_kz_naive()
                    snapshot.error = str(exc)[:4000]
            status = fail_matching_job(
                db=db,
                job_id=job_id,
                worker_id=worker_id,
                error=str(exc),
                retryable=retryable,
            )
            db.commit()
        logger.exception("[MATCH_CACHE_JOB_PROCESSING_FAILED] job_id=%s", job_id)
        return status
