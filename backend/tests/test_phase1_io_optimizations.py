from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import main
from backend.app.db import Base
from backend.app.models import (
    AppUser,
    CalculatedPrice,
    CompetitorPriceList,
    CompetitorPriceListItem,
    CompetitorPricePercentile,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    PriceFormatPercentilePreparation,
    PriceList,
    Product,
)
from backend.app.services import percentile_preparation


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _captured_selects(engine):
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().lower().startswith("select"):
            statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", capture)
    return statements, capture


def test_persisted_percentile_status_uses_rows_count_without_live_cpp_count():
    engine, Session = _session_factory()
    with Session() as db:
        pf = PriceFormat(code="PERSISTED", name="Persisted", branch="Astana")
        db.add(pf)
        db.flush()
        db.add(PriceFormatPercentilePreparation(price_format_id=pf.id, status="ready", rows_count=37))
        db.commit()
        pf_id = int(pf.id)

        statements, capture = _captured_selects(engine)
        try:
            payload = percentile_preparation.percentile_preparation_to_dict(db, pf_id)
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        assert payload["status"] == "ready"
        assert payload["rowsCount"] == 37
        assert not any("count(" in sql and "competitor_price_percentiles" in sql for sql in statements)


def test_missing_percentile_metadata_retains_exact_live_fallback():
    engine, Session = _session_factory()
    with Session() as db:
        pf = PriceFormat(code="LEGACY", name="Legacy", branch="Astana")
        product = Product(code="LEGACY-SKU", name="Legacy SKU", cost=Decimal("1"))
        db.add_all([pf, product])
        db.flush()
        db.add(
            CompetitorPricePercentile(
                price_format_id=pf.id,
                product_id=product.id,
                source_key="legacy",
                branch_name="Astana",
                competitor_name="Legacy",
                percentile_scope="regional",
                percentile=10,
                value=Decimal("10"),
            )
        )
        db.commit()
        pf_id = int(pf.id)

        statements, capture = _captured_selects(engine)
        try:
            payload = percentile_preparation.percentile_preparation_to_dict(db, pf_id)
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        assert payload["rowsCount"] == 1
        assert any("count(" in sql and "competitor_price_percentiles" in sql for sql in statements)
        assert db.get(PriceFormatPercentilePreparation, pf_id).rows_count == 1


def test_raw_percentile_availability_uses_limit_probe_not_count(monkeypatch):
    engine, Session = _session_factory()
    with Session() as db:
        pf = PriceFormat(code="RAW", name="Raw", branch="Astana")
        product = Product(code="RAW-SKU", name="Raw SKU", cost=Decimal("1"))
        db.add_all([pf, product])
        db.flush()
        price_list = CompetitorPriceList(
            price_format_id=pf.id,
            source_type="provisor",
            source_key="provisor:raw",
            items_count=1,
        )
        db.add(price_list)
        db.flush()
        db.add_all(
            [
                PriceFormatCompetitorAssignment(
                    price_format_id=pf.id,
                    competitor_price_list_id=price_list.id,
                    is_active=True,
                ),
                CompetitorPriceListItem(
                    price_list_id=price_list.id,
                    product_id=product.id,
                    distributor_price=Decimal("12"),
                ),
            ]
        )
        db.commit()
        pf_id = int(pf.id)
        monkeypatch.setattr(
            percentile_preparation,
            "eligible_percentile_assignments",
            lambda **_kwargs: [SimpleNamespace(price_list=price_list)],
        )

        statements, capture = _captured_selects(engine)
        try:
            assert percentile_preparation.has_raw_percentile_data(db, pf_id) is True
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        item_probes = [sql for sql in statements if "competitor_price_list_items" in sql]
        assert item_probes
        assert all("count(" not in sql for sql in item_probes)
        assert any(" limit " in sql for sql in item_probes)


def test_generated_price_list_counts_are_exact_in_one_grouped_query():
    engine, Session = _session_factory()
    with Session() as db:
        pf = PriceFormat(code="BATCH", name="Batch", branch="Astana")
        products = [Product(code=f"SKU-{index}", name=f"SKU {index}", cost=Decimal("1")) for index in range(3)]
        db.add_all([pf, *products])
        db.flush()
        first = PriceList(number="BATCH-1", price_format_id=pf.id)
        second = PriceList(number="BATCH-2", price_format_id=pf.id)
        db.add_all([first, second])
        db.flush()
        db.add_all(
            [
                CalculatedPrice(price_list_id=first.id, product_id=products[0].id, cost=1, base_price=1, final_price=1, competitor_price=2),
                CalculatedPrice(price_list_id=first.id, product_id=products[1].id, cost=1, base_price=1, final_price=1, competitor_price=None),
                CalculatedPrice(price_list_id=second.id, product_id=products[2].id, cost=1, base_price=1, final_price=1, competitor_price=3),
            ]
        )
        db.commit()
        ids = [int(first.id), int(second.id)]

        statements, capture = _captured_selects(engine)
        try:
            counts = main._generated_price_list_counts(db, ids)
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        aggregate_queries = [sql for sql in statements if "count(" in sql and "calculated_prices" in sql]
        assert len(aggregate_queries) == 1
        assert "group by calculated_prices.price_list_id" in aggregate_queries[0]
        assert counts == {ids[0]: (2, 1), ids[1]: (1, 1)}


def test_refresh_item_counts_use_authoritative_stored_values_without_session_query():
    rows = [
        CompetitorPriceList(id=11, source_key="one", items_count=123),
        CompetitorPriceList(id=12, source_key="two", items_count=0),
    ]

    assert main._authoritative_price_list_item_counts(rows) == {11: 123, 12: 0}


def test_production_diagnostics_require_admin(monkeypatch):
    monkeypatch.setattr(main.settings, "environment", "prod")
    manager = AppUser(id=1, username="manager", role="pricing_manager", is_active=True)
    admin = AppUser(id=2, username="admin", role="admin", is_active=True)

    with pytest.raises(HTTPException) as exc_info:
        main._require_diagnostics_access(manager)
    assert exc_info.value.status_code == 403
    main._require_diagnostics_access(admin)


def test_development_diagnostics_remain_available_to_authenticated_users(monkeypatch):
    monkeypatch.setattr(main.settings, "environment", "dev")
    manager = AppUser(id=1, username="manager", role="pricing_manager", is_active=True)

    main._require_diagnostics_access(manager)
