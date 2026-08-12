from __future__ import annotations

import asyncio
import logging
import signal

from .config import get_settings
from .db import SessionLocal
from .main import _execute_claimed_refresh_job, _worker_shutdown, _worker_startup
from .services.refresh_queue import claim_next_queued_refresh_job, worker_identity

logger = logging.getLogger(__name__)


async def _poll_refresh_queue(*, stop_event: asyncio.Event, worker_id: str, poll_interval: float) -> None:
    retry_delay = poll_interval
    while not stop_event.is_set():
        claimed_id: int | None = None
        owner_token: str | None = None
        had_error = False
        try:
            with SessionLocal() as db:
                job, token = claim_next_queued_refresh_job(db, worker_id=worker_id)
                if job is not None and token:
                    claimed_id = int(job.id)
                    owner_token = token
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
