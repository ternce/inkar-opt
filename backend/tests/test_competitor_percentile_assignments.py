from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import (
    CompetitorPrice,
    CompetitorPriceList,
    CompetitorPriceListItem,
    CompetitorPricePercentile,
    CompetitorPricePercentileSourceSummary,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    Product,
    RegularCompetitorPricePercentile,
    RegularCompetitorPricePercentileSourceSummary,
)
from backend.app.services.competitor_percentiles import (
    MULTI_PRICE_PERCENTILE_MODE,
    REGULAR_COMPETITOR_SCOPE,
    recalculate_percentiles_for_price_lists,
)
from backend.app.services.pricing import load_percentile_price_cache, resolve_percentile_prices_from_cache
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
from backend.app.services.competitors.identity import canonical_regular_competitor_identity
from backend.app.services.competitor_read_models import (
    live_emit_percentile_source_summary_rows,
    live_price_list_item_counts,
    live_regular_percentile_source_summary_rows,
    refresh_emit_percentile_source_summaries,
    refresh_price_list_item_counters,
    refresh_regular_percentile_source_summaries,
)


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


def _regular_percentile_config_name(pf_id: int, competitor_identity: str, competitor: str, pct: int = 10) -> str:
    source_id = percentile_source_id(
        percentile_source=PERCENTILE_SOURCE_COMPETITOR,
        price_format_id=pf_id,
        scope=REGULAR_COMPETITOR_SCOPE,
        source_key=competitor_identity,
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
                identity = competitor.casefold()
                percentile_sources.append((identity, competitor, price_list.id))
                db.add(
                    RegularCompetitorPricePercentile(
                        competitor_identity=identity,
                        competitor_name=competitor,
                        product_id=product.id,
                        percentile=10,
                        value=Decimal("100.00"),
                        source_count=1,
                        sample_count=1,
                    )
                )
        db.flush()
        refresh_regular_percentile_source_summaries(
            db=db,
            competitor_identities={source_key for source_key, _competitor, _price_list_id in percentile_sources},
        )
        for source_key, competitor, _price_list_id in percentile_sources:
            db.add(
                CompetitorPrice(
                    price_format_id=pf.id,
                    source_name=_regular_percentile_config_name(pf.id, source_key, competitor),
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


def test_regular_percentile_sources_reuse_assigned_identity_lookup():
    Session = _session_factory_static()
    with Session() as db:
        pf = _format(db, code="REG-QCOUNT")
        product = _product(db)
        price_list = _price_list(db, pf, source_key="7:401", competitor="Amanat")
        _assign(db, pf, price_list)
        identity = canonical_regular_competitor_identity("Amanat")
        db.add(
            RegularCompetitorPricePercentile(
                competitor_identity=identity,
                competitor_name="Amanat",
                product_id=product.id,
                percentile=10,
                value=Decimal("100.00"),
                source_count=1,
                sample_count=1,
            )
        )
        refresh_regular_percentile_source_summaries(db=db, competitor_identities={identity})
        db.commit()

        assignment_lookup_count = 0

        def count_assignment_lookup(_conn, _cursor, statement, _parameters, _context, _executemany):
            nonlocal assignment_lookup_count
            normalized = " ".join(str(statement).lower().split())
            if "from competitor_price_lists" in normalized and "price_format_competitor_assignments" in normalized:
                assignment_lookup_count += 1

        event.listen(db.bind, "before_cursor_execute", count_assignment_lookup)
        try:
            sources = list_percentile_sources(
                db=db,
                price_format_code=pf.code,
                percentile_source=PERCENTILE_SOURCE_COMPETITOR,
                include_ineligible=True,
            )
        finally:
            event.remove(db.bind, "before_cursor_execute", count_assignment_lookup)

    assert len(sources) == 1
    assert assignment_lookup_count == 1


def test_emit_only_assignment_does_not_load_regular_percentile_sources():
    import backend.app.main as main

    Session = _session_factory_static()
    with Session() as db:
        pf = _format(db, code="EMIT-ONLY")
        product = _product(db)
        price_list = CompetitorPriceList(
            price_format_id=pf.id,
            source_type="emit",
            source_key="emit:branch-1",
            display_name="Emit Aktau",
            supplier="Emit",
            branch_name="Aktau",
            competitor_name="Emit",
            external_price_list_id="branch-1",
        )
        db.add(price_list)
        db.flush()
        _assign(db, pf, price_list, percentile_mode=MULTI_PRICE_PERCENTILE_MODE)
        source_id = percentile_source_id(
            percentile_source=PERCENTILE_SOURCE_EMIT,
            price_format_id=pf.id,
            scope="regional",
            source_key=price_list.source_key,
            region="Aktau",
            competitor="Emit",
            percentile=10,
        )
        db.add(
            CompetitorPricePercentile(
                price_format_id=pf.id,
                product_id=product.id,
                competitor_price_list_id=price_list.id,
                source_type="emit",
                source_key=price_list.source_key,
                branch_name="Aktau",
                competitor_name="Emit",
                percentile_scope="regional",
                percentile=10,
                value=Decimal("100.00"),
                source_count=1,
            )
        )
        db.add(
            CompetitorPrice(
                price_format_id=pf.id,
                source_name=f"percentile:{source_id}",
                supplier="Emit",
                coefficient=1,
            )
        )
        refresh_emit_percentile_source_summaries(db=db, price_format_id=pf.id)
        db.commit()

    regular_query_count = 0

    def count_regular_queries(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal regular_query_count
        if "regular_competitor_price_percentiles" in str(statement).lower():
            regular_query_count += 1

    engine = Session.kw["bind"]
    event.listen(engine, "before_cursor_execute", count_regular_queries)
    main.app.dependency_overrides[main.get_db] = lambda: Session()
    try:
        response = TestClient(main.app).get("/api/price-formats/EMIT-ONLY/competitor-assignments?include_summary=1")
    finally:
        main.app.dependency_overrides.clear()
        event.remove(engine, "before_cursor_execute", count_regular_queries)

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["activePhysicalPlkCount"] == 1
    assert payload["summary"]["percentileSourceCount"] == 1
    assert regular_query_count == 0


def test_price_list_counters_match_live_aggregate_semantics():
    db = _session()
    pf = _format(db, code="COUNT-LIVE")
    product = _product(db)
    price_list = _price_list(db, pf, source_key="7:501", competitor="Amanat")
    rows = [
        CompetitorPriceListItem(price_list_id=price_list.id, product_id=product.id, distributor_price=Decimal("10")),
        CompetitorPriceListItem(price_list_id=price_list.id, provisor_goods_id=100, distributor_price=Decimal("20")),
        CompetitorPriceListItem(price_list_id=price_list.id, matched_sku="SKU-1", distributor_price=Decimal("30")),
        CompetitorPriceListItem(price_list_id=price_list.id, distributor_goods_id="D-1", distributor_price=Decimal("40")),
        CompetitorPriceListItem(price_list_id=price_list.id, distributor_goods_id="D-2", distributor_price=Decimal("0")),
        CompetitorPriceListItem(price_list_id=price_list.id, distributor_goods_id="D-3", distributor_price=Decimal("-1")),
        CompetitorPriceListItem(price_list_id=price_list.id, distributor_goods_id="D-4", distributor_price=None),
        CompetitorPriceListItem(price_list_id=price_list.id, distributor_price=Decimal("50")),
    ]
    db.add_all(rows)
    db.flush()

    live = live_price_list_item_counts(db=db, price_list_ids=[price_list.id])
    refresh_price_list_item_counters(db=db, price_list_ids=[price_list.id])

    assert live[int(price_list.id)] == (8, 4)
    assert int(price_list.items_count) == 8
    assert int(price_list.matched_positive_items_count) == 4


def test_competitor_price_lists_endpoint_uses_persisted_item_counters():
    import backend.app.main as main

    Session = _session_factory_static()
    with Session() as db:
        pf = _format(db, code="PLK-QCOUNT")
        price_list = _price_list(db, pf, source_key="7:601", competitor="Amanat")
        price_list.items_count = 12
        price_list.matched_positive_items_count = 9
        db.commit()

    item_table_reads = 0

    def count_item_reads(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal item_table_reads
        normalized = " ".join(str(statement).lower().split())
        if "from competitor_price_list_items" in normalized:
            item_table_reads += 1

    engine = Session.kw["bind"]
    event.listen(engine, "before_cursor_execute", count_item_reads)
    main.app.dependency_overrides[main.get_db] = lambda: Session()
    try:
        response = TestClient(main.app).get("/api/competitors/price-lists?format_code=PLK-QCOUNT")
    finally:
        main.app.dependency_overrides.clear()
        event.remove(engine, "before_cursor_execute", count_item_reads)

    assert response.status_code == 200
    assert response.json()[0]["itemsCount"] == 12
    assert item_table_reads == 0


def test_percentile_sources_endpoint_uses_persisted_summaries():
    import backend.app.main as main

    Session = _session_factory_static()
    with Session() as db:
        pf = _format(db, code="PCT-QCOUNT")
        product = _product(db)
        emit_list = _price_list(db, pf, source_key="emit:701", branch="Aktau", competitor="Emit")
        competitor_list = _price_list(db, pf, source_key="7:701", competitor="Amanat")
        _assign(db, pf, emit_list, percentile_mode=MULTI_PRICE_PERCENTILE_MODE)
        _assign(db, pf, competitor_list)
        identity = canonical_regular_competitor_identity("Amanat")
        db.add(
            CompetitorPricePercentile(
                price_format_id=pf.id,
                product_id=product.id,
                competitor_price_list_id=emit_list.id,
                source_type="emit",
                source_key=emit_list.source_key,
                branch_name="Aktau",
                competitor_name="Emit",
                percentile_scope="regional",
                percentile=10,
                value=Decimal("100.00"),
                source_count=1,
            )
        )
        db.add(
            RegularCompetitorPricePercentile(
                competitor_identity=identity,
                competitor_name="Amanat",
                product_id=product.id,
                percentile=10,
                value=Decimal("100.00"),
                source_count=1,
                sample_count=1,
            )
        )
        refresh_emit_percentile_source_summaries(db=db, price_format_id=pf.id)
        refresh_regular_percentile_source_summaries(db=db, competitor_identities={identity})
        db.commit()

    live_percentile_reads = 0

    def count_live_percentile_reads(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal live_percentile_reads
        normalized = f" {' '.join(str(statement).lower().split())} "
        if " from competitor_price_percentiles " in normalized or " from regular_competitor_price_percentiles " in normalized:
            live_percentile_reads += 1

    engine = Session.kw["bind"]
    event.listen(engine, "before_cursor_execute", count_live_percentile_reads)
    main.app.dependency_overrides[main.get_db] = lambda: Session()
    try:
        emit_response = TestClient(main.app).get("/api/competitors/percentiles?format_code=PCT-QCOUNT&percentile_source=emit")
        competitor_response = TestClient(main.app).get(
            "/api/competitors/percentiles?format_code=PCT-QCOUNT&percentile_source=competitor"
        )
    finally:
        main.app.dependency_overrides.clear()
        event.remove(engine, "before_cursor_execute", count_live_percentile_reads)

    assert emit_response.status_code == 200
    assert competitor_response.status_code == 200
    assert emit_response.json()[0]["skuCount"] == 1
    assert competitor_response.json()[0]["skuCount"] == 1
    assert live_percentile_reads == 0


def test_emit_percentile_source_summaries_match_live_aggregate():
    db = _session()
    pf = _format(db, code="EMIT-SUMMARY")
    product_a = _product(db, code="SKU-A", goods_id=101)
    product_b = _product(db, code="SKU-B", goods_id=102)
    source = _price_list(db, pf, source_key="emit:501", branch="Aktau", competitor="Emit", account_id="emit")
    for product, value in [(product_a, Decimal("100")), (product_b, None)]:
        db.add(
            CompetitorPricePercentile(
                price_format_id=pf.id,
                product_id=product.id,
                competitor_price_list_id=source.id,
                source_type="emit",
                source_key=source.source_key,
                branch_name="Aktau",
                competitor_name="Emit",
                percentile_scope="regional",
                percentile=10,
                value=value,
                source_count=2,
            )
        )
    db.flush()

    live = live_emit_percentile_source_summary_rows(db=db, price_format_id=pf.id)
    written = refresh_emit_percentile_source_summaries(db=db, price_format_id=pf.id)
    stored = db.execute(select(CompetitorPricePercentileSourceSummary)).scalars().all()

    assert written == 1
    assert len(live) == len(stored) == 1
    assert stored[0].sku_count == live[0]["sku_count"] == 2
    assert stored[0].source_count == live[0]["source_count"] == 4


def test_regular_percentile_source_summaries_match_live_aggregate():
    db = _session()
    pf = _format(db, code="REG-SUMMARY")
    product_a = _product(db, code="SKU-A", goods_id=101)
    product_b = _product(db, code="SKU-B", goods_id=102)
    identity = canonical_regular_competitor_identity("Amanat")
    for product in (product_a, product_b):
        db.add(
            RegularCompetitorPricePercentile(
                competitor_identity=identity,
                competitor_name="Amanat",
                product_id=product.id,
                percentile=20,
                value=Decimal("120.00"),
                source_count=3,
                sample_count=3,
            )
        )
    db.flush()

    live = live_regular_percentile_source_summary_rows(db=db, competitor_identities={identity})
    written = refresh_regular_percentile_source_summaries(db=db, competitor_identities={identity})
    stored = db.execute(select(RegularCompetitorPricePercentileSourceSummary)).scalars().all()

    assert written == 1
    assert len(live) == len(stored) == 1
    assert stored[0].sku_count == live[0]["sku_count"] == 2
    assert stored[0].source_count == live[0]["source_count"] == 6


def test_ordinary_provisor_refresh_recalculates_global_regular_percentiles():
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
            select(RegularCompetitorPricePercentile)
            .where(RegularCompetitorPricePercentile.competitor_identity == "amanat")
            .where(RegularCompetitorPricePercentile.product_id == product.id)
        )
        .scalars()
        .all()
    )
    values = {int(row.percentile): float(row.value) for row in rows if row.value is not None}
    assert summary["regularPercentiles"]["regularPercentileRowsWritten"] > 0
    assert values[10] == pytest.approx(86.0)
    assert values[60] == pytest.approx(116.0)
    assert sorted(values) == [10, 20, 30, 40, 60]
    assert not db.execute(
        select(CompetitorPricePercentile).where(CompetitorPricePercentile.source_key.in_(["7:302", "7:303"]))
    ).first()


def test_regular_competitor_alias_registry_is_controlled():
    assert canonical_regular_competitor_identity("Аманат (Актау)") == "аманат"
    assert canonical_regular_competitor_identity("Аманат (Астана)") == "аманат"
    assert canonical_regular_competitor_identity("Медсервис (Алматы)") == "медсервис"
    assert canonical_regular_competitor_identity("Медсервис (Шымкент)") == "медсервис"
    assert canonical_regular_competitor_identity("Атамирас ТОО (Астана)") == "атамирас"
    assert canonical_regular_competitor_identity("Зерде ТОО НПО (Костанай)") == "зерде"
    assert canonical_regular_competitor_identity("Стофарм (Алматы)") == "стофарм"
    assert canonical_regular_competitor_identity("Стофарм средняя цена") == "стофарм средняя цена"
    assert canonical_regular_competitor_identity("Unknown (Brand)") == "unknown(brand)"
    assert canonical_regular_competitor_identity("Emit") == "emit"


def test_regular_aliases_rebuild_from_union_and_delete_obsolete_rows():
    db = _session()
    pf = _format(db, code="ALIASES")
    product = _product(db)
    actau = _price_list(db, pf, source_key="amanat:aktau", branch="Aktau", competitor="Аманат (Актау)")
    astana = _price_list(db, pf, source_key="amanat:astana", branch="Astana", competitor="Аманат (Астана)")
    db.add(CompetitorPriceListItem(price_list_id=actau.id, product_id=product.id, distributor_price=Decimal("100")))
    db.add(CompetitorPriceListItem(price_list_id=astana.id, product_id=product.id, distributor_price=Decimal("200")))
    db.add(
        RegularCompetitorPricePercentile(
            competitor_identity="аманат(актау)",
            competitor_name="Аманат (Актау)",
            product_id=product.id,
            percentile=10,
            value=Decimal("1"),
        )
    )
    db.commit()

    recalculate_percentiles_for_price_lists(db=db, competitor_price_list_ids=[actau.id])
    db.commit()

    rows = db.execute(select(RegularCompetitorPricePercentile)).scalars().all()
    identities = {row.competitor_identity for row in rows}
    values = {int(row.percentile): float(row.value) for row in rows if row.competitor_identity == "аманат"}
    assert identities == {"аманат"}
    assert values[10] == pytest.approx(110.0)
    assert values[60] == pytest.approx(160.0)
    assert {int(row.sample_count) for row in rows} == {2}
    assert {int(row.source_count) for row in rows} == {2}


def test_regular_rows_prefer_api_identity_over_stale_source_key():
    db = _session()
    pf = _format(db, code="REGSEL")
    med_product_1 = _product(db, code="MED-1", goods_id=201)
    med_product_2 = _product(db, code="MED-2", goods_id=202)
    ak_product = _product(db, code="AK-1", goods_id=301)
    med = _price_list(db, pf, source_key="med:almaty", competitor="Медсервис (Алматы)")
    akniet = _price_list(db, pf, source_key="ak:aktau", competitor="Ак-Ниет (Актау)")
    db.add(CompetitorPriceListItem(price_list_id=med.id, product_id=med_product_1.id, distributor_price=Decimal("100")))
    db.add(CompetitorPriceListItem(price_list_id=med.id, product_id=med_product_2.id, distributor_price=Decimal("200")))
    db.add(CompetitorPriceListItem(price_list_id=akniet.id, product_id=ak_product.id, distributor_price=Decimal("1")))
    db.commit()
    recalculate_percentiles_for_price_lists(db=db, competitor_price_list_ids=[med.id, akniet.id])
    db.commit()

    page = list_percentile_product_rows(
        db=db,
        price_format_code=pf.code,
        percentile_source=PERCENTILE_SOURCE_COMPETITOR,
        api_identity="regular:медсервис",
        competitor="Медсервис",
        source_key="ак-ниет(актау)",
    )

    assert page["selectedApiIdentity"] == "regular:медсервис"
    assert page["selectedSourceKey"] == "медсервис"
    assert page["selectedCompetitor"] == "Медсервис"
    assert page["total"] == 2
    assert {row["sku"] for row in page["items"]} == {"MED-1", "MED-2"}


def test_regular_rows_prefer_competitor_over_stale_legacy_source_key():
    db = _session()
    pf = _format(db, code="REGLEGACY")
    med_product = _product(db, code="MED-ONLY", goods_id=401)
    ak_product = _product(db, code="AK-ONLY", goods_id=402)
    med = _price_list(db, pf, source_key="med:astana", competitor="Медсервис (Астана)")
    akniet = _price_list(db, pf, source_key="ak:aktau", competitor="Ак-Ниет (Актау)")
    db.add(CompetitorPriceListItem(price_list_id=med.id, product_id=med_product.id, distributor_price=Decimal("100")))
    db.add(CompetitorPriceListItem(price_list_id=akniet.id, product_id=ak_product.id, distributor_price=Decimal("1")))
    db.commit()
    recalculate_percentiles_for_price_lists(db=db, competitor_price_list_ids=[med.id, akniet.id])
    db.commit()

    page = list_percentile_product_rows(
        db=db,
        price_format_code=pf.code,
        percentile_source=PERCENTILE_SOURCE_COMPETITOR,
        competitor="Медсервис",
        source_key="ак-ниет(актау)",
    )

    assert page["selectedApiIdentity"] == "regular:медсервис"
    assert page["selectedSourceKey"] == "медсервис"
    assert page["total"] == 1
    assert page["items"][0]["sku"] == "MED-ONLY"


def test_obsolete_regular_alias_groups_are_hidden_when_canonical_exists():
    db = _session()
    pf = _format(db, code="HIDEALIAS")
    product = _product(db)
    db.add_all(
        [
            RegularCompetitorPricePercentile(
                competitor_identity="медсервис",
                competitor_name="Медсервис",
                product_id=product.id,
                percentile=10,
                value=Decimal("100"),
            ),
            RegularCompetitorPricePercentile(
                competitor_identity="медсервис (алматы)",
                competitor_name="Медсервис (Алматы)",
                product_id=product.id,
                percentile=10,
                value=Decimal("1"),
            ),
            RegularCompetitorPricePercentile(
                competitor_identity="unknown(brand)",
                competitor_name="Unknown (Brand)",
                product_id=product.id,
                percentile=10,
                value=Decimal("2"),
            ),
            RegularCompetitorPricePercentile(
                competitor_identity="стофарм средняя цена",
                competitor_name="Стофарм средняя цена",
                product_id=product.id,
                percentile=10,
                value=Decimal("3"),
            ),
        ]
    )
    db.commit()

    groups = list_percentile_product_rows(
        db=db,
        price_format_code=pf.code,
        percentile_source=PERCENTILE_SOURCE_COMPETITOR,
    )["groups"]
    keys = {group["sourceKey"] for group in groups}

    assert "медсервис" in keys
    assert "медсервис (алматы)" not in keys
    assert "unknown(brand)" in keys
    assert "стофарм средняя цена" in keys


def test_regular_rebuild_deletes_actual_old_alias_identity_and_whitespace_variant():
    db = _session()
    pf = _format(db, code="DELALIAS")
    product = _product(db)
    med = _price_list(db, pf, source_key="med:kokshetau", competitor="Медсервис (Кокшетау)")
    db.add(CompetitorPriceListItem(price_list_id=med.id, product_id=product.id, distributor_price=Decimal("100")))
    db.add(
        RegularCompetitorPricePercentile(
            competitor_identity="медсервис (кокшетау)",
            competitor_name="Медсервис (Кокшетау)",
            product_id=product.id,
            percentile=10,
            value=Decimal("1"),
        )
    )
    db.add(
        RegularCompetitorPricePercentile(
            competitor_identity="медсервис(кокшетау)",
            competitor_name="Медсервис(Кокшетау)",
            product_id=product.id,
            percentile=20,
            value=Decimal("2"),
        )
    )
    db.commit()

    summary = recalculate_percentiles_for_price_lists(db=db, competitor_price_list_ids=[med.id])
    db.commit()

    identities = {row.competitor_identity for row in db.execute(select(RegularCompetitorPricePercentile)).scalars().all()}
    assert identities == {"медсервис"}
    assert summary["regularPercentiles"]["totalAliasRowsDeleted"] == 2


def test_regular_competitor_percentiles_are_global_across_regions_and_lists():
    db = _session()
    pf_a = _format(db, code="REGA")
    pf_b = _format(db, code="REGB")
    product = _product(db, code="000000000001004540", goods_id=1004540)
    other_product = _product(db, code="SKU-OTHER", goods_id=200)
    prices = [
        Decimal("2270"),
        Decimal("2015"),
        Decimal("1299.2"),
        Decimal("1912.54"),
        Decimal("1912.54"),
        Decimal("1299.2"),
        Decimal("2015"),
        Decimal("2090.72"),
        Decimal("1898.02"),
        Decimal("1886.26"),
    ]
    lists = []
    for idx, price in enumerate(prices):
        pf = pf_a if idx % 2 == 0 else pf_b
        price_list = _price_list(
            db,
            pf,
            source_key=f"atamiras:{idx}",
            branch=f"Region {idx % 4}",
            competitor="Атамирас",
            account_id=str(idx % 3),
            external_price_list_id=str(1000 + idx),
        )
        lists.append(price_list)
        db.add(
            CompetitorPriceListItem(
                price_list_id=price_list.id,
                product_id=product.id,
                distributor_price=price,
            )
        )
    inactive = _price_list(db, pf_a, source_key="atamiras:inactive", branch="Inactive", competitor="Атамирас")
    inactive.last_refresh_status = "failed"
    db.add(CompetitorPriceListItem(price_list_id=inactive.id, product_id=product.id, distributor_price=Decimal("1")))
    db.add(CompetitorPriceListItem(price_list_id=lists[0].id, product_id=product.id, distributor_price=Decimal("0")))
    db.add(CompetitorPriceListItem(price_list_id=lists[0].id, product_id=product.id, distributor_price=Decimal("-5")))
    db.add(CompetitorPriceListItem(price_list_id=lists[0].id, product_id=None, distributor_price=Decimal("777")))
    other_competitor = _price_list(db, pf_a, source_key="other:1", competitor="Other")
    db.add(CompetitorPriceListItem(price_list_id=other_competitor.id, product_id=product.id, distributor_price=Decimal("10")))
    db.add(CompetitorPriceListItem(price_list_id=lists[0].id, product_id=other_product.id, distributor_price=Decimal("50")))
    db.commit()

    summary = recalculate_percentiles_for_price_lists(db=db, competitor_price_list_ids=[lists[0].id])
    db.commit()

    rows = (
        db.execute(
            select(RegularCompetitorPricePercentile)
            .where(RegularCompetitorPricePercentile.competitor_identity == "атамирас")
            .where(RegularCompetitorPricePercentile.product_id == product.id)
        )
        .scalars()
        .all()
    )
    values = {int(row.percentile): Decimal(str(row.value)) for row in rows}
    assert summary["regularPercentiles"]["regularCompetitorsProcessed"] == 1
    assert values[10] == pytest.approx(Decimal("1299.20"))
    assert values[20] == pytest.approx(Decimal("1768.848"))
    assert values[30] == pytest.approx(Decimal("1894.492"))
    assert values[40] == pytest.approx(Decimal("1906.732"))
    assert values[60] == pytest.approx(Decimal("1953.524"))
    assert {int(row.sample_count) for row in rows} == {10}
    assert {int(row.source_count) for row in rows} == {10}


def test_regular_percentile_calculation_does_not_require_price_format_assignment():
    db = _session()
    pf = _format(db, code="NOASSIGN")
    product = _product(db)
    first = _price_list(db, pf, source_key="amanat:1", competitor="Amanat")
    second = _price_list(db, pf, source_key="amanat:2", competitor="Amanat")
    db.add(CompetitorPriceListItem(price_list_id=first.id, product_id=product.id, distributor_price=Decimal("100")))
    db.add(CompetitorPriceListItem(price_list_id=second.id, product_id=product.id, distributor_price=Decimal("200")))
    db.commit()

    recalculate_percentiles_for_price_lists(db=db, competitor_price_list_ids=[first.id])
    db.commit()

    row = db.execute(
        select(RegularCompetitorPricePercentile)
        .where(RegularCompetitorPricePercentile.competitor_identity == "amanat")
        .where(RegularCompetitorPricePercentile.product_id == product.id)
        .where(RegularCompetitorPricePercentile.percentile == 60)
    ).scalar_one()
    assert float(row.value) == pytest.approx(160.0)
    assert int(row.sample_count) == 2


def test_two_price_formats_read_same_regular_global_dataset_at_different_percentiles():
    db = _session()
    pf_a = _format(db, code="PFA")
    pf_b = _format(db, code="PFB")
    product = _product(db)
    price_list = _price_list(db, pf_a, source_key="global:1", competitor="GlobalComp")
    _assign(db, pf_a, price_list)
    _assign(db, pf_b, price_list)
    for price in (Decimal("100"), Decimal("200"), Decimal("300")):
        db.add(CompetitorPriceListItem(price_list_id=price_list.id, product_id=product.id, distributor_price=price))
    db.commit()
    recalculate_percentiles_for_price_lists(db=db, competitor_price_list_ids=[price_list.id])
    db.flush()

    db.add(
        CompetitorPrice(
            price_format_id=pf_a.id,
            product_id=None,
            source_name=_regular_percentile_config_name(pf_a.id, "globalcomp", "GlobalComp", 20),
            supplier="GlobalComp",
            coefficient=1,
        )
    )
    db.add(
        CompetitorPrice(
            price_format_id=pf_b.id,
            product_id=None,
            source_name=_regular_percentile_config_name(pf_b.id, "globalcomp", "GlobalComp", 40),
            supplier="GlobalComp",
            coefficient=1,
        )
    )
    db.commit()

    cache_a = load_percentile_price_cache(db, pf_a.id)
    cache_b = load_percentile_price_cache(db, pf_b.id)
    resolved_a = resolve_percentile_prices_from_cache(cache_a, product.id, percentile_number=20)
    resolved_b = resolve_percentile_prices_from_cache(cache_b, product.id, percentile_number=40)

    assert len(db.execute(select(RegularCompetitorPricePercentile)).scalars().all()) == 5
    assert float(resolved_a.prices[0][0]) == pytest.approx(140.0)
    assert float(resolved_b.prices[0][0]) == pytest.approx(180.0)


def test_regular_percentile_cache_uses_selected_config_source_name_when_display_differs():
    db = _session()
    pf = _format(db, code="REGKEY")
    product = _product(db)
    selected_source = _regular_percentile_config_name(pf.id, "медсервис", "Медсервис", 30)
    db.add(
        CompetitorPrice(
            price_format_id=pf.id,
            product_id=None,
            source_name=selected_source,
            supplier="Медсервис - P30",
            coefficient=1,
        )
    )
    db.add(
        RegularCompetitorPricePercentile(
            competitor_identity="медсервис",
            competitor_name="Медсервис Алматы",
            product_id=product.id,
            percentile=30,
            value=Decimal("1595.17"),
            sample_count=5,
            source_count=1,
        )
    )
    db.commit()

    cache = load_percentile_price_cache(db, pf.id)
    resolved = resolve_percentile_prices_from_cache(cache, product.id, percentile_number=30)

    assert len(resolved.prices) == 1
    assert resolved.prices[0][1] == selected_source


def test_old_regular_alias_percentile_config_reads_canonical_global_dataset():
    db = _session()
    pf = _format(db, code="OLDCFG")
    product = _product(db)
    price_list = _price_list(db, pf, source_key="amanat:aktau", competitor="Аманат (Актау)")
    db.add(CompetitorPriceListItem(price_list_id=price_list.id, product_id=product.id, distributor_price=Decimal("100")))
    db.add(CompetitorPriceListItem(price_list_id=price_list.id, product_id=product.id, distributor_price=Decimal("200")))
    db.commit()
    recalculate_percentiles_for_price_lists(db=db, competitor_price_list_ids=[price_list.id])
    db.flush()

    db.add(
        CompetitorPrice(
            price_format_id=pf.id,
            product_id=None,
            source_name=_regular_percentile_config_name(pf.id, "аманат(актау)", "Аманат (Актау)", 20),
            supplier="Аманат (Актау)",
            coefficient=1,
        )
    )
    db.commit()

    cache = load_percentile_price_cache(db, pf.id)
    resolved = resolve_percentile_prices_from_cache(cache, product.id, percentile_number=20)

    assert len(resolved.prices) == 1
    assert float(resolved.prices[0][0]) == pytest.approx(120.0)
    assert ":аманат::" in resolved.prices[0][1]


def test_old_regional_regular_percentile_rows_are_not_selected_for_pricing():
    db = _session()
    pf = _format(db, code="OLDREG")
    product = _product(db)
    price_list = _price_list(db, pf, source_key="old:1", competitor="OldRegular")
    _assign(db, pf, price_list, percentile_mode=MULTI_PRICE_PERCENTILE_MODE)
    db.add(
        CompetitorPricePercentile(
            price_format_id=pf.id,
            product_id=product.id,
            competitor_price_list_id=price_list.id,
            source_type="provisor",
            source_key="old:1",
            branch_name=price_list.branch_name,
            competitor_name="OldRegular",
            percentile_scope="regional",
            percentile=10,
            value=Decimal("55"),
        )
    )
    db.commit()

    cache = load_percentile_price_cache(db, pf.id)
    assert resolve_percentile_prices_from_cache(cache, product.id, percentile_number=10).prices == []


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
    assert {row["sourceKey"] for row in sources} == {"amanat"}

    page = list_percentile_product_rows(
        db=db,
        price_format_code=pf.code,
        competitor="Amanat",
        source_key="amanat",
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
        source_key="amanat",
        percentile_source=PERCENTILE_SOURCE_COMPETITOR,
    )
    assert [group["sourceKey"] for group in hidden["groups"]] == ["amanat"]
    assert hidden["summary"]["productsWithPercentile"] == 1


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
        refresh_emit_percentile_source_summaries(db=db, price_format_id=pf.id)
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


def test_regular_percentile_assignment_availability_uses_canonical_dataset_without_physical_assignment():
    db = _session()
    pf = _format(db, code="REG-AVAILABLE")
    product = _product(db)
    db.add(
        RegularCompetitorPricePercentile(
            competitor_identity="медсервис",
            competitor_name="Медсервис Алматы",
            product_id=product.id,
            percentile=30,
            value=Decimal("1595.17"),
            sample_count=5,
            source_count=1,
        )
    )
    refresh_regular_percentile_source_summaries(db=db)
    db.commit()

    sources = list_percentile_sources(
        db=db,
        price_format_code=pf.code,
        percentile_source=PERCENTILE_SOURCE_COMPETITOR,
        include_ineligible=True,
    )

    row = next(item for item in sources if item["sourceKey"] == "медсервис" and item["percentile"] == 30)
    assert row["eligibleForPricing"] is True
    assert row["pricingEligibilityReason"] == ""


def test_regular_percentile_assignment_missing_dataset_is_unavailable():
    import backend.app.main as main

    Session = _session_factory_static()
    with Session() as db:
        pf = _format(db, code="REG-MISSING")
        db.add(
            CompetitorPrice(
                price_format_id=pf.id,
                product_id=None,
                source_name=_regular_percentile_config_name(pf.id, "медсервис", "Медсервис", 30),
                supplier="Медсервис - P30",
                coefficient=1,
            )
        )
        db.commit()

    main.app.dependency_overrides[main.get_db] = lambda: Session()
    try:
        response = TestClient(main.app).get("/api/price-formats/REG-MISSING/competitor-assignments")
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    percentile_rows = [row for row in response.json() if row["assignmentKind"] == "percentile_config"]
    assert len(percentile_rows) == 1
    assert percentile_rows[0]["sourceKey"].startswith("competitor:")
    assert percentile_rows[0]["eligibleForPricing"] is False
    assert percentile_rows[0]["pricingEligibilityReason"] == "regular_percentile_dataset_missing"


def test_emit_assignment_availability_still_requires_active_emit_assignment():
    db = _session()
    pf = _format(db, code="EMIT-UNCHANGED")
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
    refresh_emit_percentile_source_summaries(db=db, price_format_id=pf.id)
    db.commit()

    visible = list_percentile_sources(db=db, price_format_code=pf.code, percentile_source=PERCENTILE_SOURCE_EMIT)
    assert visible[0]["eligibleForPricing"] is True

    assignment.is_active = False
    db.commit()
    hidden = list_percentile_sources(db=db, price_format_code=pf.code, percentile_source=PERCENTILE_SOURCE_EMIT)
    inactive = list_percentile_sources(
        db=db,
        price_format_code=pf.code,
        percentile_source=PERCENTILE_SOURCE_EMIT,
        include_ineligible=True,
    )

    assert hidden == []
    assert inactive[0]["eligibleForPricing"] is False
    assert inactive[0]["pricingEligibilityReason"] == "no_active_physical_emit_assignment"
