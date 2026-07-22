from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import (
    BendRange,
    CompetitorPrice,
    CompetitorPriceList,
    CompetitorPriceListItem,
    MarkupRange,
    NoCompetitorMarkupRange,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    Product,
)
from backend.app.services.competitor_coefficients import validate_price_coefficient
from backend.app.services.competitor_matching import rebuild_competitor_prices_for_selected
from backend.app.services.competitor_price_lists import export_competitor_price_list, sync_selected_competitor_configs, upsert_provisor_price_list
from backend.app.services.manual_price_list_import import import_manual_price_list
from backend.app.services.pricing import calculate_price_for_product, calculate_price_zone, resolve_competitor_prices


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _format(db, *, code="FMT", bend=0):
    pf = PriceFormat(code=code, name=code, branch="1", progib=bend)
    db.add(pf)
    db.flush()
    db.add(MarkupRange(price_format_id=pf.id, cost_from=0, cost_to=None, markup_percent=0.15))
    db.add(NoCompetitorMarkupRange(price_format_id=pf.id, cost_from=0, cost_to=None, markup_percent=0.03))
    db.add(BendRange(price_format_id=pf.id, price_from=0, bend_percent=bend))
    db.flush()
    return pf


def _product(db, *, code="SKU-1", cost=100):
    product = Product(code=code, name=code, cost=cost)
    db.add(product)
    db.flush()
    return product


def _plk(db, pf, product, *, source_key, price, coefficient=1.0):
    row = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="manual",
        source_key=source_key,
        display_name=source_key,
        supplier=source_key,
        competitor_name=source_key,
        price_coefficient=coefficient,
    )
    db.add(row)
    db.flush()
    db.add(
        CompetitorPriceListItem(
            price_list_id=row.id,
            product_id=product.id,
            name=product.name,
            distributor_goods_name=product.name,
            distributor_goods_id=product.code,
            distributor_price=price,
            matched_sku=product.code,
            match_type="sku",
        )
    )
    db.add(PriceFormatCompetitorAssignment(price_format_id=pf.id, competitor_price_list_id=row.id, is_active=True))
    db.flush()
    return row


def _prepare(db, pf):
    sync_selected_competitor_configs(db=db, price_format_id=pf.id)
    rebuild_competitor_prices_for_selected(db=db, price_format_id=pf.id)
    sync_selected_competitor_configs(db=db, price_format_id=pf.id)
    db.flush()


def test_default_coefficient_is_one_and_raw_price_is_preserved():
    db = _session()
    pf = _format(db)
    product = _product(db)
    row = _plk(db, pf, product, source_key="A", price=100)
    _prepare(db, pf)

    resolved = resolve_competitor_prices(db, pf.id, product.id)
    item = db.execute(select(CompetitorPriceListItem).where(CompetitorPriceListItem.price_list_id == row.id)).scalar_one()
    derived = db.execute(select(CompetitorPrice).where(CompetitorPrice.product_id == product.id)).scalar_one()

    assert resolved.prices == [(Decimal("100.000000"), "manual:A")]
    assert float(item.distributor_price) == 100
    assert float(derived.source_price) == 100
    assert float(derived.coefficient) == 1


def test_discount_and_increase_coefficients_adjust_best_competitor_once():
    db = _session()
    pf = _format(db)
    product = _product(db)
    _plk(db, pf, product, source_key="discount", price=100, coefficient=1.10)
    _plk(db, pf, product, source_key="increase", price=105, coefficient=1.00)
    _prepare(db, pf)

    resolved = resolve_competitor_prices(db, pf.id, product.id)

    assert resolved.prices[0][1] == "manual:increase"
    assert resolved.prices[0][0] == Decimal("105.000000")
    assert resolved.details["manual:discount"]["original_price"] == Decimal("100.0000")
    assert resolved.details["manual:discount"]["price_coefficient"] == Decimal("1.100000")
    assert resolved.details["manual:discount"]["adjusted_price"] == Decimal("110.0000000000")


def test_bend_mdc_zone_and_log_use_adjusted_competitor_price():
    db = _session()
    pf = _format(db, bend=0)
    db.query(BendRange).filter(BendRange.price_format_id == pf.id).delete()
    db.add(BendRange(price_format_id=pf.id, price_from=0, bend_percent=0))
    db.add(BendRange(price_format_id=pf.id, price_from=900, bend_percent=10))
    db.add(BendRange(price_format_id=pf.id, price_from=1000, bend_percent=20))
    product = _product(db, cost=100)
    _plk(db, pf, product, source_key="discount", price=1000, coefficient=0.975)
    _prepare(db, pf)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())
    zone = debug["zone"]
    reference = debug["zone_reference_price"]

    assert debug["chosen_competitor_original_price"] == Decimal("1000.0000")
    assert debug["chosen_competitor_price_coefficient"] == Decimal("0.975000")
    assert debug["chosen_competitor_adjusted_price"] == Decimal("975.0000000000")
    assert debug["bend_percent_used"] == Decimal("10.0000")
    assert price == Decimal("877.50000000000000")
    assert reference == Decimal("975.0000000000")
    assert zone == "left"
    assert "Original competitor price" in debug["log"]


def test_export_exposes_original_coefficient_and_adjusted_price():
    db = _session()
    pf = _format(db)
    product = _product(db)
    row = _plk(db, pf, product, source_key="A", price=1000, coefficient=0.975)

    _, content, _ = export_competitor_price_list(db=db, price_list_id=row.id, fmt="csv")
    exported = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))

    assert exported[0]["Original competitor price"] == "1000.0"
    assert exported[0]["Price coefficient"] == "0.975"
    assert exported[0]["Adjusted competitor price"] == "975.0"


@pytest.mark.parametrize("value", [0, -1, float("nan"), "", 100.1])
def test_invalid_price_coefficient_rejected(value):
    with pytest.raises(ValueError):
        validate_price_coefficient(value)


def test_provisor_refresh_preserves_price_coefficient():
    db = _session()
    pf = _format(db, code="P")
    row = upsert_provisor_price_list(
        db=db,
        price_format_code=pf.code,
        filial_id=77,
        items=[{"distributorGoodsId": "SKU-1", "distributorGoodsName": "SKU-1", "goodsPrice": 100}],
        run_matching=False,
    )
    row.price_coefficient = 0.975
    db.commit()

    refreshed = upsert_provisor_price_list(
        db=db,
        price_format_code=pf.code,
        filial_id=77,
        items=[{"distributorGoodsId": "SKU-1", "distributorGoodsName": "SKU-1", "goodsPrice": 120}],
        run_matching=False,
    )

    assert refreshed.id == row.id
    assert float(refreshed.price_coefficient) == 0.975


def test_manual_reimport_preserves_price_coefficient():
    db = _session()
    _format(db)
    _product(db, code="SKU-1")
    content = "SKU,Name,Price\nSKU-1,One,100\n".encode()
    result = import_manual_price_list(db=db, price_format_code="FMT", content=content, filename="one.csv")
    row = db.get(CompetitorPriceList, result["id"])
    row.price_coefficient = 1.025
    db.commit()

    import_manual_price_list(
        db=db,
        price_format_code="FMT",
        price_list_id=row.id,
        content="SKU,Name,Price\nSKU-1,One,120\n".encode(),
        filename="two.csv",
    )

    assert float(db.get(CompetitorPriceList, row.id).price_coefficient) == 1.025
