from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import (
    BranchStock,
    CompetitorPriceList,
    CompetitorPriceListItem,
    PriceFormat,
    Product,
    ProductSubstituteMatch,
    SourceGoodsMatch,
)
from backend.app.services.competitor_matching import (
    PROVISOR_REFERENCE_FILIAL_ID,
    PROVISOR_REFERENCE_FILIAL_IDS,
    _sync_provisor_reference_mapping_from_items,
    rematch_price_list_items,
    rematch_price_list_items_by_product,
)

PROVISOR_REFERENCE_FILIAL_ID_133 = 133


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _price_format(db):
    pf = PriceFormat(code="TEST_01", name="Test")
    db.add(pf)
    db.flush()
    return pf


def _product(db, *, code="SKU128", name="Reference Product"):
    product = Product(code=code, name=name, cost=1)
    db.add(product)
    db.flush()
    return product


def _price_list(db, pf, *, source_key, filial_id=None):
    row = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key=f"account-1:{source_key}",
        display_name=f"Provisor {source_key}",
        branch_id=str(filial_id or source_key),
        account_id="account-1",
    )
    db.add(row)
    db.flush()
    return row


def test_emit_rematch_keeps_all_prices_for_same_goods_id():
    db = _session()
    pf = _price_format(db)
    product = _product(db, code="163571", name="Emit Product")
    product.provisor_goods_id = 163571
    price_list = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="emit:1106",
        display_name="Emit International 1106",
        supplier="Emit International 1106",
        branch_name="Emit International 1106",
        competitor_name="Emit International 1106",
        account_login="emit",
    )
    db.add(price_list)
    db.flush()
    for price in [8688.26, 8989.73, 9358.46]:
        db.add(
            CompetitorPriceListItem(
                price_list_id=price_list.id,
                provisor_goods_id=163571,
                filial_id=1106,
                name="Emit Product",
                distributor_goods_name="Emit Product",
                distributor_price=price,
            )
        )
    db.flush()

    rematch_price_list_items_by_product(db=db, price_list=price_list)

    rows = (
        db.execute(
            select(CompetitorPriceListItem)
            .where(CompetitorPriceListItem.price_list_id == price_list.id)
            .order_by(CompetitorPriceListItem.id.asc())
        )
        .scalars()
        .all()
    )
    assert [float(row.distributor_price) for row in rows] == [8688.26, 8989.73, 9358.46]
    assert {row.product_id for row in rows} == {product.id}
    assert {row.matched_sku for row in rows} == {"000000000000163571"}


def _item(db, price_list, *, distributor_goods_id, goods_id=None, filial_id=None, name="Supplier Product"):
    item = CompetitorPriceListItem(
        price_list_id=price_list.id,
        provisor_goods_id=goods_id,
        filial_id=filial_id,
        name=name,
        distributor_goods_name=name,
        distributor_goods_id=distributor_goods_id,
        distributor_price=10,
        stock=1,
        raw_name=name,
        raw_manufacturer="",
        match_type="unmatched",
    )
    db.add(item)
    db.flush()
    return item


def test_reference_filial_sync_saves_goods_id_mapping():
    db = _session()
    pf = _price_format(db)
    product = _product(db, code="SKU128")
    price_list = _price_list(db, pf, source_key=PROVISOR_REFERENCE_FILIAL_ID, filial_id=PROVISOR_REFERENCE_FILIAL_ID)
    _item(
        db,
        price_list,
        distributor_goods_id="SKU128",
        goods_id=9876543210,
        filial_id=PROVISOR_REFERENCE_FILIAL_ID,
    )

    stats = _sync_provisor_reference_mapping_from_items(db, account_id="account-1")
    db.flush()

    db.refresh(product)
    assert stats["mapped_products"] == 1
    assert stats["matched_via_128"] == 1
    assert stats["updated_total"] == 1
    assert product.provisor_goods_id == 9876543210
    assert db.execute(select(SourceGoodsMatch)).scalars().all() == []


