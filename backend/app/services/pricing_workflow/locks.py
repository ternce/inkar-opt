from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)

# PostgreSQL's two-key advisory-lock API uses signed int32 values.  Separate
# namespaces prevent a price-format id from colliding with a source id.
PRICE_FORMAT_LOCK_NAMESPACE = 0x494E4B01
COMPETITOR_SOURCE_LOCK_NAMESPACE = 0x494E4B02


@dataclass(frozen=True, order=True)
class WorkflowLockKey:
    namespace: int
    resource_id: int
    resource_type: str


class PricingWorkflowConflict(RuntimeError):
    def __init__(
        self,
        key: WorkflowLockKey,
        *,
        stage: str = "unspecified",
        waited_seconds: float = 0.0,
        backend_pid: int | None = None,
    ):
        self.key = key
        self.stage = str(stage or "unspecified")
        self.waited_seconds = max(0.0, float(waited_seconds))
        self.backend_pid = backend_pid
        pid_text = str(backend_pid) if backend_pid is not None else "unavailable"
        super().__init__(
            f"pricing workflow conflict: {key.resource_type} {key.resource_id} is currently being modified "
            f"(stage={self.stage}, waited_seconds={self.waited_seconds:.3f}, backend_pid={pid_text})"
        )


class PricingWorkflowCapacityExceeded(RuntimeError):
    pass


def _keys(*, price_format_ids: Iterable[int], source_ids: Iterable[int]) -> list[WorkflowLockKey]:
    keys = {
        WorkflowLockKey(PRICE_FORMAT_LOCK_NAMESPACE, int(item), "price_format")
        for item in price_format_ids
        if int(item) > 0
    }
    keys.update(
        WorkflowLockKey(COMPETITOR_SOURCE_LOCK_NAMESPACE, int(item), "competitor_source")
        for item in source_ids
        if int(item) > 0
    )
    return sorted(keys)


def _engine_for_session(db: Session) -> Engine:
    bind = db.get_bind()
    if isinstance(bind, Connection):
        return bind.engine
    return bind


def workflow_lock_retry_timeout_seconds() -> float:
    """Return the bounded workflow-entry wait without permitting indefinite requests."""

    raw_value = os.getenv("PRICING_WORKFLOW_LOCK_TIMEOUT_SECONDS", "5")
    try:
        return min(60.0, max(0.0, float(raw_value)))
    except (TypeError, ValueError):
        logger.warning(
            "invalid PRICING_WORKFLOW_LOCK_TIMEOUT_SECONDS=%r; using 5 seconds",
            raw_value,
        )
        return 5.0


def _backend_pid(db: Session) -> int | None:
    try:
        return int(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
    except Exception:
        return None


def _raise_conflict(
    key: WorkflowLockKey,
    *,
    stage: str,
    started_at: float,
    backend_pid: int | None,
) -> None:
    waited_seconds = max(0.0, time.monotonic() - started_at)
    logger.warning(
        "pricing workflow advisory lock conflict stage=%s resource_type=%s resource_id=%s "
        "waited_seconds=%.3f backend_pid=%s",
        stage,
        key.resource_type,
        key.resource_id,
        waited_seconds,
        backend_pid if backend_pid is not None else "unavailable",
    )
    raise PricingWorkflowConflict(
        key,
        stage=stage,
        waited_seconds=waited_seconds,
        backend_pid=backend_pid,
    )


def acquire_transaction_write_locks(
    db: Session,
    *,
    price_format_ids: Iterable[int] = (),
    source_ids: Iterable[int] = (),
    retry_timeout_seconds: float = 0.0,
    retry_interval_seconds: float = 0.1,
    stage: str = "transaction_write",
) -> None:
    """Guard a short assignment write until its transaction commits.

    This deliberately uses transaction locks for request-sized writes.  They
    conflict with a workflow's session locks but cannot leak through pooling.
    """

    if db.get_bind().dialect.name != "postgresql":
        return
    started_at = time.monotonic()
    deadline = started_at + max(0.0, float(retry_timeout_seconds))
    backend_pid: int | None = None
    for key in _keys(price_format_ids=price_format_ids, source_ids=source_ids):
        while True:
            acquired = bool(
                db.execute(
                    text("SELECT pg_try_advisory_xact_lock(:namespace, :resource_id)"),
                    {"namespace": key.namespace, "resource_id": key.resource_id},
                ).scalar()
            )
            if acquired:
                break
            if time.monotonic() >= deadline:
                backend_pid = backend_pid if backend_pid is not None else _backend_pid(db)
                _raise_conflict(
                    key,
                    stage=stage,
                    started_at=started_at,
                    backend_pid=backend_pid,
                )
            time.sleep(max(0.01, min(float(retry_interval_seconds), deadline - time.monotonic())))


class PricingWorkflowLockLease(AbstractContextManager["PricingWorkflowLockLease"]):
    """Own session advisory locks on a dedicated, never-borrowed connection.

    ORM commits may return the Session's connection to its pool.  This lease
    deliberately owns another Connection until every lock has been released,
    so intermediate workflow commits cannot silently drop or transfer locks.
    """

    def __init__(self, db: Session):
        self._engine = _engine_for_session(db)
        self._connection: Connection | None = None
        self._acquired: list[WorkflowLockKey] = []
        self._postgresql = self._engine.dialect.name == "postgresql"
        self._backend_pid: int | None = None

    @property
    def acquired_keys(self) -> tuple[WorkflowLockKey, ...]:
        return tuple(self._acquired)

    def __enter__(self) -> "PricingWorkflowLockLease":
        if self._postgresql:
            self._connection = self._engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            try:
                self._backend_pid = int(
                    self._connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
                )
            except Exception:
                self._backend_pid = None
        else:
            logger.warning(
                "pricing workflow advisory locks are unavailable for dialect=%s; PostgreSQL is required in production",
                self._engine.dialect.name,
            )
        return self

    def acquire(
        self,
        *,
        price_format_ids: Iterable[int] = (),
        source_ids: Iterable[int] = (),
        retry_timeout_seconds: float = 0.0,
        retry_interval_seconds: float = 0.1,
        stage: str = "session_lease",
    ) -> None:
        requested = _keys(price_format_ids=price_format_ids, source_ids=source_ids)
        if not self._postgresql:
            self._acquired.extend(key for key in requested if key not in self._acquired)
            return
        if self._connection is None:
            raise RuntimeError("pricing workflow lock lease is not active")
        started_at = time.monotonic()
        deadline = started_at + max(0.0, float(retry_timeout_seconds))
        for key in requested:
            if key in self._acquired:
                continue
            while True:
                acquired = bool(
                    self._connection.execute(
                        text("SELECT pg_try_advisory_lock(:namespace, :resource_id)"),
                        {"namespace": key.namespace, "resource_id": key.resource_id},
                    ).scalar()
                )
                if acquired:
                    self._acquired.append(key)
                    break
                if time.monotonic() >= deadline:
                    _raise_conflict(
                        key,
                        stage=stage,
                        started_at=started_at,
                        backend_pid=self._backend_pid,
                    )
                time.sleep(max(0.01, min(float(retry_interval_seconds), deadline - time.monotonic())))

    def release(self) -> None:
        connection = self._connection
        if connection is None:
            self._acquired.clear()
            return
        release_failed = False
        try:
            for key in reversed(self._acquired):
                released = bool(
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:namespace, :resource_id)"),
                        {"namespace": key.namespace, "resource_id": key.resource_id},
                    ).scalar()
                )
                if not released:
                    release_failed = True
                    logger.error("pricing workflow advisory lock was not owned at release: %s", key)
        except BaseException:
            release_failed = True
            raise
        finally:
            self._acquired.clear()
            try:
                if release_failed:
                    # Never return a possibly locked backend connection to the pool.
                    connection.invalidate()
            finally:
                try:
                    connection.close()
                finally:
                    self._connection = None
                    self._backend_pid = None

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


