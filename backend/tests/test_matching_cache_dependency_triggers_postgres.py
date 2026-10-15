from __future__ import annotations

import os
import json
from pathlib import Path
from decimal import Decimal
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.db import Base
from backend.app.models import (
    CompetitorMatchDependencyRevision,
    CompetitorPriceList,
    CompetitorPriceListItem,
    CompetitorSourcePublication,
    CompetitorSourceVersion,
    PriceFormat,
    Product,
    ProductExtra,
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
from backend.app.services.matching_cache.source_noop import (
    PROVIDER_SOURCE_FIELDS,
    source_semantic_metadata,
    validate_unchanged_source_import,
)
from backend.app.services.competitor_price_lists import upsert_provisor_price_list
from backend.app.services.manual_price_list_import import import_manual_price_list
from backend.app.services.emit_worker import (
    EmitConfig,
    parse_normalize_stage,
    replace_emit_price_list_from_staging,
)


MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "backend/scripts/migrations/20261014_phase2b3_dependency_triggers.up.sql"
)
PHASE3B_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "backend/scripts/migrations/20261015_phase3b_safe_write_invalidation.up.sql"
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
            raw.execute(PHASE3B_MIGRATION.read_text(encoding="utf-8"))
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
    token = uuid.uuid4().hex
    with Session(trigger_engine) as db:
        product = Product(code=f"ROLLBACK-{token}", name="Before", cost=1)
        db.add(product)
        db.commit()
        product_id = int(product.id)
    with Session(trigger_engine) as db:
        before = _revision(db, PRODUCT_CATALOG)
        db.execute(text("UPDATE products SET name='After' WHERE id=:id"), {"id": product_id})
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
        product_id = int(product.id)
        db.commit()

    with Session(trigger_engine) as db:
        db.execute(
            text("UPDATE products SET name=name || ' after' WHERE id=:id"),
            {"id": product_id},
        )
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


def test_product_and_extra_updates_are_field_sensitive(trigger_engine):
    token = uuid.uuid4().hex
    with Session(trigger_engine) as db:
        product = Product(code=f"FIELDS-{token}", name="Before", cost=1)
        db.add(product)
        db.flush()
        db.add(ProductExtra(product_id=product.id, manufacturer="Maker", stock=2))
        db.commit()
        product_id = int(product.id)

    with Session(trigger_engine) as db:
        before_catalog = _revision(db, PRODUCT_CATALOG)
        before_reference = _revision(db, PROVISOR_REFERENCES)
        db.execute(text("UPDATE products SET cost=cost+1 WHERE id=:id"), {"id": product_id})
        db.execute(text("UPDATE product_extras SET stock=stock+1, updated_at=CURRENT_TIMESTAMP WHERE product_id=:id"), {"id": product_id})
        db.commit()
    with Session(trigger_engine) as db:
        assert _revision(db, PRODUCT_CATALOG) == before_catalog
        assert _revision(db, PROVISOR_REFERENCES) == before_reference
        db.execute(text("UPDATE products SET name=name || ' changed' WHERE id=:id"), {"id": product_id})
        db.execute(text("UPDATE product_extras SET manufacturer=manufacturer || ' changed' WHERE product_id=:id"), {"id": product_id})
        db.commit()
    with Session(trigger_engine) as db:
        assert _revision(db, PRODUCT_CATALOG) == before_catalog + 1
        assert _revision(db, PROVISOR_REFERENCES) == before_reference


def test_source_row_update_invalidates_only_affected_publication_and_rolls_back(
    trigger_engine, monkeypatch
):
    monkeypatch.setenv("MATCH_CACHE_ENABLED", "true")
    monkeypatch.setenv("MATCH_CACHE_DUAL_WRITE", "true")
    token = uuid.uuid4().hex
    with Session(trigger_engine) as db:
        sources = []
        for suffix in ("a", "b"):
            source = CompetitorPriceList(
                source_type="manual",
                source_key=f"update-invalidation:{token}:{suffix}",
                display_name=suffix,
            )
            db.add(source)
            db.flush()
            item = CompetitorPriceListItem(
                price_list_id=source.id,
                name=suffix,
                distributor_goods_name=suffix,
                distributor_goods_id=suffix,
                distributor_price=Decimal("10"),
                raw_json="{}",
            )
            db.add(item)
            db.flush()
            capture_and_publish_legacy_source_version(
                db=db, competitor_price_list_id=int(source.id)
            )
            sources.append((int(source.id), int(item.id)))
        db.commit()

    first_source, first_item = sources[0]
    second_source, _ = sources[1]
    with Session(trigger_engine) as db:
        db.execute(
            text("UPDATE competitor_price_list_items SET distributor_price=11 WHERE id=:id"),
            {"id": first_item},
        )
        statuses = dict(db.execute(select(
            CompetitorSourcePublication.competitor_price_list_id,
            CompetitorSourcePublication.status,
        )).all())
        assert statuses[first_source] == "failed"
        assert statuses[second_source] == "published"
        db.rollback()
    with Session(trigger_engine) as db:
        statuses = dict(db.execute(select(
            CompetitorSourcePublication.competitor_price_list_id,
            CompetitorSourcePublication.status,
        )).all())
        assert statuses[first_source] == "published"
        assert statuses[second_source] == "published"
        db.execute(
            text("UPDATE competitor_price_list_items SET distributor_price=distributor_price WHERE id=:id"),
            {"id": first_item},
        )
        db.commit()
    with Session(trigger_engine) as db:
        assert db.scalar(select(CompetitorSourcePublication.status).where(
            CompetitorSourcePublication.competitor_price_list_id == first_source
        )) == "published"