def test_reference_filial_sync_uses_133_fallback():
    db = _session()
    pf = _price_format(db)
    product = _product(db, code="ASTANA-SKU")
    assert PROVISOR_REFERENCE_FILIAL_ID_133 in PROVISOR_REFERENCE_FILIAL_IDS
    price_list = _price_list(db, pf, source_key=PROVISOR_REFERENCE_FILIAL_ID_133, filial_id=PROVISOR_REFERENCE_FILIAL_ID_133)
    _item(
        db,
        price_list,
        distributor_goods_id="ASTANA-SKU",
        goods_id=9876543210,
        filial_id=PROVISOR_REFERENCE_FILIAL_ID_133,
    )

    stats = _sync_provisor_reference_mapping_from_items(db, account_id="account-1")
    db.flush()

    db.refresh(product)
    assert product.provisor_goods_id == 9876543210
    assert stats["matched_via_133"] == 1
    assert stats["updatedViaSourceGoodsMatch"] == 0


def test_reference_filial_sync_both_same_goods_id_updates_product():
    db = _session()
    pf = _price_format(db)
    product = _product(db, code="SHARED-SKU")
    almaty = _price_list(db, pf, source_key=PROVISOR_REFERENCE_FILIAL_ID, filial_id=PROVISOR_REFERENCE_FILIAL_ID)
    astana = _price_list(db, pf, source_key=PROVISOR_REFERENCE_FILIAL_ID_133, filial_id=PROVISOR_REFERENCE_FILIAL_ID_133)
    _item(db, almaty, distributor_goods_id="SHARED-SKU", goods_id=123456, filial_id=PROVISOR_REFERENCE_FILIAL_ID)
    _item(db, astana, distributor_goods_id="SHARED-SKU", goods_id=123456, filial_id=PROVISOR_REFERENCE_FILIAL_ID_133)

    stats = _sync_provisor_reference_mapping_from_items(db, account_id="account-1")
    db.flush()

    db.refresh(product)
    assert product.provisor_goods_id == 123456
    assert stats["matched_via_both_same"] == 1
    assert stats["conflict_128_133"] == 0


def test_reference_filial_sync_conflicting_128_133_skips_product():
    db = _session()
    pf = _price_format(db)
    product = _product(db, code="CONFLICT-SKU")
    almaty = _price_list(db, pf, source_key=PROVISOR_REFERENCE_FILIAL_ID, filial_id=PROVISOR_REFERENCE_FILIAL_ID)
    astana = _price_list(db, pf, source_key=PROVISOR_REFERENCE_FILIAL_ID_133, filial_id=PROVISOR_REFERENCE_FILIAL_ID_133)
    _item(db, almaty, distributor_goods_id="CONFLICT-SKU", goods_id=111, filial_id=PROVISOR_REFERENCE_FILIAL_ID)
    _item(db, astana, distributor_goods_id="CONFLICT-SKU", goods_id=222, filial_id=PROVISOR_REFERENCE_FILIAL_ID_133)

    stats = _sync_provisor_reference_mapping_from_items(db, account_id="account-1")
    db.flush()

    db.refresh(product)
    assert product.provisor_goods_id is None
    assert stats["conflict_128_133"] == 1
    assert stats["updated_total"] == 0


def test_reference_filial_sync_uses_batch_lookup_without_per_product_selects():
    db = _session()
    pf = _price_format(db)
    products = [_product(db, code=f"BATCH-{idx}") for idx in range(3)]
    price_list = _price_list(db, pf, source_key=PROVISOR_REFERENCE_FILIAL_ID, filial_id=PROVISOR_REFERENCE_FILIAL_ID)
    for idx in range(3):
        _item(
            db,
            price_list,
            distributor_goods_id=f"BATCH-{idx}",
            goods_id=9000 + idx,
            filial_id=PROVISOR_REFERENCE_FILIAL_ID,
        )

    statements: list[str] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("select"):
            statements.append(statement)

    event.listen(db.bind, "before_cursor_execute", before_cursor_execute)
    try:
        stats = _sync_provisor_reference_mapping_from_items(db, account_id="account-1")
    finally:
        event.remove(db.bind, "before_cursor_execute", before_cursor_execute)
    db.flush()

    for product in products:
        db.refresh(product)
    assert [product.provisor_goods_id for product in products] == [9000, 9001, 9002]
    assert stats["updated_total"] == 3
    assert len(statements) == 2


