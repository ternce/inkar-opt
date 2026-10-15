from __future__ import annotations

import os
import time
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.db import Base
from backend.app.models import (
    CompetitorPriceList,
    CompetitorPriceListItem,
    CompetitorPricePercentile,
    CompetitorPricePercentileSourceSummary,
    PriceFormat,
    Product,
    RegularCompetitorPricePercentile,
    RegularCompetitorPricePercentileSourceSummary,
)
from backend.app.services.competitor_percentiles import (
    _fanout_emit_percentiles_postgresql_multi_target,
    recalculate_competitor_percentiles,
    recalculate_regular_competitor_percentiles,
)


def _postgres_url() -> str:
    url = os.getenv("TEST_POSTGRES_URL", "").strip()
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required")
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture(scope="module")
def pg_engine():
    url = _postgres_url()
    admin = create_engine(url, poolclass=NullPool)
    schema = "phase3_percentiles_" + uuid.uuid4().hex
    with admin.begin() as connection:
        version = int(connection.scalar(text("SHOW server_version_num")))
        if not 160000 <= version < 170000:
            pytest.skip("PostgreSQL 16 required")
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(
        url,
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={schema},public"},
    )
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


def _business_rows(db: Session):
    return [
        tuple(row)
        for row in db.execute(
            select(
                CompetitorPricePercentile.price_format_id,
                CompetitorPricePercentile.product_id,
                CompetitorPricePercentile.competitor_price_list_id,
                CompetitorPricePercentile.source_type,
                CompetitorPricePercentile.source_key,
                CompetitorPricePercentile.branch_name,
                CompetitorPricePercentile.competitor_name,
                CompetitorPricePercentile.percentile_scope,
                CompetitorPricePercentile.percentile,
                CompetitorPricePercentile.value,
                CompetitorPricePercentile.source_count,
                CompetitorPricePercentile.price_count,
                CompetitorPricePercentile.used_price_count,
                CompetitorPricePercentile.status,
            ).order_by(
                CompetitorPricePercentile.product_id,
                CompetitorPricePercentile.percentile_scope,
                CompetitorPricePercentile.percentile,
            )
        )
    ]


