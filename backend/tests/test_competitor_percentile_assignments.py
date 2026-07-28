from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import (
    CompetitorPrice,
    CompetitorPriceList,
    CompetitorPriceListItem,
    CompetitorPricePercentile,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    Product,
)
from backend.app.services.competitor_percentiles import (
    MULTI_PRICE_PERCENTILE_MODE,
    recalculate_percentiles_for_price_lists,
)
from backend.app.services.competitors.percentiles.read_models import (
    list_percentile_product_rows,
    list_percentile_sources,
)
from backend.app.services.competitors.percentiles.sources import (
    PERCENTILE_SOURCE_COMPETITOR,
    PERCENTILE_SOURCE_EMIT,
    percentile_source_id,
)
from backend.app.services.competitor_price_lists import sync_selected_competitor_configs


def _session_factory_static():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _format(db, *, code: str = "FMT"):
    row = PriceFormat(code=code, name=code, branch="Aktau")
    db.add(row)
    db.flush()
    return row


def _product(db, *, code: str = "SKU-1", goods_id: int = 100):
    row = Product(code=code, name=f"Product {code}", provisor_goods_id=goods_id, cost=10)
    db.add(row)
    db.flush()
    return row


def _price_list(
    db,
    pf,
    *,
    source_key: str,
    branch: str = "Aktau",
    competitor: str = "Regular Provisor",
    account_id: str = "7",
    external_price_list_id: str | None = None,
):
    row = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key=source_key,
        display_name=f"{competitor} {source_key}",
        supplier=competitor,
        branch_name=branch,
        competitor_name=competitor,
        account_id=account_id,
        account_login=f"account-{account_id}",
        external_price_list_id=external_price_list_id or source_key.split(":")[-1],
    )
    db.add(row)
    db.flush()
    return row


def _assign(db, pf, price_list, *, active: bool = True, percentile_mode: str = ""):
    row = PriceFormatCompetitorAssignment(
        price_format_id=pf.id,
        competitor_price_list_id=price_list.id,
        is_active=active,
        percentile_mode=percentile_mode,
    )
    db.add(row)
    db.flush()
    return row


def _percentile_config_name(pf_id: int, source_key: str, competitor: str, pct: int = 10) -> str:
    source_id = percentile_source_id(
        percentile_source=PERCENTILE_SOURCE_COMPETITOR,
        price_format_id=pf_id,
        scope="global",
        source_key=source_key,
        region="",
        competitor=competitor,
        percentile=pct,
    )
    return f"percentile:{source_id}"


def _emit_percentile_config_name(pf_id: int, source_key: str, region: str, competitor: str, pct: int = 10) -> str:
    source_id = percentile_source_id(
        percentile_source=PERCENTILE_SOURCE_EMIT,
        price_format_id=pf_id,
        scope="regional",
        source_key=source_key,
        region=region,
        competitor=competitor,
        percentile=pct,
    )
    return f"percentile:{source_id}"


def test_assignment_summary_counts_only_active_physical_plk_rows():
    import backend.app.main as main

    Session = _session_factory_static()
    with Session() as db:
        pf = _format(db, code="COUNT")
        product = _product(db)
        percentile_sources = []
        for idx in range(6):
            source_key = f"7:{300 + idx}"
            competitor = f"Competitor {idx + 1}"
            price_list = _price_list(db, pf, source_key=source_key, competitor=competitor)
            _assign(
                db,
                pf,
                price_list,
                percentile_mode=MULTI_PRICE_PERCENTILE_MODE if idx < 3 else "",
            )
            if idx < 3:
                percentile_sources.append((source_key, competitor, price_list.id))
                db.add(
                    CompetitorPricePercentile(
                        price_format_id=pf.id,
                        product_id=product.id,
                        competitor_price_list_id=price_list.id,
                        source_type="provisor",
                        source_key=source_key,
                        branch_name=price_list.branch_name,
                        competitor_name=competitor,
                        percentile_scope="regional",
                        percentile=10,
                        value=Decimal("100.00"),
                        source_count=1,
                        price_count=1,
                        used_price_count=1,
                        status="Calculated",
                    )
                )
        db.flush()
        for source_key, competitor, _price_list_id in percentile_sources:
            db.add(
                CompetitorPrice(
                    price_format_id=pf.id,
                    source_name=_percentile_config_name(pf.id, source_key, competitor),
                    supplier=competitor,
                    coefficient=1,
                )
            )
        db.commit()

    main.app.dependency_overrides[main.get_db] = lambda: Session()
    try:
        response = TestClient(main.app).get("/api/price-formats/COUNT/competitor-assignments?include_summary=1")
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["activePhysicalPlkCount"] == 6
    assert payload["summary"]["percentileSourceCount"] == 3
    assert payload["summary"]["totalRowsCount"] == 9
    assert sum(1 for row in payload["items"] if row["assignmentKind"] == "physical") == 6
    assert sum(1 for row in payload["items"] if row["assignmentKind"] == "percentile_config") == 3


