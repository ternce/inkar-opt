from __future__ import annotations

import os
import threading
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.db import Base
from backend.app.models import BranchStock, Product, ReferenceImportJob, ReferenceUpdateStatus, StockSnapshot
from backend.app.services.references.stock_snapshots import CanonicalStockRow, publish_stock_snapshot


ROOT = Path(__file__).resolve().parents[1]
UP = ROOT / "scripts" / "migrations" / "20261016_phase5_stock_snapshots.up.sql"
DOWN = ROOT / "scripts" / "migrations" / "20261016_phase5_stock_snapshots.down.sql"


def _postgres_url() -> str:
    url = os.getenv("TEST_POSTGRES_URL", "").strip()
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required")
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture(scope="module")
def pg_engine():
    url = _postgres_url()
    admin = create_engine(url, poolclass=NullPool)
    schema = "phase5_stock_" + uuid.uuid4().hex
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


def _seed(db: Session, branch_id: str, quantity: Decimal) -> tuple[Product, StockSnapshot]:
    product = db.scalar(select(Product).where(Product.code == "0000000001005213"))
    if product is None:
        product = Product(code="0000000001005213", name="Esik fixture", cost=1)
        db.add(product)
        db.flush()
    job = ReferenceImportJob(data_type="stock", branch_ids_json=f'["{branch_id}"]', filename="fixture", status="running")
    db.add(job)
    db.flush()
    snapshot = publish_stock_snapshot(
        db,
        branch_id=branch_id,
        source_type="manual",
        effective_date=date(2026, 10, 14),
        rows=[CanonicalStockRow(product.code, product.code, product.id, quantity, {})],
        import_job=job,
    )
    return product, snapshot


def test_publication_rollback_preserves_previous_active_snapshot(pg_engine):
    branch = "rollback-" + uuid.uuid4().hex
    with Session(pg_engine) as db:
        product, first = _seed(db, branch, Decimal("32"))
        db.commit()
        _product, second = _seed(db, branch, Decimal("6"))
        second_id = second.id
        db.rollback()
        status = db.scalar(select(ReferenceUpdateStatus).where(ReferenceUpdateStatus.branch_id == branch))
        assert int(status.active_stock_snapshot_id) == first.id
        assert db.get(StockSnapshot, first.id).status == "active"
        assert db.get(StockSnapshot, second_id) is None
        assert db.scalar(select(BranchStock.stock).where(BranchStock.branch_id == branch)) == Decimal("32.0000")


def test_concurrent_publications_are_atomic_and_never_mix_rows(pg_engine):
    branch = "concurrent-" + uuid.uuid4().hex
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def publish(quantity: Decimal) -> None:
        try:
            with Session(pg_engine) as db:
                product = db.scalar(select(Product).where(Product.code == "0000000001005213"))
                if product is None:
                    product = Product(code="0000000001005213", name="Esik fixture", cost=1)
                    db.add(product)
                    db.commit()
                job = ReferenceImportJob(data_type="stock", branch_ids_json=f'["{branch}"]', filename=str(quantity), status="running")
                db.add(job)
                db.flush()
                barrier.wait(timeout=10)
                publish_stock_snapshot(
                    db,
                    branch_id=branch,
                    source_type="sap",
                    effective_date=date(2026, 10, 15),
                    rows=[CanonicalStockRow(product.code, product.code, product.id, quantity, {})],
                    import_job=job,
                )
                db.commit()
        except Exception as exc:
            errors.append(exc)

    with Session(pg_engine) as db:
        if db.scalar(select(Product.id).where(Product.code == "0000000001005213")) is None:
            db.add(Product(code="0000000001005213", name="Esik fixture", cost=1))
            db.commit()
    threads = [threading.Thread(target=publish, args=(Decimal("6"),)), threading.Thread(target=publish, args=(Decimal("32"),))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors
    with Session(pg_engine) as db:
        active = db.scalars(select(StockSnapshot).where(StockSnapshot.branch_id == branch, StockSnapshot.status == "active")).all()
        assert len(active) == 1
        rows = db.scalars(select(BranchStock).where(BranchStock.branch_id == branch)).all()
        assert len(rows) == 1
        assert rows[0].snapshot_id == active[0].id
        assert rows[0].stock in {Decimal("6.0000"), Decimal("32.0000")}


def test_phase5_migration_is_additive_and_reversible():
    url = _postgres_url()
    admin = create_engine(url, poolclass=NullPool)
    schema = "phase5_migration_" + uuid.uuid4().hex
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = create_engine(url, poolclass=NullPool, connect_args={"options": f"-csearch_path={schema},public"})
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE products (id SERIAL PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE reference_import_jobs (id SERIAL PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE branch_stock (id SERIAL PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE reference_update_statuses (id SERIAL PRIMARY KEY)")
            connection.exec_driver_sql(UP.read_text(encoding="utf-8"))
        assert {"stock_snapshots", "stock_snapshot_items"} <= set(inspect(engine).get_table_names(schema=schema))
        with engine.begin() as connection:
            connection.exec_driver_sql(DOWN.read_text(encoding="utf-8"))
        assert "stock_snapshots" not in inspect(engine).get_table_names(schema=schema)
        with engine.begin() as connection:
            connection.exec_driver_sql(UP.read_text(encoding="utf-8"))
        assert {"stock_snapshots", "stock_snapshot_items"} <= set(inspect(engine).get_table_names(schema=schema))
        engine.dispose()
    finally:
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()
