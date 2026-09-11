from datetime import date

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app import main
from backend.app.deps import get_current_user
from backend.app.models import (
    AppUser,
    Base,
    CalculatedPrice,
    CompetitorCodeMapping,
    CompetitorPriceList,
    CompetitorPriceListItem,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    PriceList,
    Product,
    ProductExtra,
)
from backend.app.services.competitors import code_mappings as code_mappings_service
from backend.app.main import create_competitor_code_mapping, unmap_competitor_code_mapping
from backend.app.services.competitors.code_mappings import (
    auto_match_product_catalog_code_mappings,
    list_catalog_code_mappings,
    list_product_catalog_code_mappings,
    product_catalog_candidates_for_product,
    source_match_key,
)


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _admin() -> AppUser:
    return AppUser(username="admin", role="admin", is_active=True)


def _viewer() -> AppUser:
    return AppUser(username="viewer", role="viewer", is_active=True)


def _price_format(db: Session, code: str = "TEST") -> PriceFormat:
    row = PriceFormat(code=code, name=code, branch="Test branch")
    db.add(row)
    db.flush()
    return row


def _price_list(db: Session, pf: PriceFormat, *, source_key: str, price_date: date) -> CompetitorPriceList:
    row = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key=source_key,
        supplier=source_key,
        display_name=source_key,
        price_date=price_date,
    )
    db.add(row)
    db.flush()
    db.add(PriceFormatCompetitorAssignment(price_format_id=pf.id, competitor_price_list_id=row.id, is_active=True))
    db.flush()
    return row


def _generated_price_list(db: Session, pf: PriceFormat, product: Product, *, number: str) -> PriceList:
    row = PriceList(number=number, price_format_id=pf.id)
    db.add(row)
    db.flush()
    db.add(
        CalculatedPrice(
            price_list_id=row.id,
            product_id=product.id,
            cost=1,
            base_price=1,
            final_price=1,
        )
    )
    db.flush()
    return row


def _item(
    db: Session,
    price_list: CompetitorPriceList,
    goods_id: int,
    *,
    name: str = "Source product",
    manufacturer: str = "Source maker",
    distributor_goods_id: str = "",
    product_id: int | None = None,
    matched_sku: str = "",
) -> CompetitorPriceListItem:
    row = CompetitorPriceListItem(
        price_list_id=price_list.id,
        product_id=product_id,
        provisor_goods_id=goods_id,
        name=name,
        raw_name=name,
        raw_manufacturer=manufacturer,
        distributor_goods_id=distributor_goods_id,
        match_type="sku" if product_id or matched_sku else "unmatched",
        matched_sku=matched_sku,
        match_score=100 if product_id or matched_sku else None,
    )
    db.add(row)
    db.flush()
    return row


def _product(db: Session, code: str, name: str, manufacturer: str = "") -> Product:
    row = Product(code=code, name=name, cost=1)
    db.add(row)
    db.flush()
    db.add(ProductExtra(product_id=row.id, manufacturer=manufacturer))
    db.flush()
    return row


def _candidate_items(db: Session, source: CompetitorPriceListItem, pf: PriceFormat) -> list[dict]:
    db.commit()
    result = list_catalog_code_mappings(
        db=db,
        platform="provisor",
        price_format_id=pf.id,
        status="unmapped",
        source_q=str(source.provisor_goods_id),
        page=1,
        limit=1,
        include_candidates=True,
    )
    assert result["pagination"]["total"] == 1
    return result["items"][0]["candidates"]


def test_unmatched_provisor_catalog_is_source_first_and_deduplicates_goods_id():
    db = _session()
    pf = _price_format(db)
    older = _price_list(db, pf, source_key="older", price_date=date(2026, 1, 1))
    newer = _price_list(db, pf, source_key="newer", price_date=date(2026, 2, 1))
    product = Product(code="SKU-MAPPED", name="Mapped", cost=1)
    db.add(product)
    db.flush()

    _item(db, older, 10, name="Old duplicate")
    latest = _item(db, newer, 10, name="Latest duplicate")
    open_row = _item(db, newer, 20, name="Open source")
    _item(db, newer, 30, name="Auto product", product_id=product.id)
    _item(db, newer, 40, name="Auto sku", matched_sku="SKU-MAPPED")
    db.add(
        CompetitorCodeMapping(
            platform="provisor",
            source_external_key="50",
            source_match_key=source_match_key(platform="provisor", source_external_key=50),
            source_name="Manual mapped",
            status="mapped",
            our_product_id=product.id,
            our_sku=product.code,
        )
    )
    db.add(
        CompetitorCodeMapping(
            platform="provisor",
            source_external_key="60",
            source_match_key=source_match_key(platform="provisor", source_external_key=60),
            source_name="Rejected",
            status="rejected",
        )
    )
    _item(db, newer, 50, name="Manual mapped")
    _item(db, newer, 60, name="Rejected")
    db.commit()

    result = list_catalog_code_mappings(
        db=db,
        platform="provisor",
        price_format_id=pf.id,
        status="unmapped",
        page=1,
        limit=50,
        include_candidates=False,
    )

    assert result["pagination"] == {"page": 1, "pageSize": 50, "total": 4, "pageCount": 1}
    assert [row["sourceExternalKey"] for row in result["items"]] == ["10", "20", "30", "40"]
    assert result["items"][0]["itemId"] == latest.id
    assert result["items"][0]["sourceName"] == "Latest duplicate"
    assert result["items"][1]["itemId"] == open_row.id
    assert result["items"][0]["ourProductId"] is None
    assert result["metrics"][0]["total"] == 6
    assert result["metrics"][0]["mapped"] == 1
    assert result["metrics"][0]["rejected"] == 1
    assert result["metrics"][0]["unmapped"] == 4


def test_provisor_catalog_assigned_format_returns_unmapped_by_global_mapping_only():
    db = _session()
    pf = _price_format(db, "003")
    price_list = _price_list(db, pf, source_key="assigned-003", price_date=date(2026, 1, 1))
    product = _product(db, "MAPPED-SKU", "Mapped product")
    for goods_id in range(1, 8):
        _item(db, price_list, goods_id, name=f"Source {goods_id}")
    db.add(
        CompetitorCodeMapping(
            platform="provisor",
            source_external_key="4",
            source_match_key=source_match_key(platform="provisor", source_external_key=4),
            source_name="Source 4",
            status="mapped",
            our_product_id=product.id,
            our_sku=product.code,
        )
    )
    db.commit()

    result = list_catalog_code_mappings(
        db=db,
        platform="provisor",
        price_format_id=pf.id,
        status="unmapped",
        page=1,
        limit=50,
        include_candidates=False,
    )

    assert result["pagination"] == {"page": 1, "pageSize": 50, "total": 6, "pageCount": 1}
    assert [row["sourceExternalKey"] for row in result["items"]] == ["1", "2", "3", "5", "6", "7"]
    assert result["metrics"][0]["total"] == 7
    assert result["metrics"][0]["mapped"] == 1
    assert result["metrics"][0]["unmapped"] == 6


