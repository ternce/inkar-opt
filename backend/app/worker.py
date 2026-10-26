from __future__ import annotations

import asyncio
import logging
import signal

from .config import get_settings
from .db import SessionLocal
from .main import _execute_claimed_refresh_job, _start_sap_balance_scheduler, _worker_shutdown, _worker_startup
from .services.emit_worker import EmitConfig
from .services.refresh_queue import (
    claim_next_queued_refresh_job,
    recover_expired_orphan_locks,
    recover_stale_parser_jobs,
    worker_identity,
)
from .services.parser_daily_orchestrator import advance_pending_daily_runs
from .services.matching_cache.config import (
    matching_cache_worker_enabled,
    matching_cache_worker_lease_seconds,
    matching_cache_worker_poll_interval_seconds,
)
from .services.matching_cache.worker import run_one_matching_job

logger = logging.getLogger(__name__)


async def _poll_refresh_queue(*, stop_event: asyncio.Event, worker_id: str, poll_interval: float) -> None:
    retry_delay = poll_interval
    settings = get_settings()
    recovery_interval = max(30.0, min(60.0, settings.parser_worker_stale_heartbeat_seconds / 3))
    next_recovery_at = 0.0
    while not stop_event.is_set():
        claimed_id: int | None = None
        owner_token: str | None = None
        had_error = False
        ensure_sap_scheduler = False
        try:
            with SessionLocal() as db:
                loop_now = asyncio.get_running_loop().time()
                if loop_now >= next_recovery_at:
                    recover_stale_parser_jobs(
                        db,
                        stale_heartbeat_seconds=settings.parser_worker_stale_heartbeat_seconds,
                        emit_config=EmitConfig.from_settings(settings),
                    )
                    recover_expired_orphan_locks(db)
                    next_recovery_at = loop_now + recovery_interval
                    ensure_sap_scheduler = True
                advance_pending_daily_runs(db)
                job, token = claim_next_queued_refresh_job(db, worker_id=worker_id)
                if job is not None and token:
                    claimed_id = int(job.id)
                    owner_token = token
            if ensure_sap_scheduler:
                _start_sap_balance_scheduler()
            if claimed_id is not None and owner_token:
                retry_delay = poll_interval
                await _execute_claimed_refresh_job(claimed_id, owner_token=owner_token)
                continue
            retry_delay = poll_interval
        except asyncio.CancelledError:
            raise
        except Exception:
            had_error = True
            logger.exception("[REFRESH_WORKER_DB_RETRY] worker=%s retry_in_sec=%s", worker_id, retry_delay)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=retry_delay)
        except asyncio.TimeoutError:
            pass
        retry_delay = min(max(poll_interval, retry_delay * 2), 60) if had_error else poll_interval


async def _poll_matching_cache_queue(
    *, stop_event: asyncio.Event, worker_id: str, poll_interval: float, lease_seconds: int
) -> None:
    """Run the existing matching-cache state machine without blocking other queues.

    Cache matching is intentionally single-concurrency per worker process.  The
    database job claim uses ``FOR UPDATE SKIP LOCKED`` and an expiring lease, so
    multiple worker hosts may safely run this poller without duplicate claims.
    """

    completed = 0
    failed = 0
    retry_delay = poll_interval
    while not stop_event.is_set():
        had_error = False
        try:
            result = await asyncio.to_thread(
                run_one_matching_job,
                session_factory=SessionLocal,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )
            if result == "succeeded":
                completed += 1
                retry_delay = poll_interval
                logger.info(
                    "[MATCH_CACHE_WORKER_METRICS] worker=%s result=%s completed=%s failed=%s",
                    worker_id,
                    result,
                    completed,
                    failed,
                )
                continue
            if result == "failed":
                failed += 1
            if result in {"pending", "failed", "lost"}:
                logger.warning(
                    "[MATCH_CACHE_WORKER_METRICS] worker=%s result=%s completed=%s failed=%s",
                    worker_id,
                    result,
                    completed,
                    failed,
                )
            retry_delay = poll_interval
        except asyncio.CancelledError:
            raise
        except Exception:
            had_error = True
            logger.exception(
                "[MATCH_CACHE_WORKER_DB_RETRY] worker=%s retry_in_sec=%s",
                worker_id,
                retry_delay,
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=retry_delay)
        except asyncio.TimeoutError:
            pass
        retry_delay = min(max(poll_interval, retry_delay * 2), 60) if had_error else poll_interval


async def run() -> None:
    settings = get_settings()
    if settings.process_role != "worker":
        raise RuntimeError("app.worker requires PROCESS_ROLE=worker")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    logger.info("[PROCESS_ROLE] role=%s entrypoint=worker", settings.process_role)
    logger.info("[WORKER] starting role=%s", settings.process_role)
    _worker_startup()
    identity = worker_identity()
    worker_id = str(identity["worker_id"])
    queue_tasks = [
        asyncio.create_task(
            _poll_refresh_queue(
                stop_event=stop_event,
                worker_id=f"{worker_id}:{idx}",
                poll_interval=settings.parser_worker_poll_interval_seconds,
            )
        )
        for idx in range(settings.parser_worker_concurrency)
    ]
    if matching_cache_worker_enabled():
        cache_worker_id = f"{worker_id}:matching-cache"
        cache_poll_interval = float(matching_cache_worker_poll_interval_seconds())
        cache_lease_seconds = matching_cache_worker_lease_seconds()
        logger.info(
            "[MATCH_CACHE_WORKER_START] worker=%s poll_interval_sec=%s lease_sec=%s concurrency=1",
            cache_worker_id,
            cache_poll_interval,
            cache_lease_seconds,
        )
        queue_tasks.append(
            asyncio.create_task(
                _poll_matching_cache_queue(
                    stop_event=stop_event,
                    worker_id=cache_worker_id,
                    poll_interval=cache_poll_interval,
                    lease_seconds=cache_lease_seconds,
                )
            )
        )
    try:
        await stop_event.wait()
    finally:
        if queue_tasks:
            await asyncio.gather(*queue_tasks, return_exceptions=True)
        logger.info("[WORKER] shutting down")
        _worker_shutdown()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
