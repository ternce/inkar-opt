from __future__ import annotations

import os
from pathlib import Path
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.db import Base
from backend.app.models import (
    CompetitorMatchDependencyRevision,
    CompetitorPriceList,
    CompetitorPriceListItem,
    PriceFormat,
    Product,
)
from backend.scripts.benchmark_matching_cache import run as run_benchmark
from backend.app.services.matching_cache.dependencies import MANUAL_CODE_MAPPINGS, PRODUCT_CATALOG
from backend.app.services.matching_cache.dependencies import (
    PROVISOR_REFERENCES,
    PROVISOR_SUBSTITUTES,
    VIDMAN_AUTHORITATIVE,
    build_dependency_vector,
)
from backend.app.services.matching_cache.dependency_tracking import assert_database_dependency_tracking
from backend.app.services.matching_cache.capture import capture_and_publish_legacy_source_version
from backend.app.services.matching_cache.shadow import build_shadow_snapshot, get_or_create_shadow_snapshot


MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "backend/scripts/migrations/20261014_phase2b3_dependency_triggers.up.sql"
)


def _postgres_url() -> str:
    url = os.getenv("TEST_POSTGRES_URL", "").strip()
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required")
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


@pytest.fixture(scope="module")
def trigger_engine():
    url = _postgres_url()
    admin = create_engine(url, poolclass=NullPool)
    schema = f"phase2b3_triggers_{uuid.uuid4().hex}"
    with admin.begin() as connection:
        version = int(connection.scalar(text("SHOW server_version_num")))
        if not (160000 <= version < 170000):
            pytest.skip("PostgreSQL 16 required")
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(
        url,
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={schema},public"},
    )
    try:
        Base.metadata.create_all(engine)
        raw = engine.raw_connection()
        try:
            raw.execute(MIGRATION.read_text(encoding="utf-8"))
            raw.commit()
        finally:
            raw.close()
        engine._phase2b3_benchmark_url = engine.url.set(  # type: ignore[attr-defined]
            query={"options": f"-csearch_path={schema},public"}
        ).render_as_string(
            hide_password=False
        )
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


def _revision(db: Session, scope: str) -> int:
    return int(db.scalar(select(CompetitorMatchDependencyRevision.revision).where(
        CompetitorMatchDependencyRevision.scope_type == scope,
        CompetitorMatchDependencyRevision.scope_key == "global",
    )) or 0)


def test_raw_sql_and_empty_rebuild_are_transactionally_invalidating(trigger_engine):
    token = uuid.uuid4().hex
    with Session(trigger_engine) as db:
        assert_database_dependency_tracking(db)
        before = _revision(db, PRODUCT_CATALOG)
        db.execute(
            text("INSERT INTO products (code, name, cost, created_at) VALUES (:code, 'raw', 1, CURRENT_TIMESTAMP)"),
            {"code": f"RAW-{token}"},
        )
        # A statement trigger also covers an empty DELETE in a truncate/rebuild
        # workflow.  Per-transaction de-duplication keeps this to one revision.
        db.execute(text("DELETE FROM internal_product_normalized WHERE FALSE"))
        db.commit()
    with Session(trigger_engine) as db:
        assert _revision(db, PRODUCT_CATALOG) == before + 1


