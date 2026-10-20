from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.models import BranchStock, ReferenceUpdateStatus, StockSnapshot
from backend.app.services.references.stock_snapshots import CanonicalStockRow, publish_stock_snapshot


MIGRATIONS = Path(__file__).resolve().parents[1] / "scripts" / "migrations"


def test_postgres16_stock_snapshot_migration_is_idempotent_and_publishes_per_branch():
    dsn = os.getenv("STOCK_HOTFIX_POSTGRES_DSN")
    if not dsn:
        pytest.skip("STOCK_HOTFIX_POSTGRES_DSN is not configured")
    schema = f"stock_hotfix_{uuid.uuid4().hex}"
    up_sql = (MIGRATIONS / "20261016_phase5_stock_snapshots.up.sql").read_text(encoding="utf-8")
    down_sql = (MIGRATIONS / "20261016_phase5_stock_snapshots.down.sql").read_text(encoding="utf-8")

    psycopg_dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(psycopg_dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
            cursor.execute(f'SET search_path TO "{schema}"')
            cursor.execute(
                """
                CREATE TABLE products (id SERIAL PRIMARY KEY, code TEXT NOT NULL);
                CREATE TABLE reference_import_jobs (id SERIAL PRIMARY KEY);
                CREATE TABLE reference_update_statuses (
                    id SERIAL PRIMARY KEY,
                    branch_id TEXT NOT NULL DEFAULT '',
                    branch_name TEXT NOT NULL DEFAULT '',
                    data_type VARCHAR(64) NOT NULL,
                    last_updated_at TIMESTAMP,
                    rows_count INTEGER NOT NULL DEFAULT 0,
                    status VARCHAR(32) NOT NULL DEFAULT 'missing',
                    error TEXT NOT NULL DEFAULT '',
                    current_import_status VARCHAR(32) NOT NULL DEFAULT '',
                    current_import_started_at TIMESTAMP,
                    current_import_finished_at TIMESTAMP,
                    last_successful_import_job_id INTEGER,
                    active_snapshot_product_count INTEGER NOT NULL DEFAULT 0,
                    CONSTRAINT uq_reference_update_branch_type UNIQUE (branch_id, data_type)
                );
                CREATE TABLE branch_stock (
                    id SERIAL PRIMARY KEY,
                    branch_id TEXT NOT NULL,
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    sku TEXT NOT NULL,
                    stock NUMERIC(18,4),
                    source_type VARCHAR(32) NOT NULL DEFAULT 'excel',
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_branch_stock_branch_product UNIQUE (branch_id, product_id)
                );
                INSERT INTO products (id, code) VALUES (1, '0000000001005213');
                INSERT INTO reference_import_jobs (id) VALUES (1), (2);
                """
            )
            cursor.execute(up_sql)
            cursor.execute(up_sql)

    engine = create_engine(dsn, connect_args={"options": f"-csearch_path={schema}"})
    Session = sessionmaker(bind=engine)
    with Session() as db:
        first = publish_stock_snapshot(
            db,
            branch_id="Есик",
            source_type="manual",
            effective_date=date(2026, 10, 21),
            rows=[CanonicalStockRow("1005213", "0000000001005213", 1, Decimal("32"), {})],
            import_job=SimpleNamespace(id=1),
        )
        second = publish_stock_snapshot(
            db,
            branch_id="1",
            source_type="manual",
            effective_date=date(2026, 10, 21),
            rows=[CanonicalStockRow("1005213", "0000000001005213", 1, Decimal("999"), {})],
            import_job=SimpleNamespace(id=2),
        )
        db.commit()
        assert first.id != second.id
        assert db.scalar(select(BranchStock.stock).where(BranchStock.branch_id == "Есик")) == Decimal("32.0000")
        assert db.scalar(select(BranchStock.stock).where(BranchStock.branch_id == "1")) == Decimal("999.0000")
        esik_status = db.scalar(select(ReferenceUpdateStatus).where(ReferenceUpdateStatus.branch_id == "Есик"))
        assert int(esik_status.active_stock_snapshot_id) == first.id
        assert db.get(StockSnapshot, first.id).status == "active"
    engine.dispose()

    with psycopg.connect(psycopg_dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
            cursor.execute(down_sql)
            cursor.execute("SELECT to_regclass('stock_snapshots'), to_regclass('stock_snapshot_items')")
            assert cursor.fetchone() == (None, None)
            cursor.execute(
                """
                SELECT count(*)
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND (
                    (table_name = 'branch_stock' AND column_name = 'snapshot_id')
                    OR
                    (table_name = 'reference_update_statuses' AND column_name = 'active_stock_snapshot_id')
                  )
                """,
                (schema,),
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(f'DROP SCHEMA "{schema}" CASCADE')
