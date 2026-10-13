from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.services.pricing_workflow.locks import (
    PricingWorkflowConflict,
    PricingWorkflowLockLease,
    acquire_transaction_write_locks,
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