class PricingWorkflowTransactionLockLease:
    """Duck-typed lease for writers that keep one database transaction."""

    transaction_scoped = True

    def __init__(self, db: Session, *, retry_timeout_seconds: float = 0.0):
        self._db = db
        self._retry_timeout_seconds = retry_timeout_seconds

    def acquire(
        self,
        *,
        price_format_ids: Iterable[int] = (),
        source_ids: Iterable[int] = (),
        retry_timeout_seconds: float | None = None,
        retry_interval_seconds: float = 0.1,
        stage: str = "transaction_lease",
    ) -> None:
        acquire_transaction_write_locks(
            self._db,
            price_format_ids=price_format_ids,
            source_ids=source_ids,
            retry_timeout_seconds=(
                self._retry_timeout_seconds
                if retry_timeout_seconds is None
                else retry_timeout_seconds
            ),
            retry_interval_seconds=retry_interval_seconds,
            stage=stage,
        )


def _workflow_concurrency_limit(engine: Engine) -> int:
    configured = str(os.getenv("PRICING_WORKFLOW_MAX_CONCURRENCY", "")).strip()
    if configured:
        try:
            return max(1, int(configured))
        except ValueError:
            logger.warning("invalid PRICING_WORKFLOW_MAX_CONCURRENCY=%r; deriving from SQLAlchemy pool", configured)
    pool = engine.pool
    try:
        pool_size = int(pool.size())
        max_overflow = max(0, int(getattr(pool, "_max_overflow", 0)))
        # Each workflow can simultaneously consume one ORM connection and one
        # dedicated lock connection. Reserve the base pool for ordinary traffic.
        overflow_capacity = max(0, pool_size + max_overflow - pool_size)
        return max(1, overflow_capacity // 2)
    except Exception:
        return 1


_gate_lock = threading.Lock()
_gates: dict[int, tuple[int, threading.BoundedSemaphore]] = {}


class PricingWorkflowConcurrencySlot(AbstractContextManager["PricingWorkflowConcurrencySlot"]):
    def __init__(self, db: Session, *, timeout_seconds: float | None = None):
        self._engine = _engine_for_session(db)
        raw_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else os.getenv("PRICING_WORKFLOW_CAPACITY_TIMEOUT_SECONDS", "5")
        )
        try:
            self._timeout = max(0.0, float(raw_timeout))
        except (TypeError, ValueError):
            logger.warning(
                "invalid PRICING_WORKFLOW_CAPACITY_TIMEOUT_SECONDS=%r; using 5 seconds",
                raw_timeout,
            )
            self._timeout = 5.0
        self._semaphore: threading.BoundedSemaphore | None = None
        self._acquired = False

    def __enter__(self) -> "PricingWorkflowConcurrencySlot":
        if self._engine.dialect.name != "postgresql":
            return self
        key = id(self._engine)
        limit = _workflow_concurrency_limit(self._engine)
        with _gate_lock:
            current = _gates.get(key)
            if current is None or current[0] != limit:
                current = (limit, threading.BoundedSemaphore(limit))
                _gates[key] = current
            self._semaphore = current[1]
        self._acquired = self._semaphore.acquire(timeout=self._timeout)
        if not self._acquired:
            raise PricingWorkflowCapacityExceeded(
                f"pricing workflow capacity is busy (limit={limit}); retry later"
            )
        return self

    def release(self) -> None:
        if self._acquired and self._semaphore is not None:
            self._semaphore.release()
        self._acquired = False

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()
