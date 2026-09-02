from datetime import date

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app import main
from backend.app.deps import get_current_user
from backend.app.models import (
    AppUser,
    Base,
    CompetitorCodeMapping,
    CompetitorPriceList,
    CompetitorPriceListItem,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    Product,
)
from backend.app.main import create_competitor_code_mapping, unmap_competitor_code_mapping
from backend.app.services.competitors.code_mappings import list_catalog_code_mappings, source_match_key


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

    assert result["pagination"] == {"page": 1, "pageSize": 50, "total": 2, "pageCount": 1}
    assert [row["sourceExternalKey"] for row in result["items"]] == ["10", "20"]
    assert result["items"][0]["itemId"] == latest.id
    assert result["items"][0]["sourceName"] == "Latest duplicate"
    assert result["items"][1]["itemId"] == open_row.id
    assert result["items"][0]["ourProductId"] is None
    assert result["metrics"][0]["total"] == 6
    assert result["metrics"][0]["mapped"] == 3
    assert result["metrics"][0]["rejected"] == 1
    assert result["metrics"][0]["unmapped"] == 2


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

    for query in ["95822", "582", "\u0410\u0441\u043f\u0438\u0440\u0438\u043d", "BAYER", "DIST-95822"]:
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
    assert source.match_type == "manual_code_mapping"

    after = list_catalog_code_mappings(db=db, platform="provisor", price_format_id=pf.id, status="unmapped")
    assert after["pagination"]["total"] == 0

    unmap_competitor_code_mapping(saved["id"], db, _admin())


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