def test_provisor_catalog_global_mode_deduplicates_across_price_lists():
    db = _session()
    pf_a = _price_format(db, "GLOBAL-A")
    pf_b = _price_format(db, "GLOBAL-B")
    older = _price_list(db, pf_a, source_key="older-global", price_date=date(2026, 1, 1))
    newer = _price_list(db, pf_b, source_key="newer-global", price_date=date(2026, 2, 1))
    _item(db, older, 100, name="Old duplicate")
    _item(db, newer, 100, name="New duplicate")
    _item(db, newer, 200, name="Second")
    db.commit()

    result = list_catalog_code_mappings(
        db=db,
        platform="provisor",
        price_format_id=None,
        status="unmapped",
        page=1,
        limit=50,
        include_candidates=False,
    )

    assert result["pagination"] == {"page": 1, "pageSize": 50, "total": 2, "pageCount": 1}
    assert [row["sourceExternalKey"] for row in result["items"]] == ["100", "200"]
    assert result["items"][0]["sourceName"] == "New duplicate"


def test_provisor_catalog_global_default_uses_bounded_query_count():
    db = _session()
    pf = _price_format(db, "QUERY-COUNT")
    price_list = _price_list(db, pf, source_key="query-count", price_date=date(2026, 1, 1))
    for goods_id in range(1, 101):
        _item(db, price_list, goods_id, name=f"Source {goods_id}")
    db.commit()

    select_count = 0

    def count_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        result = list_catalog_code_mappings(
            db=db,
            platform="provisor",
            price_format_id=None,
            status="unmapped",
            page=1,
            limit=50,
            include_candidates=False,
        )
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)

    assert result["pagination"] == {"page": 1, "pageSize": 50, "total": 100, "pageCount": 2}
    assert len(result["items"]) == 50
    assert select_count <= 3


def test_global_mapping_removes_goods_id_from_global_and_format_filtered_unmapped_views():
    db = _session()
    pf_a = _price_format(db, "MAP-A")
    pf_b = _price_format(db, "MAP-B")
    list_a = _price_list(db, pf_a, source_key="map-a", price_date=date(2026, 1, 1))
    list_b = _price_list(db, pf_b, source_key="map-b", price_date=date(2026, 1, 2))
    product = _product(db, "GLOBAL-MAPPED", "Globally mapped")
    _item(db, list_a, 555, name="Mapped in A")
    _item(db, list_b, 555, name="Mapped in B")
    _item(db, list_b, 777, name="Still open")
    db.add(
        CompetitorCodeMapping(
            platform="provisor",
            source_external_key="555",
            source_match_key=source_match_key(platform="provisor", source_external_key=555),
            source_name="Mapped",
            status="mapped",
            our_product_id=product.id,
            our_sku=product.code,
        )
    )
    db.commit()

    global_result = list_catalog_code_mappings(db=db, platform="provisor", price_format_id=None, status="unmapped", page=1, limit=50)
    filtered_a = list_catalog_code_mappings(db=db, platform="provisor", price_format_id=pf_a.id, status="unmapped", page=1, limit=50)
    filtered_b = list_catalog_code_mappings(db=db, platform="provisor", price_format_id=pf_b.id, status="unmapped", page=1, limit=50)

    assert [row["sourceExternalKey"] for row in global_result["items"]] == ["777"]
    assert filtered_a["pagination"]["total"] == 0
    assert [row["sourceExternalKey"] for row in filtered_b["items"]] == ["777"]


def test_price_format_with_zero_assignments_does_not_empty_global_default_queue():
    db = _session()
    pf_with_source = _price_format(db, "HAS-SOURCE")
    pf_without_source = _price_format(db, "NO-SOURCE")
    price_list = _price_list(db, pf_with_source, source_key="global-source", price_date=date(2026, 1, 1))
    _item(db, price_list, 900, name="Global source")
    db.commit()

    filtered = list_catalog_code_mappings(db=db, platform="provisor", price_format_id=pf_without_source.id, status="unmapped")
    global_result = list_catalog_code_mappings(db=db, platform="provisor", price_format_id=None, status="unmapped")

    assert filtered["pagination"]["total"] == 0
    assert [row["sourceExternalKey"] for row in global_result["items"]] == ["900"]


def test_auto_match_metadata_does_not_remove_source_from_manual_unmapped_queue():
    db = _session()
    pf = _price_format(db, "AUTO")
    price_list = _price_list(db, pf, source_key="auto", price_date=date(2026, 1, 1))
    product = _product(db, "AUTO-SKU", "Auto matched product")
    _item(db, price_list, 333, name="Auto product id", product_id=product.id, matched_sku=product.code)
    db.commit()

    result = list_catalog_code_mappings(db=db, platform="provisor", price_format_id=pf.id, status="unmapped", page=1, limit=50)

    assert result["pagination"]["total"] == 1
    assert result["items"][0]["sourceExternalKey"] == "333"
    assert result["items"][0]["ourProductId"] is None
    assert result["items"][0]["ourSku"] == ""
    assert result["metrics"][0]["mapped"] == 0
    assert result["metrics"][0]["unmapped"] == 1


def test_source_first_provisor_row_generates_exact_candidate():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="candidates", price_date=date(2026, 1, 1))
    source = _item(db, price_list, 1001, name="Аспирин таб 500 мг N10", manufacturer="Bayer")
    product = _product(db, "ASP-500-10", "Аспирин таб 500 мг N10", "Bayer")

    candidates = _candidate_items(db, source, pf)

    assert candidates
    assert candidates[0]["ourProductId"] == product.id
    assert candidates[0]["matchLevel"] == "exact"
    assert candidates[0]["manufacturerMismatch"] is False
    assert candidates[0]["sourceManufacturer"] == "Bayer"
    assert candidates[0]["internalManufacturer"] == "Bayer"


def test_source_first_provisor_row_generates_manufacturer_different_candidate():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="mfr-diff", price_date=date(2026, 1, 1))
    source = _item(db, price_list, 1002, name="Аспирин таб 500 мг N10", manufacturer="Bayer")
    product = _product(db, "ASP-OTHER", "Аспирин таб 500 мг N10", "Polpharma")

    candidates = _candidate_items(db, source, pf)

    assert [candidate["ourProductId"] for candidate in candidates] == [product.id]
    assert candidates[0]["matchLevel"] == "characteristics"
    assert candidates[0]["manufacturerMismatch"] is True
    assert candidates[0]["sourceManufacturer"] == "Bayer"
    assert candidates[0]["internalManufacturer"] == "Polpharma"


def test_exact_candidate_appears_before_manufacturer_different_candidate():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="candidate-order", price_date=date(2026, 1, 1))
    source = _item(db, price_list, 1003, name="Аспирин таб 500 мг N10", manufacturer="Bayer")
    diff = _product(db, "ASP-DIFF", "Аспирин таб 500 мг N10", "Polpharma")
    exact = _product(db, "ASP-EXACT", "Аспирин таб 500 мг N10", "Bayer")

    candidates = _candidate_items(db, source, pf)

    assert [candidate["ourProductId"] for candidate in candidates[:2]] == [exact.id, diff.id]
    assert [candidate["matchLevel"] for candidate in candidates[:2]] == ["exact", "characteristics"]


