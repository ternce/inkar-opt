from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import (
    CalculatedPrice,
    CompetitorPrice,
    CompetitorPriceList,
    CompetitorPriceListItem,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    PriceList,
    Product,
)
from backend.app.services.competitor_price_lists import relink_provisor_items_from_product_goods_ids, upsert_unified_price_list
from backend.app.services.price_sources import UnifiedPriceItem, UnifiedPriceList


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(db):
    pf = PriceFormat(code="FMT", name="Format")
    product_a = Product(code="SKU-A", name="Product A", cost=10, provisor_goods_id=1001)
    product_b = Product(code="SKU-B", name="Product B", cost=10, provisor_goods_id=1002)
    db.add_all([pf, product_a, product_b])
    db.commit()
    return pf, product_a, product_b


def _price_list(*, source_updated_at: str = "2026-07-27T10:00:00") -> UnifiedPriceList:
    return UnifiedPriceList(
        source="provisor",
        account_id="4",
        account_login="account-4",
        price_list_id="158",
        price_list_name="Filial 158",
        distributor_name="Filial 158",
        branch_id="158",
        branch_code="158",
        branch_name="Filial 158",
        competitor_name="Filial 158",
        source_updated_at=source_updated_at,
    )


def _item(
    *,
    goods_id: int | None,
    distributor_goods_id: str,
    name: str = "Medicine",
    price: str = "10",
    raw_extra: dict | None = None,
) -> UnifiedPriceItem:
    raw = {
        "id": (goods_id or 0) * 10,
        "goodsId": goods_id,
        "batch": "B1",
        **(raw_extra or {}),
    }
    return UnifiedPriceItem(
        source="provisor",
        account_id="4",
        price_list_id="158",
        price_list_name="Filial 158",
        distributor_name="Filial 158",
        product_name=name,
        manufacturer="Manufacturer",
        registration_number="REG",
        distributor_product_name=name,
        distributor_product_id=distributor_goods_id,
        distributor_price=Decimal(price),
        stock=Decimal("3"),
        pack_quantity=Decimal("1"),
        expiry_date="2027-01-31",
        raw=raw,
    )


def _items(db):
    return (
        db.execute(select(CompetitorPriceListItem).order_by(CompetitorPriceListItem.id.asc()))
        .scalars()
        .all()
    )


def test_existing_match_fields_survive_refresh_with_column_only_load():
    db = _session()
    _, product_a, _ = _seed(db)
    row = upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(),
        items=[_item(goods_id=1001, distributor_goods_id="DG-1")],
        run_matching=False,
    )
    existing = _items(db)[0]
    existing.product_id = product_a.id
    existing.match_type = "manual"
    existing.match_score = 87.5
    existing.matched_sku = product_a.code
    db.commit()

    refreshed = upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(source_updated_at="2026-07-27T11:00:00"),
        items=[_item(goods_id=1001, distributor_goods_id="DG-1", price="11")],
        run_matching=False,
    )
    [saved] = _items(db)

    assert refreshed.id == row.id
    assert saved.product_id == product_a.id
    assert saved.match_type == "manual"
    assert float(saved.match_score) == 87.5
    assert saved.matched_sku == "SKU-A"
    assert refreshed.source_updated_at == "2026-07-27T11:00:00"


def test_refresh_exact_goods_id_relinks_and_raw_json_remains_intact():
    db = _session()
    _, product_a, _ = _seed(db)

    upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(),
        items=[_item(goods_id=1001, distributor_goods_id="DG-1", raw_extra={"payload": {"a": 1}})],
        run_matching=False,
    )
    saved = _items(db)[0]
    raw_json = json.loads(saved.raw_json)

    assert saved.product_id == product_a.id
    assert saved.match_type == "provisor_goods_id"
    assert saved.match_key == "provisor:1001"
    assert float(saved.match_score) == 100
    assert saved.matched_sku == "provisor:1001"
    assert raw_json["raw"]["goodsId"] == 1001
    assert raw_json["raw"]["payload"] == {"a": 1}


def test_refresh_unrelated_goods_id_stays_unmatched():
    db = _session()
    _seed(db)

    upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(),
        items=[_item(goods_id=909090, distributor_goods_id="DG-X")],
        run_matching=False,
    )
    saved = _items(db)[0]

    assert saved.product_id is None
    assert saved.match_type == "unmatched"
    assert saved.matched_sku == ""


def test_refresh_null_goods_id_stays_unmatched():
    db = _session()
    _seed(db)

    upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(),
        items=[_item(goods_id=None, distributor_goods_id="DG-NULL")],
        run_matching=False,
    )
    saved = _items(db)[0]

    assert saved.provisor_goods_id is None
    assert saved.product_id is None
    assert saved.match_type == "unmatched"
    assert saved.matched_sku == ""