def test_ordinary_provisor_refresh_scope_recalculates_multi_price_percentiles():
    db = _session()
    pf = _format(db, code="RECALC")
    product = _product(db, code="SKU-1", goods_id=100)
    selected = _price_list(db, pf, source_key="7:302", competitor="Amanat", external_price_list_id="302")
    other = _price_list(db, pf, source_key="7:303", competitor="Amanat", external_price_list_id="303")
    _assign(db, pf, selected, percentile_mode=MULTI_PRICE_PERCENTILE_MODE)
    _assign(db, pf, other, percentile_mode=MULTI_PRICE_PERCENTILE_MODE)
    for price in (Decimal("80"), Decimal("100"), Decimal("120")):
        db.add(
            CompetitorPriceListItem(
                price_list_id=selected.id,
                product_id=product.id,
                provisor_goods_id=100,
                distributor_goods_id="SKU-1",
                distributor_price=price,
            )
        )
    db.add(
        CompetitorPriceListItem(
            price_list_id=other.id,
            product_id=product.id,
            provisor_goods_id=100,
            distributor_goods_id="SKU-1",
            distributor_price=Decimal("999"),
        )
    )
    db.commit()

    summary = recalculate_percentiles_for_price_lists(db=db, competitor_price_list_ids=[selected.id])
    db.commit()

    rows = (
        db.execute(
            select(CompetitorPricePercentile)
            .where(CompetitorPricePercentile.price_format_id == pf.id)
            .where(CompetitorPricePercentile.product_id == product.id)
            .where(CompetitorPricePercentile.source_key == "7:302")
            .where(CompetitorPricePercentile.percentile_scope == "regional")
        )
        .scalars()
        .all()
    )
    values = {int(row.percentile): float(row.value) for row in rows if row.value is not None}
    assert summary["priceFormatsProcessed"] == 1
    assert summary["percentileSourcesProcessed"] == 1
    assert summary["percentileRowsWritten"] > 0
    assert values[10] == pytest.approx(84.0)
    assert values[60] == pytest.approx(104.0)
    assert sorted(values) == [10, 20, 30, 40, 60]
    assert not db.execute(
        select(CompetitorPricePercentile).where(CompetitorPricePercentile.source_key == "7:303")
    ).first()


