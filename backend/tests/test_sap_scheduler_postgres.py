from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, inspect, select, text, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from backend.app.db import Base
import backend.app.main as main
from backend.app.models import Product, ReferenceUpdateStatus, RefreshJob, RefreshLock, SapBalanceScheduledRun
from backend.app.services.provisor_auto_refresh import (
    GLOBAL_REFRESH_LOCK_NAME,
    release_sap_scheduler_lock,
    try_acquire_sap_scheduler_lock,
)
from backend.app.services.refresh_queue import (
    claim_next_queued_refresh_job,
    create_queued_sap_balance_job,
    create_scheduled_sap_balance_job,
    recover_stale_parser_jobs,
)
from backend.app.services.references.sap_balance import parse_balance_response
from backend.app.services.sku import normalize_sku


ROOT = Path(__file__).resolve().parents[1]
STOCK_UP = ROOT / "scripts" / "migrations" / "20261016_phase5_stock_snapshots.up.sql"
STOCK_DOWN = ROOT / "scripts" / "migrations" / "20261016_phase5_stock_snapshots.down.sql"
SCHEDULER_UP = ROOT / "scripts" / "migrations" / "20261018_phase5_sap_scheduler_dedup.up.sql"
SCHEDULER_DOWN = ROOT / "scripts" / "migrations" / "20261018_phase5_sap_scheduler_dedup.down.sql"


def _postgres_url() -> str:
    url = os.getenv("TEST_POSTGRES_URL", "").strip()
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required")
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture(scope="module")
def pg_engine():
    url = _postgres_url()
    admin = create_engine(url, poolclass=NullPool)
    schema = "phase5_scheduler_" + uuid.uuid4().hex
    with admin.begin() as connection:
        version = int(connection.scalar(text("SHOW server_version_num")))
        if not 160000 <= version < 170000:
            pytest.skip("PostgreSQL 16 required")
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(url, poolclass=NullPool, connect_args={"options": f"-csearch_path={schema},public"})
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


@pytest.fixture(autouse=True)
def clean_refresh_state(pg_engine):
    with pg_engine.begin() as connection:
        connection.execute(text("DELETE FROM sap_balance_scheduled_runs"))
        connection.execute(text("DELETE FROM refresh_locks"))
        connection.execute(text("DELETE FROM refresh_jobs"))
    yield
    with pg_engine.begin() as connection:
        connection.execute(text("DELETE FROM sap_balance_scheduled_runs"))
        connection.execute(text("DELETE FROM refresh_locks"))
        connection.execute(text("DELETE FROM refresh_jobs"))


def _targets() -> list[tuple[str, str]]:
    return [("1001", "1"), ("1004", "Esik"), ("1013", "17")]