def test_duplicate_stable_identity_preserves_first_existing_match():
    db = _session()
    _, product_a, product_b = _seed(db)
    upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(),
        items=[
            _item(goods_id=1001, distributor_goods_id="DG-DUP", name="Medicine A"),
            _item(goods_id=1001, distributor_goods_id="DG-DUP", name="Medicine A"),
        ],
        run_matching=False,
    )
    first, second = _items(db)
    first.product_id = product_a.id
    first.match_type = "manual"
    first.match_score = 90
    first.matched_sku = product_a.code
    second.product_id = product_b.id
    second.match_type = "manual"
    second.match_score = 80
    second.matched_sku = product_b.code
    db.commit()

    upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(source_updated_at="2026-07-27T12:00:00"),
        items=[_item(goods_id=1001, distributor_goods_id="DG-DUP", name="Medicine A")],
        run_matching=False,
    )
    [saved] = _items(db)

    assert saved.product_id == product_a.id
    assert saved.matched_sku == product_a.code


def test_empty_plk_replacement_deletes_old_rows_and_keeps_per_plk_commit():
    db = _session()
    _seed(db)
    row = upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(),
        items=[_item(goods_id=1001, distributor_goods_id="DG-1")],
        run_matching=False,
    )

    refreshed = upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(source_updated_at="2026-07-27T13:00:00"),
        items=[],
        run_matching=False,
    )

    assert refreshed.id == row.id
    assert _items(db) == []
    assert refreshed._benchmark["deleted_rows_count"] == 1
    assert refreshed._benchmark["inserted_rows_count"] == 0


def test_failure_after_delete_rolls_back_old_rows_when_caller_rolls_back(monkeypatch):
    db = _session()
    _seed(db)
    upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(),
        items=[_item(goods_id=1001, distributor_goods_id="DG-1")],
        run_matching=False,
    )
    old_item_id = _items(db)[0].id

    def fail_bulk_insert_mappings(*args, **kwargs):
        raise RuntimeError("bulk insert failed")

    monkeypatch.setattr(db, "bulk_insert_mappings", fail_bulk_insert_mappings)
    with pytest.raises(RuntimeError):
        upsert_unified_price_list(
            db=db,
            price_format_code="FMT",
            price_list=_price_list(source_updated_at="2026-07-27T14:00:00"),
            items=[_item(goods_id=1002, distributor_goods_id="DG-2")],
            run_matching=False,
        )
    db.rollback()

    [saved] = _items(db)
    assert saved.id == old_item_id
    assert saved.provisor_goods_id == 1001


def test_replacement_row_count_matches_input_count():
    db = _session()
    _seed(db)
    inputs = [
        _item(goods_id=1001, distributor_goods_id="DG-1"),
        _item(goods_id=1002, distributor_goods_id="DG-2"),
        _item(goods_id=1003, distributor_goods_id="DG-3"),
    ]

    row = upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(),
        items=inputs,
        run_matching=False,
    )

    assert len(_items(db)) == len(inputs)
    assert row._benchmark["inserted_rows_count"] == len(inputs)


def test_chunked_provisor_persistence_preserves_all_rows(monkeypatch):
    import backend.app.services.competitor_price_lists as service

    db = _session()
    _seed(db)
    monkeypatch.setattr(service, "PROVISOR_INSERT_BATCH_SIZE", 2)
    inputs = [
        _item(goods_id=1000 + index, distributor_goods_id=f"DG-{index}")
        for index in range(5)
    ]

    row = upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(),
        items=inputs,
        run_matching=False,
    )

    assert len(_items(db)) == 5
    assert row._benchmark["inserted_rows_count"] == 5
    assert row._benchmark["max_insert_batch_size"] == 2


def test_provisor_raw_json_can_be_disabled(monkeypatch):
    import backend.app.services.competitor_price_lists as service

    db = _session()
    _seed(db)
    monkeypatch.setattr(service, "STORE_PROVISOR_RAW_JSON", False)

    upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(),
        items=[_item(goods_id=1001, distributor_goods_id="DG-1", raw_extra={"large": "payload"})],
        run_matching=False,
    )

    assert _items(db)[0].raw_json == ""


def test_replacement_memory_metrics_do_not_change_saved_rows(monkeypatch):
    import backend.app.services.competitor_price_lists as service

    db = _session()
    _seed(db)
    monkeypatch.setattr(service, "process_memory_snapshot", lambda: {"rss_mb": 77.7})
    inputs = [
        _item(goods_id=1001, distributor_goods_id="DG-1", raw_extra={"keep": ["raw"]}),
        _item(goods_id=1002, distributor_goods_id="DG-2"),
    ]

    row = upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(),
        items=inputs,
        run_matching=False,
    )
    saved = _items(db)
    first_raw = json.loads(saved[0].raw_json)

    assert len(saved) == 2
    assert first_raw["raw"]["keep"] == ["raw"]
    assert row._benchmark["rss_after_insert_mb"] == 77.7
    assert row._benchmark["rss_after_flush_mb"] == 77.7
    assert row._benchmark["rss_after_commit_mb"] == 77.7
    assert isinstance(row._benchmark["identity_map_after_flush"], int)


