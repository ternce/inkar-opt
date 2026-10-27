from __future__ import annotations

import os
import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.db import Base
from backend.app.services import pricing as pricing_module
from backend.app.services.pricing_workflow import workflow as workflow_module
from backend.app.services.pricing_workflow.locks import (
    PRICE_FORMAT_LOCK_NAMESPACE,
    PricingWorkflowConflict,
    PricingWorkflowLockLease,
    WorkflowLockKey,
    acquire_transaction_write_locks,
)
from backend.tests.test_pricing_engine_targeted_fixes import (
    _activate_all_products_for_generation,
    _format,
    _product,
    _workflow_context,
    _workflow_payload,
    _workflow_price_list_source,
)


@pytest.fixture(scope="module")
def pg_engine():
    url = os.getenv("TEST_POSTGRES_URL", "").strip()
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required; SQLite cannot verify PostgreSQL advisory locks")
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except Exception as exc:
        engine.dispose()
        pytest.skip(f"TEST_POSTGRES_URL is unavailable: {exc}")
    yield engine
    engine.dispose()


def test_same_format_and_assignment_write_conflict_across_intermediate_commit(pg_engine):
    with Session(pg_engine) as workflow_db, Session(pg_engine) as writer_db:
        with PricingWorkflowLockLease(workflow_db) as workflow_lock:
            workflow_lock.acquire(price_format_ids=[910001], source_ids=[920001])
            workflow_db.commit()  # must not release the dedicated connection's lock
            with PricingWorkflowLockLease(writer_db) as competing_lock:
                with pytest.raises(PricingWorkflowConflict, match="price_format 910001"):
                    competing_lock.acquire(price_format_ids=[910001])
            with pytest.raises(PricingWorkflowConflict, match="price_format 910001"):
                acquire_transaction_write_locks(writer_db, price_format_ids=[910001])
            writer_db.rollback()


def test_different_formats_sharing_source_conflict_but_independent_sources_do_not(pg_engine):
    with Session(pg_engine) as first_db, Session(pg_engine) as second_db:
        with PricingWorkflowLockLease(first_db) as first:
            first.acquire(price_format_ids=[910002], source_ids=[920002])
            with PricingWorkflowLockLease(second_db) as second:
                second.acquire(price_format_ids=[910003])
                with pytest.raises(PricingWorkflowConflict, match="competitor_source 920002"):
                    second.acquire(source_ids=[920002])
                second.acquire(source_ids=[920003])


def test_lock_cleanup_after_exception_allows_retry(pg_engine):
    with Session(pg_engine) as first_db:
        with pytest.raises(TimeoutError):
            with PricingWorkflowLockLease(first_db) as first:
                first.acquire(price_format_ids=[910004], source_ids=[920004])
                raise TimeoutError("simulated calculation timeout")

    with Session(pg_engine) as retry_db:
        with PricingWorkflowLockLease(retry_db) as retry:
            retry.acquire(price_format_ids=[910004], source_ids=[920004])


def test_source_writer_conflicts_with_active_workflow(pg_engine):
    with Session(pg_engine) as workflow_db, Session(pg_engine) as writer_db:
        with PricingWorkflowLockLease(workflow_db) as workflow:
            workflow.acquire(price_format_ids=[910005], source_ids=[920005])
            with pytest.raises(PricingWorkflowConflict, match="competitor_source 920005"):
                acquire_transaction_write_locks(writer_db, source_ids=[920005])
            writer_db.rollback()


def test_format_configuration_writer_conflicts_with_active_workflow(pg_engine):
    with Session(pg_engine) as workflow_db, Session(pg_engine) as writer_db:
        with PricingWorkflowLockLease(workflow_db) as workflow:
            workflow.acquire(price_format_ids=[910006])
            with pytest.raises(PricingWorkflowConflict, match="price_format 910006"):
                acquire_transaction_write_locks(writer_db, price_format_ids=[910006])
            writer_db.rollback()


def test_lock_cleanup_after_cancellation_base_exception(pg_engine):
    with Session(pg_engine) as first_db:
        with pytest.raises(KeyboardInterrupt):
            with PricingWorkflowLockLease(first_db) as first:
                first.acquire(price_format_ids=[910007], source_ids=[920007])
                raise KeyboardInterrupt()

    with Session(pg_engine) as retry_db:
        with PricingWorkflowLockLease(retry_db) as retry:
            retry.acquire(price_format_ids=[910007], source_ids=[920007])