def test_emit_percentile_differential_is_noop_exact_and_transactional(pg_engine, monkeypatch):
    with Session(pg_engine) as db:
        price_format = PriceFormat(id=4, code="PHASE3-PCT", name="Phase 3", branch="Benchmark")
        first = Product(code="PHASE3-1", name="First", cost=1)
        second = Product(code="PHASE3-2", name="Second", cost=1)
        missing = Product(code="PHASE3-3", name="Missing", cost=1)
        db.add_all([price_format, first, second, missing])
        db.flush()
        source = CompetitorPriceList(
            price_format_id=4,
            source_type="emit",
            source_key="emit:phase3",
            display_name="Synthetic Emit",
            supplier="Synthetic",
            branch_name="Benchmark",
            competitor_name="Synthetic",
            external_price_list_id="phase3",
        )
        db.add(source)
        db.flush()
        db.add_all([
            CompetitorPriceListItem(price_list_id=source.id, product_id=first.id, distributor_price=10),
            CompetitorPriceListItem(price_list_id=source.id, product_id=first.id, distributor_price=20),
            CompetitorPriceListItem(price_list_id=source.id, product_id=second.id, distributor_price=30),
        ])
        db.commit()
        source_id = int(source.id)

    monkeypatch.setenv("PERCENTILE_DIFFERENTIAL_PUBLICATION_ENABLED", "false")
    with Session(pg_engine) as db:
        legacy = recalculate_competitor_percentiles(
            db=db,
            price_format_id=4,
            source_price_list_ids=[source_id],
            require_assignment=False,
        )
        db.commit()
        expected = _business_rows(db)
        original_ids = list(db.scalars(select(CompetitorPricePercentile.id).order_by(CompetitorPricePercentile.id)))
        original_summary_ids = list(db.scalars(select(CompetitorPricePercentileSourceSummary.id).order_by(CompetitorPricePercentileSourceSummary.id)))
    assert legacy["rows_created"] == len(expected)
    assert any(row[9] is None for row in expected)

    monkeypatch.setenv("PERCENTILE_DIFFERENTIAL_PUBLICATION_ENABLED", "true")
    with Session(pg_engine) as db:
        unchanged = recalculate_competitor_percentiles(
            db=db,
            price_format_id=4,
            source_price_list_ids=[source_id],
            require_assignment=False,
        )
        db.commit()
        assert _business_rows(db) == expected
        assert list(db.scalars(select(CompetitorPricePercentile.id).order_by(CompetitorPricePercentile.id))) == original_ids
        assert list(db.scalars(select(CompetitorPricePercentileSourceSummary.id).order_by(CompetitorPricePercentileSourceSummary.id))) == original_summary_ids
    assert unchanged["rows_created"] == 0
    assert unchanged["rows_updated"] == 0
    assert unchanged["rows_deleted"] == 0
    assert unchanged["rows_skipped"] == len(expected)

    with Session(pg_engine) as db:
        item = db.scalar(
            select(CompetitorPriceListItem)
            .where(CompetitorPriceListItem.price_list_id == source_id)
            .order_by(CompetitorPriceListItem.id)
        )
        item.distributor_price = Decimal("11")
        changed = recalculate_competitor_percentiles(
            db=db,
            price_format_id=4,
            source_price_list_ids=[source_id],
            require_assignment=False,
        )
        differential_rows = _business_rows(db)
        db.commit()
    assert changed["rows_updated"] > 0
    assert changed["rows_created"] == 0

    monkeypatch.setenv("PERCENTILE_DIFFERENTIAL_PUBLICATION_ENABLED", "false")
    with Session(pg_engine) as db:
        recalculate_competitor_percentiles(
            db=db,
            price_format_id=4,
            source_price_list_ids=[source_id],
            require_assignment=False,
        )
        assert _business_rows(db) == differential_rows
        db.rollback()

    monkeypatch.setenv("PERCENTILE_DIFFERENTIAL_PUBLICATION_ENABLED", "true")
    with Session(pg_engine) as db:
        before = _business_rows(db)
        item = db.scalar(
            select(CompetitorPriceListItem)
            .where(CompetitorPriceListItem.price_list_id == source_id)
            .order_by(CompetitorPriceListItem.id)
        )
        item.distributor_price = Decimal("99")
        recalculate_competitor_percentiles(
            db=db,
            price_format_id=4,
            source_price_list_ids=[source_id],
            require_assignment=False,
        )
        assert _business_rows(db) != before
        db.rollback()
    with Session(pg_engine) as db:
        assert _business_rows(db) == before


def test_regular_percentile_differential_preserves_ids_and_null_semantics(pg_engine, monkeypatch):
    with Session(pg_engine) as db:
        source = CompetitorPriceList(
            price_format_id=4,
            source_type="manual",
            source_key="manual:phase3-regular",
            display_name="Regular Synthetic",
            supplier="Regular Synthetic",
            competitor_name="Regular Synthetic",
            last_refresh_status="success",
        )
        db.add(source)
        db.flush()
        product_id = int(db.scalar(select(Product.id).order_by(Product.id)))
        db.add_all([
            CompetitorPriceListItem(price_list_id=source.id, product_id=product_id, distributor_price=10),
            CompetitorPriceListItem(price_list_id=source.id, product_id=product_id, distributor_price=20),
        ])
        db.commit()

    monkeypatch.setenv("PERCENTILE_DIFFERENTIAL_PUBLICATION_ENABLED", "false")
    with Session(pg_engine) as db:
        recalculate_regular_competitor_percentiles(db=db)
        db.commit()
        expected = list(db.execute(select(
            RegularCompetitorPricePercentile.competitor_identity,
            RegularCompetitorPricePercentile.product_id,
            RegularCompetitorPricePercentile.percentile,
            RegularCompetitorPricePercentile.value,
            RegularCompetitorPricePercentile.sample_count,
            RegularCompetitorPricePercentile.source_count,
        ).order_by(RegularCompetitorPricePercentile.competitor_identity, RegularCompetitorPricePercentile.percentile)))
        ids = list(db.scalars(select(RegularCompetitorPricePercentile.id).order_by(RegularCompetitorPricePercentile.id)))
        summary_ids = list(db.scalars(select(RegularCompetitorPricePercentileSourceSummary.id).order_by(RegularCompetitorPricePercentileSourceSummary.id)))

    monkeypatch.setenv("PERCENTILE_DIFFERENTIAL_PUBLICATION_ENABLED", "true")
    with Session(pg_engine) as db:
        result = recalculate_regular_competitor_percentiles(db=db)
        db.commit()
        actual = list(db.execute(select(
            RegularCompetitorPricePercentile.competitor_identity,
            RegularCompetitorPricePercentile.product_id,
            RegularCompetitorPricePercentile.percentile,
            RegularCompetitorPricePercentile.value,
            RegularCompetitorPricePercentile.sample_count,
            RegularCompetitorPricePercentile.source_count,
        ).order_by(RegularCompetitorPricePercentile.competitor_identity, RegularCompetitorPricePercentile.percentile)))
        assert actual == expected
        assert list(db.scalars(select(RegularCompetitorPricePercentile.id).order_by(RegularCompetitorPricePercentile.id))) == ids
        assert list(db.scalars(select(RegularCompetitorPricePercentileSourceSummary.id).order_by(RegularCompetitorPricePercentileSourceSummary.id))) == summary_ids
    assert result["regularPercentileRowsWritten"] == 0
    assert result["regularPercentileRowsUpdated"] == 0
    assert result["regularPercentileRowsUnchanged"] == len(expected)