def test_competitor_percentile_rows_survive_reload_only_for_active_matching_assignment():
    db = _session()
    pf = _format(db, code="RELOAD")
    product = _product(db, code="SKU-1", goods_id=100)
    price_list = _price_list(db, pf, source_key="7:302", competitor="Amanat", external_price_list_id="302")
    assignment = _assign(db, pf, price_list, percentile_mode=MULTI_PRICE_PERCENTILE_MODE)
    for price in (Decimal("80"), Decimal("100"), Decimal("120")):
        db.add(
            CompetitorPriceListItem(
                price_list_id=price_list.id,
                product_id=product.id,
                provisor_goods_id=100,
                distributor_goods_id="SKU-1",
                distributor_price=price,
            )
        )
    db.commit()

    recalculate_percentiles_for_price_lists(db=db, competitor_price_list_ids=[price_list.id])
    db.commit()

    sources = list_percentile_sources(
        db=db,
        price_format_code=pf.code,
        percentile_source=PERCENTILE_SOURCE_COMPETITOR,
    )
    assert {row["sourceKey"] for row in sources} == {"7:302"}

    page = list_percentile_product_rows(
        db=db,
        price_format_code=pf.code,
        competitor="Amanat",
        source_key="7:302",
        percentile_source=PERCENTILE_SOURCE_COMPETITOR,
    )
    assert page["summary"]["productsWithPercentile"] == 1
    assert page["items"][0]["percentiles"]["10"] == pytest.approx(84.0)

    assignment.is_active = False
    db.commit()

    assert list_percentile_sources(
        db=db,
        price_format_code=pf.code,
        percentile_source=PERCENTILE_SOURCE_COMPETITOR,
    ) == []
    hidden = list_percentile_product_rows(
        db=db,
        price_format_code=pf.code,
        competitor="Amanat",
        source_key="7:302",
        percentile_source=PERCENTILE_SOURCE_COMPETITOR,
    )
    assert hidden["groups"] == []
    assert hidden["summary"]["productsWithPercentile"] == 0


def test_assignment_visibility_keeps_stored_emit_percentile_sources_after_physical_assignment_removed():
    import backend.app.main as main

    Session = _session_factory_static()
    with Session() as db:
        pf = _format(db, code="ASSIGN-PCT")
        pf_id = int(pf.id)
        product = _product(db)
        source_key = "emit:302"
        competitor = "Emiti"
        price_list = _price_list(db, pf, source_key=source_key, competitor=competitor, external_price_list_id="302")
        price_list.source_type = "emit"
        assignment = _assign(db, pf, price_list, percentile_mode=MULTI_PRICE_PERCENTILE_MODE)
        db.add(
            CompetitorPricePercentile(
                price_format_id=pf.id,
                product_id=product.id,
                competitor_price_list_id=price_list.id,
                source_type="emit",
                source_key=source_key,
                branch_name=price_list.branch_name,
                competitor_name=competitor,
                percentile_scope="regional",
                percentile=10,
                value=Decimal("100.00"),
                source_count=1,
                price_count=1,
                used_price_count=1,
                status="Calculated",
            )
        )
        db.add(
            CompetitorPrice(
                price_format_id=pf.id,
                source_name=_emit_percentile_config_name(pf_id, source_key, price_list.branch_name, competitor),
                supplier=competitor,
                coefficient=1,
            )
        )
        db.commit()

        visible_for_pricing = list_percentile_sources(
            db=db,
            price_format_code=pf.code,
            percentile_source=PERCENTILE_SOURCE_EMIT,
        )
        assert len(visible_for_pricing) == 1
        assert visible_for_pricing[0]["eligibleForPricing"] is True

        assignment.is_active = False
        sync_selected_competitor_configs(db=db, price_format_id=pf.id)
        db.commit()

        assert db.execute(
            select(CompetitorPrice).where(CompetitorPrice.source_name.like("percentile:%"))
        ).scalar_one_or_none() is not None
        assert list_percentile_sources(
            db=db,
            price_format_code=pf.code,
            percentile_source=PERCENTILE_SOURCE_EMIT,
        ) == []
        assignment_visible = list_percentile_sources(
            db=db,
            price_format_code=pf.code,
            percentile_source=PERCENTILE_SOURCE_EMIT,
            include_ineligible=True,
        )
        assert len(assignment_visible) == 1
        assert assignment_visible[0]["sourceKey"] == source_key
        assert assignment_visible[0]["eligibleForPricing"] is False

    main.app.dependency_overrides[main.get_db] = lambda: Session()
    try:
        response = TestClient(main.app).get("/api/price-formats/ASSIGN-PCT/competitor-assignments")
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    percentile_rows = [row for row in payload if row["assignmentKind"] == "percentile_config"]
    assert len(percentile_rows) == 1
    assert percentile_rows[0]["sourceKey"].startswith(f"{pf_id}:regional:{source_key}:")
    assert percentile_rows[0]["eligibleForPricing"] is False