def test_different_dosage_is_not_characteristics_candidate():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="dosage", price_date=date(2026, 1, 1))
    source = _item(db, price_list, 1004, name="Аспирин таб 500 мг N10", manufacturer="Bayer")
    _product(db, "ASP-250", "Аспирин таб 250 мг N10", "Polpharma")

    assert _candidate_items(db, source, pf) == []


def test_different_volume_is_not_characteristics_candidate():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="volume", price_date=date(2026, 1, 1))
    source = _item(db, price_list, 1005, name="Аспирин сироп 500 мг 10 мл N10", manufacturer="Bayer")
    _product(db, "ASP-20ML", "Аспирин сироп 500 мг 20 мл N10", "Polpharma")

    assert _candidate_items(db, source, pf) == []


def test_different_dosage_form_is_not_characteristics_candidate():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="form", price_date=date(2026, 1, 1))
    source = _item(db, price_list, 1006, name="Аспирин таб 500 мг N10", manufacturer="Bayer")
    _product(db, "ASP-CAPS", "Аспирин капс 500 мг N10", "Polpharma")

    assert _candidate_items(db, source, pf) == []


def test_different_package_count_is_not_characteristics_candidate():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="quantity", price_date=date(2026, 1, 1))
    source = _item(db, price_list, 1007, name="Аспирин таб 500 мг N10", manufacturer="Bayer")
    _product(db, "ASP-N20", "Аспирин таб 500 мг N20", "Polpharma")

    assert _candidate_items(db, source, pf) == []


def test_no_candidate_row_still_supports_manual_product_search_endpoint():
    db = _session()
    pf = _price_format(db, "SEARCH")
    price_list = _price_list(db, pf, source_key="search-products", price_date=date(2026, 1, 1))
    source = _item(db, price_list, 1008, name="Неточный источник 10 мг N10", manufacturer="Source")
    product = _product(db, "MANUAL-1", "Ручной товар 1 мг N1", "Manual")
    db.commit()

    result = list_catalog_code_mappings(
        db=db,
        platform="provisor",
        price_format_id=pf.id,
        status="unmapped",
        source_q=str(source.provisor_goods_id),
        page=1,
        limit=1,
        include_candidates=True,
    )
    assert result["items"][0]["candidates"] == []

    def override_db():
        try:
            yield db
        finally:
            pass

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[get_current_user] = _admin
    try:
        client = TestClient(main.app)
        response = client.get("/api/products/search?q=MANUAL&limit=10")
        assert response.status_code == 200
        assert response.json()[0]["productId"] == product.id
    finally:
        main.app.dependency_overrides.clear()


def test_competitor_items_search_finds_provisor_goods_id_and_deduplicates():
    db = _session()
    pf = _price_format(db, "SEARCH-EXT")
    first_list = _price_list(db, pf, source_key="external-a", price_date=date(2026, 1, 1))
    second_list = _price_list(db, pf, source_key="external-b", price_date=date(2026, 1, 2))
    _item(db, first_list, 123456, name="Manual external older", manufacturer="Maker")
    _item(db, second_list, 123456, name="Manual external newer", manufacturer="Maker")
    for idx in range(35):
        _item(db, second_list, 200000 + idx, name=f"Bounded external {idx}", manufacturer="Maker")
    db.commit()

    def override_db():
        try:
            yield db
        finally:
            pass

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[get_current_user] = _admin
    try:
        client = TestClient(main.app)
        by_goods = client.get("/api/competitor-items/search?platform=provisor&q=123456&format_code=SEARCH-EXT&limit=30")
        assert by_goods.status_code == 200
        rows = by_goods.json()
        assert len(rows) == 1
        assert rows[0]["sourceExternalKey"] == "123456"
        assert rows[0]["sourceMatchKey"] == "provisor:123456"
        assert rows[0]["sourceName"] == "Manual external newer"

        bounded = client.get("/api/competitor-items/search?platform=provisor&q=Bounded&format_code=SEARCH-EXT&limit=30")
        assert bounded.status_code == 200
        assert len(bounded.json()) == 30
    finally:
        main.app.dependency_overrides.clear()


def test_provisor_catalog_paginates_distinct_goods_with_stable_ordering():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="page", price_date=date(2026, 1, 1))
    for goods_id in [30, 10, 50, 20, 40]:
        _item(db, price_list, goods_id, name=f"Product {goods_id}")
    db.commit()

    first = list_catalog_code_mappings(db=db, platform="provisor", price_format_id=pf.id, status="unmapped", page=1, limit=2)
    second = list_catalog_code_mappings(db=db, platform="provisor", price_format_id=pf.id, status="unmapped", page=2, limit=2)
    third = list_catalog_code_mappings(db=db, platform="provisor", price_format_id=pf.id, status="unmapped", page=3, limit=2)

    assert first["pagination"] == {"page": 1, "pageSize": 2, "total": 5, "pageCount": 3}
    assert [row["sourceExternalKey"] for row in first["items"]] == ["10", "20"]
    assert [row["sourceExternalKey"] for row in second["items"]] == ["30", "40"]
    assert [row["sourceExternalKey"] for row in third["items"]] == ["50"]


def test_provisor_catalog_searches_goods_id_name_manufacturer_and_distributor_id():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="search", price_date=date(2026, 1, 1))
    _item(db, price_list, 95822, name="\u0410\u0441\u043f\u0438\u0440\u0438\u043d \u043a\u0430\u0440\u0434\u0438\u043e", manufacturer="Bayer", distributor_goods_id="DIST-95822")
    _item(db, price_list, 12345, name="Other", manufacturer="Other maker", distributor_goods_id="OTHER")
    db.commit()

    for query in ["95822", "\u0410\u0441\u043f\u0438\u0440\u0438\u043d", "BAYER", "DIST-95822"]:
        result = list_catalog_code_mappings(
            db=db,
            platform="provisor",
            price_format_id=pf.id,
            status="unmapped",
            source_q=query,
            page=1,
            limit=50,
        )
        assert result["pagination"]["total"] == 1
        assert result["items"][0]["sourceExternalKey"] == "95822"

    partial_numeric = list_catalog_code_mappings(
        db=db,
        platform="provisor",
        price_format_id=pf.id,
        status="unmapped",
        source_q="582",
        page=1,
        limit=50,
    )
    assert partial_numeric["pagination"]["total"] == 0
    assert partial_numeric["items"] == []

    empty = list_catalog_code_mappings(
        db=db,
        platform="provisor",
        price_format_id=pf.id,
        status="unmapped",
        source_q="does-not-exist",
        page=1,
        limit=50,
    )
    assert empty["pagination"]["total"] == 0
    assert empty["items"] == []