def test_reference_filial_sync_skips_ambiguous_branch_stock_lookup():
    db = _session()
    pf = _price_format(db)
    first = _product(db, code="FIRST-SKU")
    second = _product(db, code="SECOND-SKU")
    db.add_all(
        [
            BranchStock(branch_id="A", product_id=first.id, sku="REF-DIST-SKU", stock=1),
            BranchStock(branch_id="B", product_id=second.id, sku="REF-DIST-SKU", stock=1),
        ]
    )
    price_list = _price_list(db, pf, source_key=PROVISOR_REFERENCE_FILIAL_ID, filial_id=PROVISOR_REFERENCE_FILIAL_ID)
    _item(
        db,
        price_list,
        distributor_goods_id="REF-DIST-SKU",
        goods_id=555001,
        filial_id=PROVISOR_REFERENCE_FILIAL_ID,
    )

    stats = _sync_provisor_reference_mapping_from_items(db, account_id="account-1")
    db.flush()

    db.refresh(first)
    db.refresh(second)
    assert first.provisor_goods_id is None
    assert second.provisor_goods_id is None
    assert stats["ambiguousMatch"] == 0
    assert stats["skipped_product_not_found"] == 2
    assert stats["updatedViaBranchStock"] == 0


def test_reference_filial_sync_does_not_overwrite_existing_goods_id():
    db = _session()
    pf = _price_format(db)
    product = _product(db, code="REF-DIST-SKU")
    product.provisor_goods_id = 111001
    price_list = _price_list(db, pf, source_key=PROVISOR_REFERENCE_FILIAL_ID, filial_id=PROVISOR_REFERENCE_FILIAL_ID)
    _item(
        db,
        price_list,
        distributor_goods_id="REF-DIST-SKU",
        goods_id=222002,
        filial_id=PROVISOR_REFERENCE_FILIAL_ID,
    )

    stats = _sync_provisor_reference_mapping_from_items(db, account_id="account-1")
    db.flush()

    db.refresh(product)
    assert product.provisor_goods_id == 111001
    assert stats["existing_goods_id_conflict"] == 1
    assert stats["updated_total"] == 0


def test_non_reference_filial_matches_by_goods_id_without_fuzzy():
    db = _session()
    pf = _price_format(db)
    product = _product(db, code="SKU128", name="Reference Product")
    reference = _price_list(db, pf, source_key=PROVISOR_REFERENCE_FILIAL_ID, filial_id=PROVISOR_REFERENCE_FILIAL_ID)
    _item(
        db,
        reference,
        distributor_goods_id="SKU128",
        goods_id=555001,
        filial_id=PROVISOR_REFERENCE_FILIAL_ID,
    )
    target = _price_list(db, pf, source_key=159, filial_id=159)
    target_item = _item(
        db,
        target,
        distributor_goods_id="OTHER-FILIAL-SKU",
        goods_id=555001,
        filial_id=159,
        name="Different supplier spelling",
    )

    stats = rematch_price_list_items(db=db, price_list=target)

    db.refresh(target_item)
    assert target_item.product_id == product.id
    assert target_item.match_type == "provisor_goods_id"
    assert stats["matchedByGoodsId"] == 1
    assert stats["matchedByFuzzy"] == 0


def test_product_rebuild_matches_non_reference_filial_by_goods_id():
    db = _session()
    pf = _price_format(db)
    product = _product(db, code="SKU128", name="Reference Product")
    reference = _price_list(db, pf, source_key=PROVISOR_REFERENCE_FILIAL_ID, filial_id=PROVISOR_REFERENCE_FILIAL_ID)
    _item(
        db,
        reference,
        distributor_goods_id="SKU128",
        goods_id=777001,
        filial_id=PROVISOR_REFERENCE_FILIAL_ID,
    )
    target = _price_list(db, pf, source_key=1106, filial_id=1106)
    target_item = _item(
        db,
        target,
        distributor_goods_id="DIFFERENT-SKU-1106",
        goods_id=777001,
        filial_id=1106,
        name="Different branch product spelling",
    )

    stats = rematch_price_list_items_by_product(db=db, price_list=target)

    db.refresh(target_item)
    assert target_item.product_id == product.id
    assert target_item.match_type == "provisor_goods_id"
    assert stats["matchedByFuzzy"] == 0
    assert stats["matchedByGoodsId"] >= 1