def test_exact_goods_id_relink_is_idempotent_for_already_correct_item():
    db = _session()
    _, product_a, _ = _seed(db)
    row = upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(),
        items=[_item(goods_id=1001, distributor_goods_id="DG-1")],
        run_matching=False,
    )
    saved = _items(db)[0]
    assert saved.product_id == product_a.id

    summary = relink_provisor_items_from_product_goods_ids(db=db, price_list_ids=[row.id])
    db.refresh(saved)

    assert summary["relinkedItems"] == 0
    assert saved.product_id == product_a.id
    assert saved.match_type == "provisor_goods_id"
    assert saved.match_key == "provisor:1001"
    assert saved.matched_sku == "provisor:1001"


def test_exact_goods_id_relink_preserves_conflicting_product_id():
    db = _session()
    _, _, product_b = _seed(db)
    row = CompetitorPriceList(price_format_id=None, source_type="provisor", source_key="conflict")
    db.add(row)
    db.flush()
    item = CompetitorPriceListItem(
        price_list_id=row.id,
        provisor_goods_id=1001,
        product_id=product_b.id,
        distributor_price=10,
        match_type="manual",
        matched_sku=product_b.code,
        match_score=87,
    )
    db.add(item)
    db.flush()

    summary = relink_provisor_items_from_product_goods_ids(db=db, price_list_ids=[row.id])
    db.refresh(item)

    assert summary["relinkedItems"] == 0
    assert summary["conflictItems"] == 1
    assert item.product_id == product_b.id
    assert item.match_type == "manual"
    assert item.matched_sku == product_b.code
    assert float(item.match_score) == 87


def test_exact_goods_id_relink_links_null_product_id():
    db = _session()
    _, product_a, _ = _seed(db)
    row = CompetitorPriceList(price_format_id=None, source_type="provisor", source_key="unlinked")
    db.add(row)
    db.flush()
    item = CompetitorPriceListItem(price_list_id=row.id, provisor_goods_id=1001, distributor_price=10, match_type="unmatched")
    db.add(item)
    db.flush()

    summary = relink_provisor_items_from_product_goods_ids(db=db, price_list_ids=[row.id])
    db.refresh(item)

    assert summary["relinkedItems"] == 1
    assert summary["conflictItems"] == 0
    assert item.product_id == product_a.id
    assert item.match_type == "provisor_goods_id"


def test_exact_goods_id_relink_skips_ambiguous_goods_id():
    db = _session()
    _seed(db)
    duplicate = Product(code="SKU-DUP", name="Duplicate Product", cost=10, provisor_goods_id=1001)
    db.add(duplicate)
    db.commit()

    row = upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(),
        items=[_item(goods_id=1001, distributor_goods_id="DG-AMB")],
        run_matching=False,
    )
    saved = _items(db)[0]

    assert saved.product_id is None
    assert saved.match_type == "unmatched"
    assert row._benchmark["exact_goods_id_relinked_items"] == 0
    assert row._benchmark["exact_goods_id_ambiguous_goods_ids"] == 1
    assert row._benchmark["exact_goods_id_ambiguous_items"] == 1


def test_exact_goods_id_relink_is_scoped_to_requested_plk():
    db = _session()
    _, product_a, _ = _seed(db)
    first = CompetitorPriceList(price_format_id=None, source_type="provisor", source_key="first")
    second = CompetitorPriceList(price_format_id=None, source_type="provisor", source_key="second")
    db.add_all([first, second])
    db.flush()
    db.add_all(
        [
            CompetitorPriceListItem(price_list_id=first.id, provisor_goods_id=1001, distributor_price=10, match_type="unmatched"),
            CompetitorPriceListItem(price_list_id=second.id, provisor_goods_id=1001, distributor_price=11, match_type="unmatched"),
        ]
    )
    db.flush()

    summary = relink_provisor_items_from_product_goods_ids(db=db, price_list_ids=[first.id])
    rows = {
        int(item.price_list_id): item
        for item in db.execute(select(CompetitorPriceListItem)).scalars().all()
    }

    assert summary["relinkedItems"] == 1
    assert rows[first.id].product_id == product_a.id
    assert rows[second.id].product_id is None