def test_provisor_catalog_searches_mapped_rows_by_our_sku():
    db = _session()
    pf = _price_format(db, "SKU-SEARCH")
    price_list = _price_list(db, pf, source_key="sku-search", price_date=date(2026, 1, 1))
    product = _product(db, "SKU-777", "Internal product")
    _item(db, price_list, 777, name="Mapped source")
    db.add(
        CompetitorCodeMapping(
            platform="provisor",
            source_external_key="777",
            source_match_key=source_match_key(platform="provisor", source_external_key=777),
            source_name="Mapped source",
            status="mapped",
            our_product_id=product.id,
            our_sku=product.code,
        )
    )
    db.commit()

    result = list_catalog_code_mappings(
        db=db,
        platform="provisor",
        price_format_id=None,
        status="mapped",
        product_q="SKU-777",
        page=1,
        limit=50,
        include_candidates=False,
    )

    assert result["pagination"]["total"] == 1
    assert result["items"][0]["sourceExternalKey"] == "777"
    assert result["items"][0]["ourSku"] == "SKU-777"


def test_provisor_catalog_only_includes_assigned_price_lists_for_format():
    db = _session()
    pf = _price_format(db, "A")
    other_pf = _price_format(db, "B")
    assigned = _price_list(db, pf, source_key="assigned", price_date=date(2026, 1, 1))
    other = _price_list(db, other_pf, source_key="other", price_date=date(2026, 1, 1))
    _item(db, assigned, 100, name="Assigned")
    _item(db, other, 200, name="Other")
    db.commit()

    result = list_catalog_code_mappings(db=db, platform="provisor", price_format_id=pf.id, status="unmapped")

    assert result["pagination"]["total"] == 1
    assert result["items"][0]["sourceExternalKey"] == "100"


def test_mapping_save_uses_selected_product_and_row_disappears_from_unmapped_list():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="save", price_date=date(2026, 1, 1))
    source = _item(db, price_list, 777, name="Source")
    correct = Product(code="CORRECT", name="Correct", cost=1)
    wrong = Product(code="WRONG", name="Wrong", cost=1)
    db.add_all([correct, wrong])
    db.commit()

    before = list_catalog_code_mappings(db=db, platform="provisor", price_format_id=pf.id, status="unmapped")
    assert before["pagination"]["total"] == 1

    saved = create_competitor_code_mapping(
        {
            "platform": "provisor",
            "status": "mapped",
            "formatCode": pf.code,
            "itemId": source.id,
            "sourceMatchKey": source_match_key(platform="provisor", source_external_key=777),
            "ourProductId": correct.id,
        },
        db,
        _admin(),
    )
    db.refresh(correct)
    db.refresh(wrong)
    db.refresh(source)

    assert saved["ourProductId"] == correct.id
    assert correct.provisor_goods_id == 777
    assert wrong.provisor_goods_id is None
    assert source.product_id == correct.id
    assert source.match_type == "provisor_goods_id"
    assert source.matched_sku == source_match_key(platform="provisor", source_external_key=777)

    after = list_catalog_code_mappings(db=db, platform="provisor", price_format_id=pf.id, status="unmapped")
    assert after["pagination"]["total"] == 0

    unmap_competitor_code_mapping(saved["id"], db, _admin())


