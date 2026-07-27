from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import CompetitorPriceListItem, PriceFormat, Product
from backend.app.services.competitor_price_lists import upsert_unified_price_list
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
    goods_id: int,
    distributor_goods_id: str,
    name: str = "Medicine",
    price: str = "10",
    raw_extra: dict | None = None,
) -> UnifiedPriceItem:
    raw = {
        "id": goods_id * 10,
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


def test_unmatched_rows_stay_unmatched_and_raw_json_remains_intact():
    db = _session()
    _seed(db)

    upsert_unified_price_list(
        db=db,
        price_format_code="FMT",
        price_list=_price_list(),
        items=[_item(goods_id=1001, distributor_goods_id="DG-1", raw_extra={"payload": {"a": 1}})],
        run_matching=False,
    )
    saved = _items(db)[0]
    raw_json = json.loads(saved.raw_json)

    assert saved.product_id is None
    assert saved.match_type == "unmatched"
    assert saved.matched_sku == ""
    assert raw_json["raw"]["goodsId"] == 1001
    assert raw_json["raw"]["payload"] == {"a": 1}


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