def test_source_insert_delete_and_truncate_invalidation_is_transactional(
    trigger_engine, monkeypatch
):
    monkeypatch.setenv("MATCH_CACHE_ENABLED", "true")
    monkeypatch.setenv("MATCH_CACHE_DUAL_WRITE", "true")
    token = uuid.uuid4().hex
    with Session(trigger_engine) as db:
        source = CompetitorPriceList(
            source_type="manual",
            source_key=f"row-events:{token}",
            display_name="row events",
        )
        db.add(source)
        db.flush()
        item = CompetitorPriceListItem(
            price_list_id=source.id,
            name="row",
            distributor_goods_name="row",
            distributor_goods_id="row",
            distributor_price=Decimal("10"),
            raw_json="{}",
        )
        db.add(item)
        db.flush()
        capture_and_publish_legacy_source_version(
            db=db, competitor_price_list_id=int(source.id)
        )
        source_id = int(source.id)
        item_id = int(item.id)
        db.commit()

    operations = ("insert", "delete", "truncate")
    for operation in operations:
        with Session(trigger_engine) as db:
            if operation == "insert":
                db.execute(
                    CompetitorPriceListItem.__table__.insert().values(
                        price_list_id=source_id,
                        name="new",
                        distributor_goods_name="new",
                        distributor_goods_id="new",
                        raw_json="{}",
                    )
                )
            elif operation == "delete":
                db.execute(text("DELETE FROM competitor_price_list_items WHERE id=:item_id"), {"item_id": item_id})
            else:
                db.execute(text("TRUNCATE competitor_price_list_items CASCADE"))
            assert db.scalar(select(CompetitorSourcePublication.status).where(
                CompetitorSourcePublication.competitor_price_list_id == source_id
            )) == "failed"
            db.rollback()
        with Session(trigger_engine) as db:
            assert db.scalar(select(CompetitorSourcePublication.status).where(
                CompetitorSourcePublication.competitor_price_list_id == source_id
            )) == "published"


def test_verified_source_noop_preserves_order_duplicates_and_fails_closed(
    trigger_engine, monkeypatch
):
    monkeypatch.setenv("MATCH_CACHE_ENABLED", "true")
    monkeypatch.setenv("MATCH_CACHE_DUAL_WRITE", "true")
    monkeypatch.setenv("MATCH_CACHE_SOURCE_NOOP_ENABLED", "true")
    token = uuid.uuid4().hex
    with Session(trigger_engine) as db:
        source = CompetitorPriceList(
            source_type="manual",
            source_key=f"source-noop:{token}",
            display_name="source noop",
        )
        db.add(source)
        db.flush()
        for goods_id, price in (("duplicate", "10"), ("duplicate", "10"), ("other", "11")):
            db.add(CompetitorPriceListItem(
                price_list_id=source.id,
                name=goods_id,
                distributor_goods_name=goods_id,
                distributor_goods_id=goods_id,
                distributor_price=Decimal(price),
                match_type="unmatched",
                raw_json="{}",
            ))
        db.flush()
        capture = capture_and_publish_legacy_source_version(
            db=db, competitor_price_list_id=int(source.id)
        )
        vector = build_dependency_vector(db=db)
        snapshot, _ = get_or_create_shadow_snapshot(
            db=db, source_version_id=int(capture.version_id), vector=vector
        )
        build_shadow_snapshot(
            db=db,
            snapshot_id=int(snapshot.id),
            expected_dependency_fingerprint=vector.fingerprint,
        )
        source_id = int(source.id)
        db.commit()

    with Session(trigger_engine) as db:
        source = db.get(CompetitorPriceList, source_id)
        rows = list(db.scalars(select(CompetitorPriceListItem).where(
            CompetitorPriceListItem.price_list_id == source_id
        ).order_by(CompetitorPriceListItem.id)))
        incoming = [
            {field: getattr(row, field) for field in PROVIDER_SOURCE_FIELDS}
            for row in rows
        ]
        decision = validate_unchanged_source_import(
            db=db,
            source=source,
            incoming_rows=incoming,
            semantic_metadata_before=source_semantic_metadata(source),
        )
        assert decision.reusable
        assert decision.row_count == 3

        reordered = [incoming[2], incoming[0], incoming[1]]
        rejected = validate_unchanged_source_import(
            db=db,
            source=source,
            incoming_rows=reordered,
            semantic_metadata_before=source_semantic_metadata(source),
        )
        assert not rejected.reusable
        assert rejected.reason == "canonical_source_content_changed"

        old_metadata = source_semantic_metadata(source)
        source.display_name = "changed metadata"
        metadata_rejected = validate_unchanged_source_import(
            db=db,
            source=source,
            incoming_rows=incoming,
            semantic_metadata_before=old_metadata,
        )
        assert not metadata_rejected.reusable
        assert metadata_rejected.reason == "semantic_source_metadata_changed"
        db.rollback()