def test_provisor_manual_mapping_bulk_updates_by_goods_id_without_loading_items(monkeypatch):
    db = _session()
    pf = _price_format(db, "BULK")
    price_list = _price_list(db, pf, source_key="bulk", price_date=date(2026, 1, 1))
    product = _product(db, "BULK-P", "Bulk mapped product")
    key = source_match_key(platform="provisor", source_external_key=424242)
    for idx in range(1000):
        _item(db, price_list, 424242, name=f"Bulk source {idx}")
    already_correct = _item(db, price_list, 424242, name="Already correct", product_id=product.id)
    already_correct.match_key = key
    already_correct.match_type = "provisor_goods_id"
    already_correct.matched_sku = key
    already_correct.match_score = 100
    other = _item(db, price_list, 999999, name="Other goods")
    db.commit()

    item_update_statements: list[str] = []

    def count_item_updates(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lower().lstrip().startswith("update competitor_price_list_items"):
            item_update_statements.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", count_item_updates)
    monkeypatch.setattr(
        code_mappings_service,
        "source_match_key_for_item",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fast path must not inspect ORM items")),
    )
    try:
        saved = create_competitor_code_mapping(
            {
                "platform": "provisor",
                "status": "mapped",
                "sourceExternalKey": "424242",
                "sourceMatchKey": key,
                "sourceName": "Bulk source",
                "ourProductId": product.id,
            },
            db,
            _admin(),
        )
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", count_item_updates)

    db.refresh(product)
    db.refresh(other)
    updated_rows = db.query(CompetitorPriceListItem).filter_by(provisor_goods_id=424242).all()
    mapping = db.query(CompetitorCodeMapping).filter_by(source_match_key=key).one()

    assert saved["touchedItems"] == 1000
    assert len(item_update_statements) == 1
    assert product.provisor_goods_id == 424242
    assert mapping.our_product_id == product.id
    assert mapping.status == "mapped"
    assert all(row.product_id == product.id for row in updated_rows)
    assert all(row.match_key == key for row in updated_rows)
    assert all(row.match_type == "provisor_goods_id" for row in updated_rows)
    assert all(row.matched_sku == key for row in updated_rows)
    assert other.product_id is None
    assert other.match_key == ""
    assert other.match_type == "unmatched"


def test_non_provisor_manual_mapping_keeps_existing_item_update_semantics():
    db = _session()
    product = _product(db, "VID-P", "Vidman mapped product")
    price_list = CompetitorPriceList(source_type="vidman", source_key="vidman", supplier="vidman", display_name="vidman")
    db.add(price_list)
    db.flush()
    item = CompetitorPriceListItem(
        price_list_id=price_list.id,
        distributor_goods_id="VID-1",
        name="Vidman source",
        raw_name="Vidman source",
        match_type="unmatched",
        matched_sku="",
    )
    db.add(item)
    db.commit()

    saved = create_competitor_code_mapping(
        {
            "platform": "vidman",
            "status": "mapped",
            "sourceExternalKey": "VID-1",
            "sourceMatchKey": source_match_key(platform="vidman", source_external_key="VID-1"),
            "sourceName": "Vidman source",
            "ourProductId": product.id,
        },
        db,
        _admin(),
    )

    db.refresh(item)
    assert saved["touchedItems"] == 1
    assert item.product_id == product.id
    assert item.match_type == "manual_code_mapping"
    assert item.matched_sku == product.code


def test_product_catalog_returns_product_rows_and_existing_mappings():
    db = _session()
    product = _product(db, "SKU-1", "Aspirin tab 500 mg N10", "Bayer")
    other = _product(db, "SKU-2", "Paracetamol tab 200 mg N20", "Other")
    db.add(
        CompetitorCodeMapping(
            platform="provisor",
            source_external_key="84721",
            source_match_key=source_match_key(platform="provisor", source_external_key=84721),
            source_name="Aspirin tab 500 mg N10",
            source_manufacturer="Bayer",
            status="mapped",
            our_product_id=product.id,
            our_sku=product.code,
        )
    )
    db.commit()

    result = list_product_catalog_code_mappings(db=db, platform="all", status="all", page=1, limit=50, include_candidates=False)

    assert result["pagination"]["total"] == 2
    rows = {row["sku"]: row for row in result["items"]}
    assert rows["SKU-1"]["productId"] == product.id
    assert rows["SKU-1"]["status"] == "mapped"
    assert rows["SKU-1"]["mappingCount"] == 1
    assert rows["SKU-1"]["mappings"][0]["externalId"] == "84721"
    assert rows["SKU-2"]["productId"] == other.id
    assert rows["SKU-2"]["status"] == "unmapped"
    assert result["metrics"][0]["total"] == 2
    assert result["metrics"][0]["mapped"] == 1


def test_product_catalog_supports_multiple_external_mappings_per_product():
    db = _session()
    product = _product(db, "SKU-MULTI", "Nurofen 200 mg N20", "Reckitt")
    db.add_all(
        [
            CompetitorCodeMapping(
                platform="provisor",
                source_external_key="84721",
                source_match_key=source_match_key(platform="provisor", source_external_key=84721),
                source_name="Nurofen 200 mg N20",
                status="mapped",
                our_product_id=product.id,
                our_sku=product.code,
            ),
            CompetitorCodeMapping(
                platform="provisor",
                source_external_key="91234",
                source_match_key=source_match_key(platform="provisor", source_external_key=91234),
                source_name="Nurofen tab 200 mg N20",
                status="mapped",
                our_product_id=product.id,
                our_sku=product.code,
            ),
            CompetitorCodeMapping(
                platform="vidman",
                source_external_key="1106:44556",
                source_match_key=source_match_key(platform="vidman", source_external_key="1106:44556"),
                source_name="Nurofen 200 mg N20",
                status="mapped",
                our_product_id=product.id,
                our_sku=product.code,
            ),
        ]
    )
    db.commit()

    row = list_product_catalog_code_mappings(db=db, platform="all", include_candidates=False)["items"][0]

    assert row["sku"] == "SKU-MULTI"
    assert row["mappingCount"] == 3
    assert [item["externalId"] for item in row["mappings"]] == ["84721", "91234", "1106:44556"]


def test_product_catalog_searches_internal_and_external_fields():
    db = _session()
    product = _product(db, "SKU-SEARCH", "Citramon forte N20", "Pharm")
    _product(db, "SKU-OTHER", "Ibuprofen N10", "Other")
    db.add(
        CompetitorCodeMapping(
            platform="provisor",
            source_external_key="95822",
            source_match_key=source_match_key(platform="provisor", source_external_key=95822),
            source_name="External Citramon",
            status="mapped",
            our_product_id=product.id,
            our_sku=product.code,
        )
    )
    db.commit()

    by_sku = list_product_catalog_code_mappings(db=db, q="SKU-SEARCH", include_candidates=False)
    by_name = list_product_catalog_code_mappings(db=db, q="Citramon", include_candidates=False)
    by_external = list_product_catalog_code_mappings(db=db, q="95822", include_candidates=False)

    assert [row["sku"] for row in by_sku["items"]] == ["SKU-SEARCH"]
    assert [row["sku"] for row in by_name["items"]] == ["SKU-SEARCH"]
    assert [row["sku"] for row in by_external["items"]] == ["SKU-SEARCH"]


def test_product_catalog_candidates_treat_manufacturer_as_soft_and_critical_fields_as_hard():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="candidates", price_date=date(2026, 1, 1))
    product = _product(db, "ASP-500", "Aspirin tab 500 mg N10", "Polpharma")
    _item(db, price_list, 1001, name="Aspirin tab 500 mg N10", manufacturer="Bayer")
    _item(db, price_list, 1002, name="Aspirin tab 200 mg N10", manufacturer="Polpharma")
    _item(db, price_list, 1003, name="Aspirin caps 500 mg N10", manufacturer="Polpharma")
    _item(db, price_list, 1004, name="Aspirin tab 500 mg N20", manufacturer="Polpharma")
    _item(db, price_list, 1005, name="Aspirin tab 500 mg N10 200 ml", manufacturer="Polpharma")
    db.commit()

    candidates = product_catalog_candidates_for_product(db=db, product_id=product.id, platform="provisor")

    assert [candidate["sourceExternalKey"] for candidate in candidates] == ["1001"]
    assert candidates[0]["manufacturerMismatch"] is True


def _product_catalog_candidates(db: Session, product: Product, *, platform: str = "provisor") -> list[dict]:
    db.commit()
    return product_catalog_candidates_for_product(db=db, product_id=product.id, platform=platform)


def test_product_catalog_provisor_goods_id_is_existing_mapping_without_code_mapping():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="goods-id", price_date=date(2026, 1, 1))
    product = _product(db, "NURO-GID", "\u041d\u0443\u0440\u043e\u0444\u0435\u043d \u0442\u0430\u0431\u043b\u0435\u0442\u043a\u0438 200 \u043c\u0433 \u211620", "Reckitt")
    product.provisor_goods_id = 555001
    _item(db, price_list, 555001, name="\u041d\u0443\u0440\u043e\u0444\u0435\u043d 200 \u043c\u0433 \u0442\u0430\u0431. \u211620", manufacturer="Famar")
    _item(db, price_list, 555002, name="\u041d\u0443\u0440\u043e\u0444\u0435\u043d 200 \u043c\u0433 \u0442\u0430\u0431. \u211620", manufacturer="Other")

    result = list_product_catalog_code_mappings(db=db, platform="provisor", q=product.code, status="all", include_candidates=True)
    row = result["items"][0]

    assert row["status"] == "mapped"
    assert row["mappingCount"] == 1
    assert row["mappings"][0]["platform"] == "provisor"
    assert row["mappings"][0]["sourceExternalKey"] == "555001"
    assert row["mappings"][0]["sourceMatchKey"] == "provisor:555001"
    assert row["reviewCandidates"] == []
    assert result["metrics"][0]["mapped"] == 1


def test_product_catalog_provisor_goods_id_drives_mapped_and_unmapped_filters():
    db = _session()
    mapped = _product(db, "PROV-MAPPED", "Mapped tab 5 mg N10", "Maker")
    mapped.provisor_goods_id = 555011
    unmapped = _product(db, "PROV-UNMAPPED", "Unmapped tab 5 mg N10", "Maker")
    db.commit()

    mapped_result = list_product_catalog_code_mappings(db=db, platform="provisor", status="mapped", include_candidates=False)
    unmapped_result = list_product_catalog_code_mappings(db=db, platform="provisor", status="unmapped", include_candidates=False)

    assert [row["sku"] for row in mapped_result["items"]] == [mapped.code]
    assert [row["sku"] for row in unmapped_result["items"]] == [unmapped.code]
    assert mapped_result["metrics"][0]["total"] == 2
    assert mapped_result["metrics"][0]["mapped"] == 1
    assert mapped_result["metrics"][0]["unmapped"] == 1