def test_exact_goods_id_relink_ignores_non_provisor_price_lists():
    db = _session()
    _seed(db)
    row = CompetitorPriceList(price_format_id=None, source_type="vidman", source_key="vidman")
    db.add(row)
    db.flush()
    item = CompetitorPriceListItem(price_list_id=row.id, provisor_goods_id=1001, distributor_price=10, match_type="unmatched")
    db.add(item)
    db.flush()

    summary = relink_provisor_items_from_product_goods_ids(db=db, price_list_ids=[row.id])
    db.refresh(item)

    assert summary["relinkedItems"] == 0
    assert item.product_id is None
    assert item.match_type == "unmatched"


def test_exact_goods_id_relink_materializes_scoped_source_with_partial_pf_rows():
    db = _session()
    pf, product_a, product_b = _seed(db)
    other_pf = PriceFormat(code="OTHER", name="Other Format")
    history_list = PriceList(number="HIST-1", price_format_id=pf.id)
    db.add_all([other_pf, history_list])
    db.flush()
    history = CalculatedPrice(
        price_list_id=history_list.id,
        product_id=product_a.id,
        cost=10,
        base_price=12,
        final_price=15,
    )
    db.add(history)
    row = upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(),
        items=[_item(goods_id=909090, distributor_goods_id="DG-OLD", price="99")],
        run_matching=False,
    )
    db.add(
        PriceFormatCompetitorAssignment(
            price_format_id=pf.id,
            competitor_price_list_id=row.id,
            is_active=True,
            coefficient=1.0,
        )
    )
    db.add_all(
        [
            CompetitorPrice(
                price_format_id=pf.id,
                product_id=product_b.id,
                source_name="provisor:account:4:plk:158",
                supplier="Stale Same Source",
                source_price=99,
            ),
            CompetitorPrice(
                price_format_id=pf.id,
                product_id=product_b.id,
                source_name="provisor:other-source",
                supplier="Existing Other Source",
                source_price=77,
            ),
            CompetitorPrice(
                price_format_id=other_pf.id,
                product_id=product_b.id,
                source_name="provisor:account:4:plk:158",
                supplier="Other PF",
                source_price=66,
            ),
            CompetitorPrice(
                price_format_id=pf.id,
                product_id=None,
                source_name="provisor:other-source",
                supplier="Unrelated Config",
                coefficient=1.25,
            ),
            CompetitorPrice(
                price_format_id=pf.id,
                product_id=None,
                source_name="percentile:existing",
                supplier="Percentile Config",
                coefficient=1.75,
            ),
            CompetitorPrice(
                price_format_id=pf.id,
                product_id=None,
                source_name="provisor:account:4:plk:158",
                supplier="Current Source Config",
                coefficient=1.5,
            ),
        ]
    )
    db.commit()

    upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(source_updated_at="2026-07-27T15:00:00"),
        items=[_item(goods_id=1001, distributor_goods_id="DG-1", price="42")],
        run_matching=False,
    )

    product_rows = (
        db.execute(
            select(CompetitorPrice)
            .where(CompetitorPrice.price_format_id == pf.id)
            .where(CompetitorPrice.product_id == product_a.id)
        )
        .scalars()
        .all()
    )
    other_source_rows = (
        db.execute(
            select(CompetitorPrice)
            .where(CompetitorPrice.price_format_id == pf.id)
            .where(CompetitorPrice.source_name == "provisor:other-source")
            .where(CompetitorPrice.product_id.is_not(None))
        )
        .scalars()
        .all()
    )
    stale_same_source_rows = (
        db.execute(
            select(CompetitorPrice)
            .where(CompetitorPrice.price_format_id == pf.id)
            .where(CompetitorPrice.product_id == product_b.id)
            .where(CompetitorPrice.source_name == "provisor:account:4:plk:158")
        )
        .scalars()
        .all()
    )

    assert product_rows
    assert {row.source_name for row in product_rows} == {"provisor:account:4:plk:158"}
    assert {float(row.source_price) for row in product_rows} == {42.0}
    assert {float(row.source_price) for row in other_source_rows} == {77.0}
    assert stale_same_source_rows == []
    configs = {
        row.source_name: row
        for row in db.execute(
            select(CompetitorPrice)
            .where(CompetitorPrice.price_format_id == pf.id)
            .where(CompetitorPrice.product_id.is_(None))
        ).scalars()
    }
    assert {name: (row.supplier, float(row.coefficient)) for name, row in configs.items()} == {
        "provisor:other-source": ("Unrelated Config", 1.25),
        "percentile:existing": ("Percentile Config", 1.75),
        "provisor:account:4:plk:158": ("Current Source Config", 1.5),
    }
    other_pf_rows = db.execute(select(CompetitorPrice).where(CompetitorPrice.price_format_id == other_pf.id)).scalars().all()
    assert [(row.product_id, row.source_name, float(row.source_price)) for row in other_pf_rows] == [
        (product_b.id, "provisor:account:4:plk:158", 66.0)
    ]
    db.refresh(history)
    assert float(history.final_price) == 15.0