def test_provisor_identical_import_skips_replacement_but_changed_import_does_not(
    trigger_engine, monkeypatch
):
    monkeypatch.setenv("MATCH_CACHE_ENABLED", "true")
    monkeypatch.setenv("MATCH_CACHE_DUAL_WRITE", "true")
    monkeypatch.setenv("MATCH_CACHE_SOURCE_NOOP_ENABLED", "true")
    token = uuid.uuid4().hex
    format_code = f"NOOP-{token}"
    sku = f"NOOP-SKU-{token}"
    items = [{
        "id": 101,
        "goodsId": 202,
        "filialId": 303,
        "distributorGoodsId": sku,
        "distributorGoodsName": "No-op item",
        "goodsPrice": "12.50",
        "stored": "4",
    }]
    with Session(trigger_engine) as db:
        db.add(Product(code=sku, name="No-op item", cost=1))
        db.commit()
        source = upsert_provisor_price_list(
            db=db,
            price_format_code=format_code,
            filial_id=303,
            items=items,
            account_id=f"account-{token}",
            account_login="tester",
            run_matching=True,
        )
        source_id = int(source.id)
        original_ids = list(db.scalars(select(CompetitorPriceListItem.id).where(
            CompetitorPriceListItem.price_list_id == source_id
        ).order_by(CompetitorPriceListItem.id)))
        version_id = int(db.scalar(select(CompetitorSourceVersion.id).where(
            CompetitorSourceVersion.competitor_price_list_id == source_id,
            CompetitorSourceVersion.status == "published",
        )))
        vector = build_dependency_vector(db=db)
        snapshot, _ = get_or_create_shadow_snapshot(
            db=db, source_version_id=version_id, vector=vector
        )
        build_shadow_snapshot(
            db=db,
            snapshot_id=int(snapshot.id),
            expected_dependency_fingerprint=vector.fingerprint,
        )
        db.commit()

    with Session(trigger_engine) as db:
        source = upsert_provisor_price_list(
            db=db,
            price_format_code=format_code,
            filial_id=303,
            items=items,
            account_id=f"account-{token}",
            account_login="tester",
            run_matching=True,
        )
        assert source._source_noop.reusable
        assert list(db.scalars(select(CompetitorPriceListItem.id).where(
            CompetitorPriceListItem.price_list_id == source_id
        ).order_by(CompetitorPriceListItem.id))) == original_ids
        assert db.scalar(select(func.count(CompetitorSourceVersion.id)).where(
            CompetitorSourceVersion.competitor_price_list_id == source_id
        )) == 1

    changed_items = [{**items[0], "goodsPrice": "13.50"}]
    with Session(trigger_engine) as db:
        source = upsert_provisor_price_list(
            db=db,
            price_format_code=format_code,
            filial_id=303,
            items=changed_items,
            account_id=f"account-{token}",
            account_login="tester",
            run_matching=True,
        )
        assert not source._source_noop.reusable
        assert list(db.scalars(select(CompetitorPriceListItem.id).where(
            CompetitorPriceListItem.price_list_id == source_id
        ).order_by(CompetitorPriceListItem.id))) != original_ids
        assert db.scalar(select(func.count(CompetitorSourceVersion.id)).where(
            CompetitorSourceVersion.competitor_price_list_id == source_id
        )) == 2