def test_phase5_scheduler_migration_follows_stock_snapshot_migration_and_is_reversible():
    url = _postgres_url()
    admin = create_engine(url, poolclass=NullPool)
    schema = "phase5_scheduler_migration_" + uuid.uuid4().hex
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = create_engine(url, poolclass=NullPool, connect_args={"options": f"-csearch_path={schema},public"})
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE products (id SERIAL PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE reference_import_jobs (id SERIAL PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE refresh_jobs (id SERIAL PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE branch_stock (id SERIAL PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE reference_update_statuses (id SERIAL PRIMARY KEY)")
            connection.exec_driver_sql(STOCK_UP.read_text(encoding="utf-8"))
            connection.exec_driver_sql(SCHEDULER_UP.read_text(encoding="utf-8"))
            connection.exec_driver_sql(SCHEDULER_UP.read_text(encoding="utf-8"))
        assert "stock_snapshots" in inspect(engine).get_table_names(schema=schema)
        assert "sap_balance_scheduled_runs" in inspect(engine).get_table_names(schema=schema)
        with engine.begin() as connection:
            connection.exec_driver_sql(SCHEDULER_DOWN.read_text(encoding="utf-8"))
            connection.exec_driver_sql(STOCK_DOWN.read_text(encoding="utf-8"))
        assert "sap_balance_scheduled_runs" not in inspect(engine).get_table_names(schema=schema)
        assert "stock_snapshots" not in inspect(engine).get_table_names(schema=schema)
        engine.dispose()
    finally:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


def test_two_scheduler_instances_create_one_job_and_one_reservation_per_branch(pg_engine):
    business_date = date(2031, 1, 1)
    barrier = threading.Barrier(2)
    job_ids: list[int | None] = []
    errors: list[Exception] = []

    def enqueue() -> None:
        try:
            with Session(pg_engine) as db:
                barrier.wait(timeout=10)
                job = create_scheduled_sap_balance_job(
                    db,
                    plant_branches=_targets(),
                    business_date=business_date,
                )
                job_ids.append(int(job.id) if job is not None else None)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=enqueue), threading.Thread(target=enqueue)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors
    assert len([job_id for job_id in job_ids if job_id is not None]) == 1
    with Session(pg_engine) as db:
        reservations = db.scalars(
            select(SapBalanceScheduledRun).where(SapBalanceScheduledRun.business_date == business_date)
        ).all()
        assert {(row.plant, row.branch_id) for row in reservations} == set(_targets())
        assert len({row.refresh_job_id for row in reservations}) == 1


def test_database_scheduler_lease_has_single_owner_and_can_be_released(pg_engine):
    barrier = threading.Barrier(2)
    results: list[tuple[str, bool]] = []

    def acquire(owner: str) -> None:
        with Session(pg_engine) as db:
            barrier.wait(timeout=10)
            results.append((owner, try_acquire_sap_scheduler_lock(db, owner_token=owner)))

    threads = [threading.Thread(target=acquire, args=("scheduler-a",)), threading.Thread(target=acquire, args=("scheduler-b",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    winners = [owner for owner, acquired in results if acquired]
    assert len(winners) == 1
    with Session(pg_engine) as db:
        assert release_sap_scheduler_lock(db, owner_token=winners[0]) is True
        assert try_acquire_sap_scheduler_lock(db, owner_token="scheduler-after-release") is True
        assert release_sap_scheduler_lock(db, owner_token="scheduler-after-release") is True


def test_two_workers_claim_scheduled_sap_job_exactly_once_and_winner_completes(pg_engine, monkeypatch):
    business_date = date(2031, 1, 2)
    with Session(pg_engine) as db:
        product_code = normalize_sku("0000000001005213")
        if db.scalar(select(Product.id).where(Product.code == product_code)) is None:
            db.add(Product(code=product_code, name="SAP worker fixture", cost=1))
            db.commit()
        queued = create_scheduled_sap_balance_job(
            db,
            plant_branches=[("1004", "Esik")],
            business_date=business_date,
        )
        assert queued is not None
        queued_id = int(queued.id)

    barrier = threading.Barrier(2)
    claims: list[tuple[int | None, str | None]] = []

    def claim(worker_id: str) -> None:
        with Session(pg_engine) as db:
            barrier.wait(timeout=10)
            job, token = claim_next_queued_refresh_job(db, worker_id=worker_id, sources=("sap",))
            claims.append((int(job.id) if job is not None else None, token))

    threads = [threading.Thread(target=claim, args=("worker-a",)), threading.Thread(target=claim, args=("worker-b",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    claimed = [(job_id, token) for job_id, token in claims if job_id is not None]
    assert len(claimed) == 1
    assert claimed[0][0] == queued_id
    assert claimed[0][1]

    async def fake_fetch_balance(_settings, *, plant, date_from, date_to):
        return parse_balance_response(
            {
                "MT_BalanceResponse": {
                    "status": "SUCCESS",
                    "rowCount": 1,
                    "items": [
                        {
                            "material": "0000000001005213",
                            "plant": plant,
                            "lgort": "1000",
                            "charge": "",
                            "endingStockQuantity": "32",
                        }
                    ],
                }
            },
            requested_plant=plant,
            effective_date=date_to,
        )

    SessionFactory = sessionmaker(bind=pg_engine)
    monkeypatch.setattr(main, "SessionLocal", SessionFactory)
    monkeypatch.setattr(main, "fetch_balance", fake_fetch_balance)
    monkeypatch.setattr(
        main,
        "settings",
        main.settings.model_copy(update={"sap_balance_enabled": True, "sap_manual_precedence_hours": 0}),
    )
    asyncio.run(main._execute_claimed_refresh_job(queued_id, owner_token=str(claimed[0][1])))
    with Session(pg_engine) as db:
        assert db.get(RefreshJob, queued_id).status == "success"
        assert db.get(RefreshLock, GLOBAL_REFRESH_LOCK_NAME) is None
        status = db.scalar(
            select(ReferenceUpdateStatus).where(
                ReferenceUpdateStatus.data_type == "stock",
                ReferenceUpdateStatus.active_source_type == "sap",
            )
        )
        assert status is not None and status.active_stock_snapshot_id is not None


def test_manual_retry_bypasses_scheduled_reservation(pg_engine):
    business_date = date(2031, 1, 3)
    with Session(pg_engine) as db:
        scheduled = create_scheduled_sap_balance_job(
            db,
            plant_branches=[("1004", "Esik")],
            business_date=business_date,
        )
        manual = create_queued_sap_balance_job(
            db,
            plants=["1004"],
            effective_date=business_date.isoformat(),
            requested_by="manual-retry",
        )
        assert scheduled is not None and manual.id != scheduled.id
        assert db.scalar(
            select(func.count(SapBalanceScheduledRun.id)).where(
                SapBalanceScheduledRun.business_date == business_date
            )
        ) == 1


def test_crashed_sap_job_releases_global_mutex_and_manual_retry_is_claimable(pg_engine):
    business_date = date(2031, 1, 4)
    with Session(pg_engine) as db:
        queued = create_scheduled_sap_balance_job(
            db,
            plant_branches=[("1004", "Esik")],
            business_date=business_date,
        )
        assert queued is not None
        job, token = claim_next_queued_refresh_job(db, worker_id="crashed-worker", sources=("sap",))
        assert job is not None and token
        job.heartbeat_at = datetime.utcnow() - timedelta(hours=1)
        db.commit()
        recovered = recover_stale_parser_jobs(db, stale_heartbeat_seconds=30)
        assert [row.id for row in recovered] == [job.id]
        assert db.get(RefreshJob, job.id).status == "interrupted"
        assert db.get(RefreshLock, GLOBAL_REFRESH_LOCK_NAME) is None
        retry = create_queued_sap_balance_job(
            db,
            plants=["1004"],
            effective_date=business_date.isoformat(),
            requested_by="manual-recovery",
        )
        claimed, retry_token = claim_next_queued_refresh_job(db, worker_id="recovery-worker", sources=("sap",))
        assert claimed is not None and claimed.id == retry.id and retry_token
        db.execute(update(RefreshJob).where(RefreshJob.id == retry.id).values(status="success"))
        db.execute(text("DELETE FROM refresh_locks"))
        db.commit()