def test_manual_goods_id_mapping_overrides_product_field_in_product_rebuild():
    db = _session()
    pf = _price_format(db)
    auto_product = _product(db, code="AUTO-SKU", name="Auto Product")
    manual_product = _product(db, code="MANUAL-SKU", name="Manual Product")
    reference = _price_list(db, pf, source_key=PROVISOR_REFERENCE_FILIAL_ID, filial_id=PROVISOR_REFERENCE_FILIAL_ID)
    _item(
        db,
        reference,
        distributor_goods_id="AUTO-SKU",
        goods_id=888001,
        filial_id=PROVISOR_REFERENCE_FILIAL_ID,
    )
    db.add(
        SourceGoodsMatch(
            price_format_id=pf.id,
            source_type="provisor",
            distributor_goods_id="MANUAL-SKU",
            goods_id=888001,
            product_id=manual_product.id,
            similarity_score=100,
            match_method="manual_approved",
        )
    )
    target = _price_list(db, pf, source_key=159, filial_id=159)
    target_item = _item(
        db,
        target,
        distributor_goods_id="BRANCH-SKU-159",
        goods_id=888001,
        filial_id=159,
        name="Manual override item",
    )

    rematch_price_list_items_by_product(db=db, price_list=target)

    db.refresh(auto_product)
    db.refresh(target_item)
    assert auto_product.provisor_goods_id == 888001
    assert target_item.product_id == manual_product.id
    assert target_item.match_type == "provisor_goods_id"


def test_non_reference_filial_does_not_match_by_distributor_goods_id_only():
    db = _session()
    pf = _price_format(db)
    _product(db, code="SKU128", name="Reference Product")
    target = _price_list(db, pf, source_key=1106, filial_id=1106)
    target_item = _item(
        db,
        target,
        distributor_goods_id="SKU128",
        goods_id=None,
        filial_id=1106,
        name="Completely unrelated item",
    )

    stats = rematch_price_list_items(db=db, price_list=target)

    db.refresh(target_item)
    assert target_item.product_id is None
    assert target_item.match_type == "unmatched"
    assert stats["matchedBySku"] == 0


def test_manual_substitute_matches_only_when_primary_goods_price_missing():
    db = _session()
    pf = _price_format(db)
    product = _product(db, code="SKU128", name="Reference Product")
    product.provisor_goods_id = 111001
    db.add(
        ProductSubstituteMatch(
            product_id=product.id,
            source_type="provisor",
            source_goods_id=222002,
            source_distributor_goods_id="SUB-SKU",
            source_name="Substitute Product",
            source_manufacturer="Other Manufacturer",
            status="approved",
        )
    )
    target = _price_list(db, pf, source_key=159, filial_id=159)
    target_item = _item(
        db,
        target,
        distributor_goods_id="SUB-SKU",
        goods_id=222002,
        filial_id=159,
        name="Substitute Product",
    )
    db.flush()

    stats = rematch_price_list_items_by_product(db=db, price_list=target)

    db.refresh(target_item)
    assert target_item.product_id == product.id
    assert target_item.match_type == "provisor_manual_substitute"
    assert stats["matchedByManualSubstitute"] == 1

    primary_item = _item(
        db,
        target,
        distributor_goods_id="PRIMARY-SKU",
        goods_id=111001,
        filial_id=159,
        name="Primary Product",
    )

    rematch_price_list_items_by_product(db=db, price_list=target)

    db.refresh(primary_item)
    db.refresh(target_item)
    assert primary_item.product_id == product.id
    assert primary_item.match_type == "provisor_goods_id"
    assert target_item.product_id is None