def test_product_catalog_main_endpoint_does_not_generate_candidates(monkeypatch):
    db = _session()
    mapped = _product(db, "PROV-SKIP", "Mapped skip tab 5 mg N10", "Maker")
    mapped.provisor_goods_id = 555012
    needs_review = _product(db, "PROV-NEEDS", "Needscandidate tab 5 mg N10", "Maker")

    def fake_candidates(db: Session, *, products: list[tuple[Product, ProductExtra | None]], platforms: list[str], assigned_ids=None, limit_per_product: int = 5):
        raise AssertionError("main product-catalog endpoint must not generate fuzzy candidates")

    monkeypatch.setattr(code_mappings_service, "_product_catalog_source_candidates_for_products", fake_candidates)

    result = code_mappings_service.list_product_catalog_code_mappings(db=db, platform="provisor", status="all", include_candidates=True)

    rows = {row["sku"]: row for row in result["items"]}
    assert rows[mapped.code]["status"] == "mapped"
    assert rows[mapped.code]["reviewCandidates"] == []
    assert rows[needs_review.code]["status"] == "unmapped"


def test_product_catalog_null_provisor_goods_id_with_exact_name_candidate_is_review():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="null-exact", price_date=date(2026, 1, 1))
    product = _product(db, "NULL-EXACT", "Exactdrug tab 5 mg N10", "Maker")
    _item(db, price_list, 555021, name="Exactdrug tab 5 mg N10", manufacturer="Maker")

    candidates = product_catalog_candidates_for_product(db=db, product_id=product.id, platform="provisor")

    assert candidates[0]["sourceExternalKey"] == "555021"


def test_product_catalog_null_provisor_goods_id_without_candidates_is_unmapped():
    db = _session()
    product = _product(db, "NO-CAND", "No candidate unique tab 5 mg N10", "Maker")
    db.commit()

    result = list_product_catalog_code_mappings(db=db, platform="provisor", q=product.code, status="all", include_candidates=True)
    candidates = product_catalog_candidates_for_product(db=db, product_id=product.id, platform="provisor")

    assert result["items"][0]["status"] == "unmapped"
    assert result["items"][0]["reviewCandidates"] == []
    assert candidates == []


def test_product_catalog_rejected_candidate_exclusion_does_not_mark_product_mapped():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="rejected-status", price_date=date(2026, 1, 1))
    product = _product(db, "REJ-STATUS", "Rejectstatus tab 5 mg N10", "Maker")
    source = _item(db, price_list, 555031, name="Rejectstatus tab 5 mg N10", manufacturer="Maker")
    db.add(
        CompetitorCodeMapping(
            platform="provisor",
            source_external_key=str(source.provisor_goods_id),
            source_match_key=source_match_key(platform="provisor", source_external_key=source.provisor_goods_id),
            source_name=source.raw_name,
            source_manufacturer=source.raw_manufacturer,
            status="rejected",
        )
    )

    result = list_product_catalog_code_mappings(db=db, platform="provisor", q=product.code, status="all", include_candidates=True)
    candidates = product_catalog_candidates_for_product(db=db, product_id=product.id, platform="provisor")

    assert result["items"][0]["status"] == "unmapped"
    assert result["items"][0]["reviewCandidates"] == []
    assert candidates == []


def test_product_catalog_main_statuses_do_not_call_candidate_generator(monkeypatch):
    db = _session()
    mapped = _product(db, "PERF-MAPPED", "Mapped perf tab 5 mg N10", "Maker")
    mapped.provisor_goods_id = 700001
    review = _product(db, "PERF-REVIEW", "Review perf tab 5 mg N10", "Maker")
    unmapped = _product(db, "PERF-UNMAPPED", "Unmapped perf tab 5 mg N10", "Maker")
    db.add(
        CompetitorCodeMapping(
            platform="provisor",
            source_external_key="700002",
            source_match_key=source_match_key(platform="provisor", source_external_key=700002),
            source_name="Review source",
            status="review",
            our_product_id=review.id,
            our_sku=review.code,
        )
    )
    db.commit()

    def fail_candidates(*_args, **_kwargs):
        raise AssertionError("main product-catalog endpoint must not generate candidates")

    monkeypatch.setattr(code_mappings_service, "_product_catalog_source_candidates_for_products", fail_candidates)

    all_rows = list_product_catalog_code_mappings(db=db, platform="provisor", status="all", include_candidates=True)
    review_rows = list_product_catalog_code_mappings(db=db, platform="provisor", status="review", include_candidates=True)
    unmapped_rows = list_product_catalog_code_mappings(db=db, platform="provisor", status="unmapped", include_candidates=True)

    assert {row["sku"]: row["status"] for row in all_rows["items"]} == {
        mapped.code: "mapped",
        review.code: "review",
        unmapped.code: "unmapped",
    }
    assert [row["sku"] for row in review_rows["items"]] == [review.code]
    assert [row["sku"] for row in unmapped_rows["items"]] == [unmapped.code]
    assert all_rows["metrics"][0]["review"] == 1
    assert all_rows["metrics"][0]["unmapped"] == 1


