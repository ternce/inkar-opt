from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.models import Base, CompetitorPriceList, CompetitorPriceListItem, PriceFormat, Product, ProductExtra
from backend.app.main import create_competitor_code_mapping, unmap_competitor_code_mapping
from backend.app.services.competitors.code_mappings import list_catalog_code_mappings


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def test_unmapped_queue_excludes_primary_goods_id_and_includes_no_candidate_products():
    db = _session()
    db.add_all(
        [
            Product(code="SKU-MAPPED", name="Mapped", cost=1, provisor_goods_id=101),
            Product(code="SKU-OPEN", name="Open", cost=1),
        ]
    )
    db.commit()

    result = list_catalog_code_mappings(db=db, platform="provisor", status="unmapped", page=1, limit=50)

    assert [row["ourSku"] for row in result["items"]] == ["SKU-OPEN"]
    assert result["pagination"] == {"page": 1, "pageSize": 50, "total": 1, "pageCount": 1}
    assert result["metrics"][0]["mapped"] == 1
    assert result["metrics"][0]["noCandidates"] == 1


def test_sku_search_is_case_insensitive_and_paginates_filtered_unmapped_rows():
    db = _session()
    db.add_all(
        [
            Product(code="AbC-001", name="First", cost=1),
            Product(code="abc-002", name="Second", cost=1),
            Product(code="OTHER", name="Other", cost=1),
        ]
    )
    db.commit()

    first = list_catalog_code_mappings(
        db=db, platform="provisor", status="unmapped", product_q="ABC-", page=1, limit=1
    )
    second = list_catalog_code_mappings(
        db=db, platform="provisor", status="unmapped", product_q="abc-", page=2, limit=1
    )

    assert first["pagination"]["total"] == 2
    assert first["pagination"]["pageCount"] == 2
    assert first["metrics"][0]["total"] == 3
    assert [row["ourSku"] for row in first["items"]] == ["AbC-001"]
    assert [row["ourSku"] for row in second["items"]] == ["abc-002"]


def test_candidates_reuse_match_score_ranking_and_deduplicate_goods_id():
    db = _session()
    product = Product(code="SKU-OPEN", name="Same product", cost=1)
    price_format = PriceFormat(code="TEST", name="Test", branch="Test")
    db.add(price_format)
    db.flush()
    price_list = CompetitorPriceList(
        price_format_id=price_format.id,
        source_type="provisor",
        source_key="test",
        supplier="test",
        display_name="test",
        price_date=date(2026, 1, 1),
    )
    db.add_all([product, price_list])
    db.flush()
    db.add_all(
        [
            CompetitorPriceListItem(
                price_list_id=price_list.id,
                provisor_goods_id=10,
                name="Same product",
                raw_name="Same product",
                match_score=70,
            ),
            CompetitorPriceListItem(
                price_list_id=price_list.id,
                provisor_goods_id=20,
                name="Same product",
                raw_name="Same product",
                match_score=95,
            ),
        ]
    )
    db.commit()

    result = list_catalog_code_mappings(db=db, platform="provisor", status="unmapped", page=1, limit=50)

    assert [row["sourceExternalKey"] for row in result["items"][0]["candidates"]] == ["20", "10"]


def test_mapping_save_and_unmap_keep_primary_goods_id_in_sync():
    db = _session()
    product = Product(code="SKU-OPEN", name="Same product", cost=1)
    price_format = PriceFormat(code="TEST", name="Test", branch="Test")
    db.add_all([product, price_format])
    db.flush()
    price_list = CompetitorPriceList(
        price_format_id=price_format.id,
        source_type="provisor",
        source_key="test",
        supplier="test",
        display_name="test",
        price_date=date(2026, 1, 1),
    )
    db.add(price_list)
    db.flush()
    item = CompetitorPriceListItem(
        price_list_id=price_list.id,
        provisor_goods_id=777,
        name="Same product",
        raw_name="Same product",
    )
    db.add(item)
    db.commit()

    saved = create_competitor_code_mapping(
        {"platform": "provisor", "status": "mapped", "itemId": item.id, "ourProductId": product.id},
        db,
    )
    db.refresh(product)
    db.refresh(item)

    assert product.provisor_goods_id == 777
    assert item.product_id == product.id
    assert item.matched_sku == "SKU-OPEN"
    assert item.match_type == "manual_code_mapping"

    unmap_competitor_code_mapping(saved["id"], db)
    db.refresh(product)
    db.refresh(item)
    assert product.provisor_goods_id is None
    assert item.product_id is None
    assert item.matched_sku == ""


def test_manual_suggestions_allow_different_manufacturer_but_reject_wrong_dosage():
    db = _session()
    product = Product(code="PARA-A", name="Парацетамол 500 мг №20 таб", cost=1)
    price_format = PriceFormat(code="TEST", name="Test", branch="Test")
    db.add_all([product, price_format])
    db.flush()
    db.add(ProductExtra(product_id=product.id, manufacturer="Manufacturer A"))
    price_list = CompetitorPriceList(
        price_format_id=price_format.id,
        source_type="provisor",
        source_key="test",
        supplier="test",
        display_name="test",
        price_date=date(2026, 1, 1),
    )
    db.add(price_list)
    db.flush()
    db.add_all(
        [
            CompetitorPriceListItem(
                price_list_id=price_list.id,
                provisor_goods_id=501,
                name="Парацетамол 500 мг №20 таб",
                raw_name="Парацетамол 500 мг №20 таб",
                raw_manufacturer="Manufacturer B",
            ),
            CompetitorPriceListItem(
                price_list_id=price_list.id,
                provisor_goods_id=502,
                name="Парацетамол 250 мг №20 таб",
                raw_name="Парацетамол 250 мг №20 таб",
                raw_manufacturer="Manufacturer C",
            ),
            CompetitorPriceListItem(
                price_list_id=price_list.id,
                provisor_goods_id=503,
                name="Ибупрофен 500 мг №20 таб",
                raw_name="Ибупрофен 500 мг №20 таб",
                raw_manufacturer="Manufacturer D",
            ),
        ]
    )
    db.commit()

    result = list_catalog_code_mappings(db=db, platform="provisor", status="unmapped", page=1, limit=50)
    candidates = result["items"][0]["candidates"]

    assert [row["sourceExternalKey"] for row in candidates] == ["501"]
    assert 55 <= candidates[0]["confidence"] < 100
    assert candidates[0]["manualSuggestion"]["dosageMatch"] is True
    assert candidates[0]["manualSuggestion"]["quantityMatch"] is True