def test_approved_source_goods_invalidates_but_auto_learning_does_not(trigger_engine):
    token = uuid.uuid4().hex
    with Session(trigger_engine) as db:
        product = Product(code=f"TRIGGER-{token}", name="Trigger", cost=1)
        db.add(product)
        db.commit()
        product_id = int(product.id)
    with Session(trigger_engine) as db:
        before = _revision(db, MANUAL_CODE_MAPPINGS)
        db.execute(text(
            """INSERT INTO source_goods_matches
               (source_type, distributor_goods_id, distributor_goods_name,
                distributor_producer, product_id, match_method, created_at, updated_at)
               VALUES ('provisor', :key, '', '', :product, 'auto_exact', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""
        ), {"key": f"AUTO-{token}", "product": product_id})
        db.commit()
    with Session(trigger_engine) as db:
        assert _revision(db, MANUAL_CODE_MAPPINGS) == before
        db.execute(text(
            "UPDATE source_goods_matches SET match_method='manual_approved' WHERE distributor_goods_id=:key"
        ), {"key": f"AUTO-{token}"})
        db.commit()
    with Session(trigger_engine) as db:
        assert _revision(db, MANUAL_CODE_MAPPINGS) == before + 1


def test_trigger_revision_rolls_back_with_writer_transaction(trigger_engine):
    with Session(trigger_engine) as db:
        before = _revision(db, PRODUCT_CATALOG)
        db.execute(text("UPDATE products SET name=name WHERE FALSE"))
        assert _revision(db, PRODUCT_CATALOG) == before + 1
        db.rollback()
    with Session(trigger_engine) as db:
        assert _revision(db, PRODUCT_CATALOG) == before


def test_benchmark_harness_runs_non_destructively_on_small_fixture(trigger_engine):
    token = uuid.uuid4().hex
    with Session(trigger_engine) as db:
        price_format = PriceFormat(code=f"BENCH-{token}", name="Benchmark", branch="test")
        db.add(price_format)
        db.flush()
        source = CompetitorPriceList(
            price_format_id=price_format.id,
            source_type="manual",
            source_key=f"benchmark:{token}",
            display_name="Benchmark source",
        )
        db.add(source)
        db.flush()
        source_id = int(source.id)
        db.add(CompetitorPriceListItem(
            price_list_id=source_id,
            name="Unmatched benchmark item",
            distributor_goods_name="Unmatched benchmark item",
            distributor_goods_id=f"BENCH-ITEM-{token}",
            distributor_price=Decimal("10"),
            match_type="unmatched",
            raw_json="{}",
        ))
        db.commit()

    report = run_benchmark(
        database_url=trigger_engine._phase2b3_benchmark_url,  # type: ignore[attr-defined]
        source_id=source_id,
        expected_rows=1,
        commit=False,
    )
    assert report["sourceRows"] == 1
    assert report["persistedCounts"] == {"sourceVersionItems": 1, "snapshotItems": 1}
    assert report["committedCacheArtifacts"] is False


@pytest.mark.parametrize(
    ("table_name", "identity_column", "scopes"),
    [
        ("products", "id", (PRODUCT_CATALOG, PROVISOR_REFERENCES)),
        ("product_extras", "product_id", (PRODUCT_CATALOG,)),
        ("internal_product_normalized", "id", (PRODUCT_CATALOG,)),
        ("competitor_code_mappings", "id", (MANUAL_CODE_MAPPINGS,)),
        ("provisor_goods_map", "id", (PROVISOR_REFERENCES,)),
        ("product_substitute_matches", "id", (PROVISOR_SUBSTITUTES,)),
        ("vidman_product_matches", "id", (VIDMAN_AUTHORITATIVE,)),
    ],
)
def test_every_statement_trigger_invalidates_its_declared_scopes_once(
    trigger_engine, table_name, identity_column, scopes
):
    with Session(trigger_engine) as db:
        before = {scope: _revision(db, scope) for scope in scopes}
        # Empty INSERT/UPDATE/DELETE statements model both an empty rebuild and
        # the statement-level trigger contract without needing table-specific
        # fixture rows.  A replacement's delete+insert pair must still bump
        # each dependency scope only once in the transaction.
        db.execute(text(f'INSERT INTO "{table_name}" SELECT * FROM "{table_name}" WHERE FALSE'))
        db.execute(text(
            f'UPDATE "{table_name}" SET "{identity_column}"="{identity_column}" WHERE FALSE'
        ))
        db.execute(text(f'DELETE FROM "{table_name}" WHERE FALSE'))
        db.commit()
    with Session(trigger_engine) as db:
        assert {scope: _revision(db, scope) for scope in scopes} == {
            scope: value + 1 for scope, value in before.items()
        }


def test_orm_and_database_trigger_do_not_double_increment(trigger_engine, monkeypatch):
    monkeypatch.setenv("MATCH_CACHE_ENABLED", "true")
    monkeypatch.setenv("MATCH_CACHE_DUAL_WRITE", "true")
    with Session(trigger_engine) as db:
        before_catalog = _revision(db, PRODUCT_CATALOG)
        before_reference = _revision(db, PROVISOR_REFERENCES)
        db.add(Product(code=f"ORM-ONCE-{uuid.uuid4().hex}", name="ORM once", cost=1))
        db.commit()
    with Session(trigger_engine) as db:
        assert _revision(db, PRODUCT_CATALOG) == before_catalog + 1
        assert _revision(db, PROVISOR_REFERENCES) == before_reference + 1


def test_relevant_trigger_change_forces_new_snapshot_vector(
    trigger_engine, monkeypatch
):
    monkeypatch.setenv("MATCH_CACHE_ENABLED", "true")
    monkeypatch.setenv("MATCH_CACHE_DUAL_WRITE", "true")
    monkeypatch.setenv("MATCH_CACHE_SHADOW_ENABLED", "false")
    token = uuid.uuid4().hex
    with Session(trigger_engine) as db:
        price_format = PriceFormat(code=f"STALE-{token}", name="Stale", branch="test")
        product = Product(code=f"STALE-SKU-{token}", name="Stale product", cost=1)
        db.add_all([price_format, product])
        db.flush()
        source = CompetitorPriceList(
            price_format_id=price_format.id,
            source_type="manual",
            source_key=f"stale:{token}",
            display_name="Stale source",
        )
        db.add(source)
        db.flush()
        db.add(CompetitorPriceListItem(
            price_list_id=source.id,
            name=product.name,
            distributor_goods_name=product.name,
            distributor_goods_id=product.code,
            distributor_price=Decimal("10"),
            match_type="unmatched",
            raw_json="{}",
        ))
        db.flush()
        capture = capture_and_publish_legacy_source_version(
            db=db, competitor_price_list_id=int(source.id)
        )
        vector_before = build_dependency_vector(db=db)
        snapshot, _ = get_or_create_shadow_snapshot(
            db=db, source_version_id=int(capture.version_id), vector=vector_before
        )
        snapshot_id = int(snapshot.id)
        build_shadow_snapshot(
            db=db,
            snapshot_id=snapshot_id,
            expected_dependency_fingerprint=vector_before.fingerprint,
        )
        db.commit()

    with Session(trigger_engine) as db:
        db.execute(text("UPDATE product_extras SET manufacturer=manufacturer WHERE FALSE"))
        db.commit()
    with Session(trigger_engine) as db:
        vector_after = build_dependency_vector(db=db)
        assert vector_after.fingerprint != vector_before.fingerprint
        replacement, reused = get_or_create_shadow_snapshot(
            db=db,
            source_version_id=int(capture.version_id),
            vector=vector_after,
        )
        assert not reused
        assert int(replacement.id) != snapshot_id
        assert replacement.status == "pending"
        db.rollback()