def test_manual_identical_import_preserves_physical_rows_and_ready_artifacts(
    trigger_engine, monkeypatch
):
    monkeypatch.setenv("MATCH_CACHE_ENABLED", "true")
    monkeypatch.setenv("MATCH_CACHE_DUAL_WRITE", "true")
    monkeypatch.setenv("MATCH_CACHE_SOURCE_NOOP_ENABLED", "true")
    token = uuid.uuid4().hex
    format_code = f"MANUAL-NOOP-{token}"
    sku = f"MANUAL-SKU-{token}"
    content = f"SKU,Name,Price,Stock\n{sku},Manual no-op,9.99,3\n".encode()
    with Session(trigger_engine) as db:
        db.add(Product(code=sku, name="Manual no-op", cost=1))
        db.commit()
        created = import_manual_price_list(
            db=db,
            price_format_code=format_code,
            content=content,
            filename="manual-noop.csv",
            display_name="Manual no-op",
            requested_by="tester",
        )
        source_id = int(created["id"])
        original_ids = list(db.scalars(select(CompetitorPriceListItem.id).where(
            CompetitorPriceListItem.price_list_id == source_id
        ).order_by(CompetitorPriceListItem.id)))
        version_id = int(db.scalar(select(CompetitorSourceVersion.id).where(
            CompetitorSourceVersion.competitor_price_list_id == source_id,
            CompetitorSourceVersion.status == "published",
        )))
        vector = build_dependency_vector(db=db)
        snapshot, _ = get_or_create_shadow_snapshot(
            db=db, source_version_id=version_id, vector=vector
        )
        build_shadow_snapshot(
            db=db,
            snapshot_id=int(snapshot.id),
            expected_dependency_fingerprint=vector.fingerprint,
        )
        db.commit()

    with Session(trigger_engine) as db:
        result = import_manual_price_list(
            db=db,
            price_format_code=format_code,
            content=content,
            filename="manual-noop.csv",
            display_name="Manual no-op",
            price_list_id=source_id,
            requested_by="tester",
        )
        assert result["sourceNoop"] is True
        assert list(db.scalars(select(CompetitorPriceListItem.id).where(
            CompetitorPriceListItem.price_list_id == source_id
        ).order_by(CompetitorPriceListItem.id))) == original_ids
        assert db.scalar(select(func.count(CompetitorSourceVersion.id)).where(
            CompetitorSourceVersion.competitor_price_list_id == source_id
        )) == 1


def test_emit_identical_stage_skips_physical_replacement(
    trigger_engine, monkeypatch, tmp_path
):
    monkeypatch.setenv("MATCH_CACHE_ENABLED", "true")
    monkeypatch.setenv("MATCH_CACHE_DUAL_WRITE", "true")
    monkeypatch.setenv("MATCH_CACHE_SOURCE_NOOP_ENABLED", "true")
    source_path = tmp_path / "emit.ndjson"
    staging_path = tmp_path / "emit-stage.sqlite"
    source_path.write_text("\n".join(json.dumps(row) for row in (
        {"id": 1, "goodsId": 10, "distributorGoodsName": "A", "distributorGoodsId": "SKU-A", "goodsPrice": 12.34, "stored": 5},
        {"id": 2, "goodsId": 10, "distributorGoodsName": "A", "distributorGoodsId": "SKU-A", "goodsPrice": 13.45, "stored": 6},
    )), encoding="utf-8")
    config = EmitConfig(
        temp_dir=str(tmp_path),
        min_free_disk_gb=0,
        batch_insert_size=1,
        min_final_rows=1,
    )
    first_stats = parse_normalize_stage(
        source_path=source_path,
        stage_db_path=staging_path,
        filial_id=991106,
        filial_name="Synthetic Emit",
        config=config,
    )
    with Session(trigger_engine) as db:
        source = replace_emit_price_list_from_staging(
            db=db,
            config=config,
            filial_id=991106,
            filial_name="Synthetic Emit",
            staging_path=staging_path,
            stats=first_stats,
        )
        source_id = int(source.id)
        original_ids = list(db.scalars(select(CompetitorPriceListItem.id).where(
            CompetitorPriceListItem.price_list_id == source_id
        ).order_by(CompetitorPriceListItem.id)))

    second_stats = parse_normalize_stage(
        source_path=source_path,
        stage_db_path=tmp_path / "emit-stage-second.sqlite",
        filial_id=991106,
        filial_name="Synthetic Emit",
        config=config,
    )
    with Session(trigger_engine) as db:
        source = replace_emit_price_list_from_staging(
            db=db,
            config=config,
            filial_id=991106,
            filial_name="Synthetic Emit",
            staging_path=tmp_path / "emit-stage-second.sqlite",
            stats=second_stats,
        )
        assert source._source_noop.reusable
        assert second_stats.replacement_strategy == "verified_identical_noop"
        assert second_stats.deleted_old_row_count == 0
        assert second_stats.final_inserted_row_count == 0
        assert list(db.scalars(select(CompetitorPriceListItem.id).where(
            CompetitorPriceListItem.price_list_id == source_id
        ).order_by(CompetitorPriceListItem.id))) == original_ids
