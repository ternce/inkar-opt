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
from backend.app.services.competitors.code_mappings import list_product_catalog_code_mappings
from backend.app.services.references.sap_balance import SapBalanceItem, SapBalancePayload
from backend.app.services.references.stock_snapshots import CanonicalStockRow, publish_stock_snapshot
from backend.app.services.references.stock_snapshots import import_sap_balance_payload, preview_sap_catalog_registration


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
    # Do not include public in search_path: CREATE TABLE IF NOT EXISTS would
    # otherwise resolve existing application tables and leak fixtures into the
    # developer database instead of this disposable schema.
    engine = create_engine(url, poolclass=NullPool, connect_args={"options": f"-csearch_path={schema}"})
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
        engine = create_engine(url, poolclass=NullPool, connect_args={"options": f"-csearch_path={schema}"})
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


def test_postgresql_approved_sap_catalog_registration_and_active_branch_scope_are_atomic(pg_engine):
    branch = "pg-approved-" + uuid.uuid4().hex
    other_branch = "pg-other-" + uuid.uuid4().hex
    material = "8" + uuid.uuid4().hex[:17]
    # Use digits only while retaining an exact 18-character SAP identity.
    material = "8" + "".join(str(int(char, 16) % 10) for char in material[1:])
    payload = SapBalancePayload(
        "1001",
        date(2026, 10, 21),
        (
            SapBalanceItem(material, "1001", "1000", "a", Decimal("1.25"), {"materialText": "PG SAP product"}),
            SapBalanceItem(material, "1001", "1000", "b", Decimal("2.75"), {"materialText": "PG SAP product"}),
        ),
        2,
    )
    with Session(pg_engine) as db:
        preview = preview_sap_catalog_registration(db, payload)
        assert len(preview.missing) == 1
        failed = import_sap_balance_payload(
            db, branch_id=branch, payload=payload, user_name="pg-test",
            manual_precedence_hours=0, approve_catalog_registration=False,
        )
        assert failed.status == "error"
        assert db.scalar(select(func.count(Product.id)).where(Product.code == material)) == 0

        success = import_sap_balance_payload(
            db, branch_id=branch, payload=payload, user_name="pg-test",
            manual_precedence_hours=0, approve_catalog_registration=True,
        )
        assert success.status == "success"
        product = db.scalar(select(Product).where(Product.code == material))
        assert product is not None
        stock = db.scalar(select(BranchStock).where(BranchStock.branch_id == branch))
        assert stock.stock == Decimal("4.0000")
        catalog = list_product_catalog_code_mappings(db=db, assortment="active", branch_id=branch)
        assert catalog["metrics"][0]["total"] == 1
        assert catalog["items"][0]["productId"] == product.id
        isolated = list_product_catalog_code_mappings(db=db, assortment="active", branch_id=other_branch)
        assert isolated["metrics"][0]["total"] == 0