def test_product_catalog_unmapped_query_count_is_bounded_without_candidate_searches():
    db = _session()
    for idx in range(80):
        _product(db, f"BOUND-{idx:03d}", f"Bounded unique {idx} tab 5 mg N10", "Maker")
    db.commit()
    statements: list[str] = []

    def before_execute(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", before_execute)
    try:
        result = list_product_catalog_code_mappings(db=db, platform="provisor", status="unmapped", page=1, limit=50, include_candidates=True)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", before_execute)

    token_source_queries = [
        statement
        for statement in statements
        if "competitor_price_list_items" in statement and "LIKE" in statement.upper()
    ]
    assert len(result["items"]) == 50
    assert len(statements) <= 8
    assert token_source_queries == []


def test_product_catalog_candidate_endpoint_generates_for_one_product(monkeypatch):
    db = _session()
    product = _product(db, "ONE-CAND", "Onecandidate tab 5 mg N10", "Maker")
    other = _product(db, "OTHER-CAND", "Othercandidate tab 5 mg N10", "Maker")
    db.commit()
    seen: list[int] = []

    def fake_candidates(db: Session, *, products: list[tuple[Product, ProductExtra | None]], platforms: list[str], assigned_ids=None, limit_per_product: int = 5):
        seen.extend(int(product.id) for product, _extra in products)
        product_id = int(products[0][0].id)
        return {product_id: [{"platform": platforms[0], "sourceMatchKey": "provisor:1", "sourceExternalKey": "1", "sourceName": "One"}]}

    monkeypatch.setattr(code_mappings_service, "_product_catalog_source_candidates_for_products", fake_candidates)

    def override_db():
        try:
            yield db
        finally:
            pass

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[get_current_user] = _admin
    try:
        client = TestClient(main.app)
        response = client.get(f"/api/competitors/code-mappings/product-catalog/{product.id}/candidates?platform=provisor")
    finally:
        main.app.dependency_overrides.clear()

    assert other.id not in seen
    assert seen == [product.id]
    assert response.status_code == 200
    assert response.json()[0]["sourceMatchKey"] == "provisor:1"


def test_product_catalog_candidate_endpoint_respects_platform_and_format_scope():
    db = _session()
    current = _price_format(db, "CAND-FMT")
    other_pf = _price_format(db, "OTHER-FMT")
    assigned = _price_list(db, current, source_key="assigned", price_date=date(2026, 1, 2))
    other = _price_list(db, other_pf, source_key="other", price_date=date(2026, 1, 1))
    product = _product(db, "FMT-CAND", "Formatdrug tab 5 mg N10", "Maker")
    _generated_price_list(db, current, product, number="FMT-CAND-PL")
    _item(db, assigned, 701001, name="Formatdrug tab 5 mg N10", manufacturer="Maker")
    _item(db, other, 701002, name="Formatdrug tab 5 mg N10", manufacturer="Maker")
    db.commit()

    scoped = product_catalog_candidates_for_product(db=db, product_id=product.id, platform="provisor", format_code=current.code)
    vidman = product_catalog_candidates_for_product(db=db, product_id=product.id, platform="vidman", format_code=current.code)

    assert [candidate["sourceExternalKey"] for candidate in scoped] == ["701001"]
    assert vidman == []


def test_product_catalog_vidman_is_not_mapped_by_provisor_goods_id():
    db = _session()
    product = _product(db, "VIDMAN-PROV", "Vidman product tab 5 mg N10", "Maker")
    product.provisor_goods_id = 555041
    db.commit()

    result = list_product_catalog_code_mappings(db=db, platform="vidman", q=product.code, status="all", include_candidates=False)

    assert result["items"][0]["status"] == "unmapped"
    assert result["items"][0]["mappingCount"] == 0
    assert result["metrics"][0]["mapped"] == 0


def test_product_catalog_price_format_scope_limits_products_and_counts():
    db = _session()
    pf_a = _price_format(db, "PF-A")
    pf_b = _price_format(db, "PF-B")
    product_a = _product(db, "PF-A-SKU", "Format A tab 5 mg N10", "Maker")
    product_b = _product(db, "PF-B-SKU", "Format B tab 5 mg N10", "Maker")
    product_a.provisor_goods_id = 555051
    _generated_price_list(db, pf_a, product_a, number="PL-A")
    _generated_price_list(db, pf_b, product_b, number="PL-B")
    db.commit()

    result = list_product_catalog_code_mappings(db=db, platform="provisor", status="all", include_candidates=False, format_code="PF-A")

    assert result["pagination"]["total"] == 1
    assert [row["sku"] for row in result["items"]] == [product_a.code]
    assert result["metrics"][0]["total"] == 1
    assert result["metrics"][0]["mapped"] == 1


def test_product_catalog_same_structure_different_or_missing_manufacturer_returns_candidates():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="manufacturer-soft", price_date=date(2026, 1, 1))
    product = _product(db, "NURO-MFR", "\u041d\u0443\u0440\u043e\u0444\u0435\u043d \u0442\u0430\u0431\u043b\u0435\u0442\u043a\u0438 200 \u043c\u0433 \u211620", "Reckitt")
    _item(db, price_list, 555101, name="\u041d\u0443\u0440\u043e\u0444\u0435\u043d 200 \u043c\u0433 \u0442\u0430\u0431. \u211620", manufacturer="Famar")
    _item(db, price_list, 555102, name="\u041d\u0443\u0440\u043e\u0444\u0435\u043d 200 \u043c\u0433 \u0442\u0430\u0431. \u211620", manufacturer="")

    candidates = _product_catalog_candidates(db, product)

    assert {candidate["sourceExternalKey"] for candidate in candidates} == {"555101", "555102"}
    assert any(candidate["manufacturerMismatch"] is True for candidate in candidates)
    assert all(candidate["confidence"] >= 80 for candidate in candidates)


def test_product_catalog_acc_long_latin_cyrillic_variants_return_candidate():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="acc-long", price_date=date(2026, 1, 1))
    product = _product(db, "ACC-LONG", "\u0410\u0426\u0426 \u041b\u043e\u043d\u0433 \u0442\u0430\u0431\u043b\u0435\u0442\u043a\u0438 \u0448\u0438\u043f\u0443\u0447\u0438\u0435 600 \u043c\u0433 N10", "Sandoz")
    _item(db, price_list, 555201, name="ACC LONG 600 mg \u211610", manufacturer="Hexal")

    candidates = _product_catalog_candidates(db, product)

    assert [candidate["sourceExternalKey"] for candidate in candidates] == ["555201"]


def test_product_catalog_true_dosage_conflict_rejected_but_missing_dosage_allowed():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="dosage-tiers", price_date=date(2026, 1, 1))
    product = _product(db, "ASP-MISS-DOSE", "Aspirin tab 500 mg N10", "Bayer")
    _item(db, price_list, 555301, name="Aspirin tab 1000 mg N10", manufacturer="Bayer")
    _item(db, price_list, 555302, name="Aspirin tab N10", manufacturer="Bayer")

    candidates = _product_catalog_candidates(db, product)

    assert [candidate["sourceExternalKey"] for candidate in candidates] == ["555302"]
    assert candidates[0]["matchLevel"] in {"medium", "fuzzy"}


def test_product_catalog_pack_variants_and_missing_pack_are_allowed_when_safe():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="pack-tiers", price_date=date(2026, 1, 1))
    product = _product(db, "PACK-20", "Aspirin tab 500 mg №20", "Bayer")
    _item(db, price_list, 555401, name="Aspirin tab 500 mg 20 tabs", manufacturer="Bayer")
    _item(db, price_list, 555402, name="Aspirin tab 500 mg 2x10", manufacturer="Bayer")
    _item(db, price_list, 555403, name="Aspirin tab 500 mg", manufacturer="Bayer")

    candidates = _product_catalog_candidates(db, product)

    assert {candidate["sourceExternalKey"] for candidate in candidates} == {"555401", "555402", "555403"}
    assert candidates[0]["sourceExternalKey"] in {"555401", "555402"}


def test_product_catalog_missing_form_does_not_eliminate_candidate():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="missing-form", price_date=date(2026, 1, 1))
    product = _product(db, "FORM-MISS", "Aspirin tab 500 mg N10", "Bayer")
    _item(db, price_list, 555501, name="Aspirin 500 mg N10", manufacturer="Bayer")

    candidates = _product_catalog_candidates(db, product)

    assert [candidate["sourceExternalKey"] for candidate in candidates] == ["555501"]