def test_multi_target_fanout_differential_preserves_rows_and_summaries(pg_engine, monkeypatch):
    with Session(pg_engine) as db:
        target = PriceFormat(code="PHASE3-FANOUT", name="Fanout", branch="Benchmark")
        db.add(target)
        db.flush()
        target_id = int(target.id)
        source = db.scalar(select(CompetitorPriceList).where(CompetitorPriceList.source_key == "emit:phase3"))
        selected = [{
            "price_list_id": int(source.id),
            "branch_name": "Benchmark",
            "competitor_name": "Synthetic",
            "source_key": "emit:phase3",
        }]
        db.commit()

    monkeypatch.setenv("PERCENTILE_DIFFERENTIAL_PUBLICATION_ENABLED", "false")
    with Session(pg_engine) as db:
        legacy = _fanout_emit_percentiles_postgresql_multi_target(
            db=db,
            source_price_format_id=4,
            target_price_format_ids=[target_id],
            selected_sources=selected,
            started_at=time.perf_counter(),
        )
        db.commit()
        row_ids = list(db.scalars(select(CompetitorPricePercentile.id).where(
            CompetitorPricePercentile.price_format_id == target_id
        ).order_by(CompetitorPricePercentile.id)))
        summary_ids = list(db.scalars(select(CompetitorPricePercentileSourceSummary.id).where(
            CompetitorPricePercentileSourceSummary.price_format_id == target_id
        ).order_by(CompetitorPricePercentileSourceSummary.id)))
    assert legacy["actual_copied_rows"] == legacy["expected_copied_rows"]

    monkeypatch.setenv("PERCENTILE_DIFFERENTIAL_PUBLICATION_ENABLED", "true")
    with Session(pg_engine) as db:
        unchanged = _fanout_emit_percentiles_postgresql_multi_target(
            db=db,
            source_price_format_id=4,
            target_price_format_ids=[target_id],
            selected_sources=selected,
            started_at=time.perf_counter(),
        )
        db.commit()
        assert list(db.scalars(select(CompetitorPricePercentile.id).where(
            CompetitorPricePercentile.price_format_id == target_id
        ).order_by(CompetitorPricePercentile.id))) == row_ids
        assert list(db.scalars(select(CompetitorPricePercentileSourceSummary.id).where(
            CompetitorPricePercentileSourceSummary.price_format_id == target_id
        ).order_by(CompetitorPricePercentileSourceSummary.id))) == summary_ids
    assert unchanged["rows_created"] == 0
    assert unchanged["rows_updated"] == 0
    assert unchanged["rows_deleted"] == 0
    assert unchanged["rows_skipped"] == unchanged["expected_copied_rows"]
    assert unchanged["summary_rows_inserted"] == 0
    assert unchanged["summary_rows_updated"] == 0
    assert unchanged["summary_rows_deleted"] == 0
    assert unchanged["summary_rows_unchanged"] == unchanged["summary_rows_refreshed"]