def test_concurrent_same_format_waits_for_successful_generation_cleanup(pg_engine):
    locked = threading.Event()
    release = threading.Event()

    def hold_generation_lock():
        with Session(pg_engine) as first_db:
            with PricingWorkflowLockLease(first_db) as first:
                first.acquire(price_format_ids=[910008])
                locked.set()
                assert release.wait(timeout=2.0)

    thread = threading.Thread(target=hold_generation_lock)
    thread.start()
    assert locked.wait(timeout=2.0)
    try:
        with Session(pg_engine) as second_db:
            with PricingWorkflowLockLease(second_db) as second:
                timer = threading.Timer(0.15, release.set)
                timer.start()
                started_at = time.monotonic()
                second.acquire(
                    price_format_ids=[910008],
                    retry_timeout_seconds=1.0,
                    retry_interval_seconds=0.02,
                    stage="workflow_initial_lock",
                )
                assert time.monotonic() - started_at >= 0.1
                timer.join(timeout=1.0)
    finally:
        release.set()
        thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_configuration_update_waits_for_generation_then_acquires(pg_engine):
    with Session(pg_engine) as workflow_db, Session(pg_engine) as writer_db:
        with PricingWorkflowLockLease(workflow_db) as workflow:
            workflow.acquire(price_format_ids=[910009])
            timer = threading.Timer(0.15, workflow.release)
            timer.start()
            acquire_transaction_write_locks(
                writer_db,
                price_format_ids=[910009],
                retry_timeout_seconds=1.0,
                retry_interval_seconds=0.02,
                stage="configuration_update",
            )
            timer.join(timeout=1.0)
            writer_db.rollback()


def test_source_update_waits_for_overlapping_generation_then_acquires(pg_engine):
    with Session(pg_engine) as workflow_db, Session(pg_engine) as writer_db:
        with PricingWorkflowLockLease(workflow_db) as workflow:
            workflow.acquire(source_ids=[920010])
            timer = threading.Timer(0.15, workflow.release)
            timer.start()
            acquire_transaction_write_locks(
                writer_db,
                source_ids=[920010],
                retry_timeout_seconds=1.0,
                retry_interval_seconds=0.02,
                stage="source_update",
            )
            timer.join(timeout=1.0)
            writer_db.rollback()


def test_conflict_diagnostics_and_partial_acquisition_cleanup(pg_engine):
    with Session(pg_engine) as blocker_db, Session(pg_engine) as contender_db:
        with PricingWorkflowLockLease(blocker_db) as blocker:
            blocker.acquire(source_ids=[920012])
            with PricingWorkflowLockLease(contender_db) as contender:
                with pytest.raises(PricingWorkflowConflict) as caught:
                    contender.acquire(
                        source_ids=[920011, 920012],
                        retry_timeout_seconds=0.05,
                        retry_interval_seconds=0.01,
                        stage="workflow_initial_lock",
                    )
                assert caught.value.stage == "workflow_initial_lock"
                assert caught.value.key.resource_id == 920012
                assert caught.value.waited_seconds >= 0.04
                assert caught.value.backend_pid is not None

    with Session(pg_engine) as retry_db:
        with PricingWorkflowLockLease(retry_db) as retry:
            retry.acquire(source_ids=[920011, 920012])


def test_zero_materialized_rows_fallback_rebuild_does_not_self_conflict(
    pg_engine,
    monkeypatch,
):
    Base.metadata.create_all(pg_engine)
    monkeypatch.setenv("MATCH_CACHE_ENABLED", "false")
    monkeypatch.setenv("MATCH_CACHE_READ", "false")

    with Session(pg_engine) as db:
        pf = _format(db, price_format_id=930001)
        product = _product(db, code="LOCK-FALLBACK-PG", cost=100)
        context = _workflow_context(db, branch_id="lock-fallback")
        source = _workflow_price_list_source(db, pf, product=product, price=150)
        source.source_type = "manual"
        _activate_all_products_for_generation(db, branch_id="lock-fallback")
        payload = _workflow_payload(
            context,
            pf,
            source,
            percentile_sources=[],
            price_list_number="LOCK-FALLBACK-PG-PL",
        )

        # Reproduce production's zero-row state after the workflow matching
        # stage so calculate_prices() must execute its defensive rebuild.
        monkeypatch.setattr(
            workflow_module,
            "rebuild_competitor_prices_for_selected",
            lambda **_kwargs: {"_stageTimings": []},
        )
        fallback_leases = []
        original_fallback_rebuild = pricing_module.rebuild_competitor_prices_for_selected

        def capture_fallback_lease(**kwargs):
            lease = kwargs.get("lock_lease")
            fallback_leases.append((lease, tuple(lease.acquired_keys) if lease is not None else ()))
            return original_fallback_rebuild(**kwargs)

        monkeypatch.setattr(
            pricing_module,
            "rebuild_competitor_prices_for_selected",
            capture_fallback_lease,
        )

        run = workflow_module.create_workflow_run(db=db, payload=payload)

        assert len(fallback_leases) == 1
        assert fallback_leases[0][0] is not None
        assert WorkflowLockKey(
            PRICE_FORMAT_LOCK_NAMESPACE,
            int(pf.id),
            "price_format",
        ) in fallback_leases[0][1]
        assert run.status != "conflict"
        assert "pricing workflow conflict" not in str(run.error or "")