def test_product_catalog_candidate_outside_old_global_window_is_found():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="global-window", price_date=date(2026, 1, 1))
    common = _product(db, "WINDOW-1", "Commonbrand tab 5 mg N10", "Maker")
    target = _product(db, "WINDOW-2", "Windowdrug tab 5 mg N10", "Maker")
    _item(db, price_list, 556001, name="Windowdrug tab 5 mg N10", manufacturer="Maker")
    for idx in range(800):
        _item(db, price_list, 557000 + idx, name="Commonbrand tab 5 mg N10", manufacturer="Maker")

    db.commit()
    candidates = product_catalog_candidates_for_product(db=db, product_id=target.id, platform="provisor")

    assert candidates[0]["sourceExternalKey"] == "556001"


def test_product_catalog_more_than_fifty_tokens_still_returns_later_candidate():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="token-window", price_date=date(2026, 1, 1))
    target = None
    for idx in range(60):
        product = _product(db, f"TOK-{idx:02d}", f"Tokenbrand{idx} tab 5 mg N10", "Maker")
        _item(db, price_list, 558000 + idx, name=f"Tokenbrand{idx} tab 5 mg N10", manufacturer="Maker")
        if idx == 59:
            target = product
    assert target is not None

    db.commit()
    candidates = product_catalog_candidates_for_product(db=db, product_id=target.id, platform="provisor")

    assert candidates[0]["sourceExternalKey"] == "558059"


def test_product_catalog_form_conflict_is_not_returned_as_candidate():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="form-conflict", price_date=date(2026, 1, 1))
    product = _product(db, "FORM-CONFLICT", "Samebrand tab 500 mg N10", "Bayer")
    _item(db, price_list, 558501, name="Samebrand caps 500 mg N10", manufacturer="Bayer")

    db.commit()
    candidates = product_catalog_candidates_for_product(db=db, product_id=product.id, platform="provisor")

    assert candidates == []


def test_product_catalog_rejected_candidate_remains_excluded():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="rejected", price_date=date(2026, 1, 1))
    product = _product(db, "REJ-1", "Rejectdrug tab 5 mg N10", "Maker")
    source = _item(db, price_list, 559001, name="Rejectdrug tab 5 mg N10", manufacturer="Maker")
    db.add(
        CompetitorCodeMapping(
            platform="provisor",
            source_external_key=str(source.provisor_goods_id),
            source_match_key=source_match_key(platform="provisor", source_external_key=source.provisor_goods_id),
            source_name=source.raw_name,
            source_manufacturer=source.raw_manufacturer,
            status="rejected",
        )
    )

    candidates = product_catalog_candidates_for_product(db=db, product_id=product.id, platform="provisor")

    assert candidates == []


def test_product_catalog_auto_match_creates_global_mapping_for_exact_candidate():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="auto-product", price_date=date(2026, 1, 1))
    product = _product(db, "AUTO-1", "Aspirin tab 500 mg N10", "Bayer")
    source = _item(db, price_list, 7777, name="Aspirin tab 500 mg N10", manufacturer="Bayer")
    db.commit()

    result = auto_match_product_catalog_code_mappings(db=db, platform="provisor", limit=10, created_by="test")

    assert result["createdMappings"] == 1
    mapping = db.query(CompetitorCodeMapping).filter_by(source_match_key=source_match_key(platform="provisor", source_external_key=7777)).one()
    db.refresh(source)
    assert mapping.our_product_id == product.id
    assert mapping.status == "mapped"
    assert source.product_id == product.id
    assert source.match_type == "provisor_goods_id"
    assert source.matched_sku == source_match_key(platform="provisor", source_external_key=7777)


def test_manual_confirm_reuses_global_source_key_and_reject_is_source_specific():
    db = _session()
    pf = _price_format(db)
    price_list = _price_list(db, pf, source_key="manual", price_date=date(2026, 1, 1))
    product = _product(db, "MANUAL-1", "Ibuprofen tab 200 mg N20", "A")
    other = _product(db, "MANUAL-2", "Ibuprofen tab 200 mg N20", "A")
    source = _item(db, price_list, 8888, name="Ibuprofen tab 200 mg N20", manufacturer="A")
    rejected_source = _item(db, price_list, 9999, name="Ibuprofen tab 200 mg N10", manufacturer="A")
    db.commit()

    first = create_competitor_code_mapping(
        {
            "platform": "provisor",
            "status": "mapped",
            "itemId": source.id,
            "sourceMatchKey": source_match_key(platform="provisor", source_external_key=8888),
            "ourProductId": product.id,
        },
        db,
        _admin(),
    )
    second = create_competitor_code_mapping(
        {
            "platform": "provisor",
            "status": "mapped",
            "itemId": source.id,
            "sourceMatchKey": source_match_key(platform="provisor", source_external_key=8888),
            "ourProductId": other.id,
        },
        db,
        _admin(),
    )
    rejected = create_competitor_code_mapping(
        {
            "platform": "provisor",
            "status": "rejected",
            "itemId": rejected_source.id,
            "sourceMatchKey": source_match_key(platform="provisor", source_external_key=9999),
        },
        db,
        _admin(),
    )

    assert first["id"] == second["id"]
    assert second["ourProductId"] == other.id
    assert db.query(CompetitorCodeMapping).filter_by(source_match_key=source_match_key(platform="provisor", source_external_key=8888)).count() == 1
    assert rejected["status"] == "rejected"
    assert rejected["sourceMatchKey"] == source_match_key(platform="provisor", source_external_key=9999)
    assert not hasattr(db.query(CompetitorCodeMapping).first(), "price_format_id")


def test_mapping_catalog_requires_auth_and_write_requires_write_role():
    db = _session()
    pf = _price_format(db, "AUTH")
    price_list = _price_list(db, pf, source_key="auth", price_date=date(2026, 1, 1))
    source = _item(db, price_list, 888, name="Auth source")
    product = Product(code="AUTH-P", name="Auth product", cost=1)
    db.add(product)
    db.commit()

    def override_db():
        try:
            yield db
        finally:
            pass

    def reject_user():
        raise HTTPException(status_code=401, detail="Not authenticated")

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[get_current_user] = reject_user
    try:
        client = TestClient(main.app)
        response = client.get("/api/competitors/code-mappings/catalog-view?platform=provisor&format_code=AUTH")
        assert response.status_code == 401
    finally:
        main.app.dependency_overrides.clear()

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[get_current_user] = _viewer
    try:
        client = TestClient(main.app)
        response = client.post(
            "/api/competitors/code-mappings",
            json={
                "platform": "provisor",
                "status": "mapped",
                "formatCode": "AUTH",
                "itemId": source.id,
                "ourProductId": product.id,
            },
        )
        assert response.status_code == 403
    finally:
        main.app.dependency_overrides.clear()

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[get_current_user] = _admin
    try:
        client = TestClient(main.app)
        response = client.get("/api/competitors/code-mappings/catalog-view?platform=provisor&format_code=AUTH")
        assert response.status_code == 200
        assert response.json()["pagination"]["total"] == 1
    finally:
        main.app.dependency_overrides.clear()
