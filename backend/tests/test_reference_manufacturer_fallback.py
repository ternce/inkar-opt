from __future__ import annotations

import io
import logging

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import BranchCost, BranchStock, InternalProductNormalized, Product, ProductExtra
from backend.app.services.products_excel_import import import_products_excel
from backend.app.services.references.batch import import_reference_batch
from backend.app.services.references.imports import (
    ManufacturerBatchState, _apply_manufacturer_fallback, _manufacturer_summary,
    _record_batch_candidates, import_reference_excel,
)
from backend.app.services.references.sources import ExcelReferenceSource, ReferenceFilePayload
from backend.app.services.sku import normalize_sku


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _xlsx(headers, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def _seed(db, manufacturer=None, *, with_extra=True):
    product = Product(code=normalize_sku(12345), name="Medicine", cost=17)
    db.add(product)
    db.flush()
    if with_extra:
        db.add(ProductExtra(product_id=product.id, manufacturer=manufacturer, stock=9))
    db.commit()
    return product.id


def _import(db, data_type, rows, *, branch_ids=None):
    headers = ["sku", "name", "manufacturer", "branch_id"] if data_type == "products" else [
        "sku", "name", "manufacturer", data_type, "branch_id",
    ]
    return import_reference_excel(
        db=db,
        data_type=data_type,
        branch_ids=branch_ids or ["1"],
        content=_xlsx(headers, rows),
        filename=f"{data_type}.xlsx",
    )


@pytest.mark.parametrize("missing", [None, "", "   ", "-"])
def test_stock_fills_only_missing_manufacturer_by_normalized_sku(missing, caplog):
    db = _db()
    product_id = _seed(db, missing)
    with caplog.at_level(logging.INFO):
        job = _import(db, "stock", [[12345, "Medicine", "Bayer Farma", 24, "1"]])
    extra = db.get(ProductExtra, product_id)
    assert job.status == "success"
    assert extra.manufacturer == "BAYER FARMA"
    assert extra.stock == 24
    assert db.scalar(select(BranchStock.stock).where(BranchStock.product_id == product_id)) == 24
    assert "\"manufacturer_filled\": 1" in caplog.text


def test_cost_preserves_valid_catalog_manufacturer_and_numeric_values(caplog):
    db = _db()
    product_id = _seed(db, "Existing Maker")
    with caplog.at_level(logging.INFO):
        job = _import(db, "cost", [["000000000000012345", "Medicine", "Other Maker", 31.5, "1"]])
    assert job.status == "success"
    assert db.get(ProductExtra, product_id).manufacturer == "Existing Maker"
    assert db.get(Product, product_id).cost == 31.5
    assert db.scalar(select(BranchCost.cost).where(BranchCost.product_id == product_id)) == 31.5
    assert "\"manufacturer_already_present\": 1" in caplog.text


def test_cost_import_fills_missing_manufacturer():
    db = _db()
    product_id = _seed(db, "")
    job = _import(db, "cost", [[12345, "Medicine", "Bayer Farma", 31.5, "1"]])
    assert job.status == "success"
    assert db.get(ProductExtra, product_id).manufacturer == "BAYER FARMA"
    assert db.get(Product, product_id).cost == 31.5


def test_missing_source_manufacturer_leaves_catalog_unchanged(caplog):
    db = _db()
    product_id = _seed(db, "-")
    with caplog.at_level(logging.INFO):
        job = _import(db, "stock", [[12345, "Medicine Bayer", "-", 4, "1"]])
    assert job.status == "success"
    assert db.get(ProductExtra, product_id).manufacturer == "-"
    assert "\"manufacturer_missing_in_source\": 1" in caplog.text


def test_repeated_sku_with_same_normalized_manufacturer_fills_once():
    db = _db()
    product_id = _seed(db, "")
    job = _import(db, "stock", [
        [12345, "Medicine", "Bayer Farma", 3, "1"],
        ["000000000000012345", "Medicine", "BAYER FARMA", 5, "2"],
    ], branch_ids=["1", "2"])
    assert job.status == "success"
    assert db.get(ProductExtra, product_id).manufacturer == "BAYER FARMA"
    assert db.scalar(select(func.count(BranchStock.id)).where(BranchStock.product_id == product_id)) == 2


def test_conflicting_manufacturers_across_branches_are_not_filled(caplog):
    db = _db()
    product_id = _seed(db, "")
    with caplog.at_level(logging.INFO):
        job = _import(db, "cost", [
            [12345, "Medicine", "Bayer Farma", 10, "1"],
            [12345, "Medicine", "Novartis", 12, "2"],
        ], branch_ids=["1", "2"])
    assert job.status == "success"
    assert db.get(ProductExtra, product_id).manufacturer == ""
    assert db.scalar(select(func.count(BranchCost.id)).where(BranchCost.product_id == product_id)) == 2
    assert f"product_id={product_id}" in caplog.text
    assert db.get(Product, product_id).code in caplog.text
    assert "BAYER FARMA" in caplog.text and "NOVARTIS" in caplog.text
    assert "\"manufacturer_conflicts\": 1" in caplog.text


def test_batch_stock_and_cost_conflict_does_not_choose_source_order(caplog):
    db = _db()
    product_id = _seed(db, "")
    source = ExcelReferenceSource([
        ReferenceFilePayload("stock", "stock.xlsx", _xlsx(
            ["sku", "name", "manufacturer", "stock", "branch_id"],
            [[12345, "Medicine", "Bayer Farma", 3, "1"]],
        )),
        ReferenceFilePayload("cost", "cost.xlsx", _xlsx(
            ["sku", "name", "manufacturer", "cost", "branch_id"],
            [[12345, "Medicine", "Novartis", 12, "2"]],
        )),
    ])
    with caplog.at_level(logging.INFO):
        result = import_reference_batch(db=db, source=source, selected_branch_ids=["1", "2"])
    assert result["status"] == "success"
    assert db.get(ProductExtra, product_id).manufacturer == ""
    assert db.scalar(select(BranchStock.stock).where(BranchStock.product_id == product_id)) == 3
    assert db.scalar(select(BranchCost.cost).where(BranchCost.product_id == product_id)) == 12
    assert f"product_id={product_id}" in caplog.text
    assert "BAYER FARMA" in caplog.text and "NOVARTIS" in caplog.text


def test_batch_repeated_same_manufacturer_fills_once():
    db = _db()
    product_id = _seed(db, "")
    source = ExcelReferenceSource([
        ReferenceFilePayload("stock", "stock.xlsx", _xlsx(
            ["sku", "name", "manufacturer", "stock", "branch_id"],
            [[12345, "Medicine", "Bayer Farma", 3, "1"]],
        )),
        ReferenceFilePayload("cost", "cost.xlsx", _xlsx(
            ["sku", "name", "manufacturer", "cost", "branch_id"],
            [[12345, "Medicine", "BAYER FARMA", 12, "2"]],
        )),
    ])
    result = import_reference_batch(db=db, source=source, selected_branch_ids=["1", "2"])
    assert result["status"] == "success"
    assert db.get(ProductExtra, product_id).manufacturer == "BAYER FARMA"


def test_batch_parses_each_spreadsheet_once(monkeypatch):
    from backend.app.services.references import imports

    db = _db()
    _seed(db, "")
    source = ExcelReferenceSource([
        ReferenceFilePayload("stock", "stock.xlsx", _xlsx(
            ["sku", "name", "manufacturer", "stock", "branch_id"],
            [[12345, "Medicine", "Bayer Farma", 3, "1"]],
        )),
        ReferenceFilePayload("cost", "cost.xlsx", _xlsx(
            ["sku", "name", "manufacturer", "cost", "branch_id"],
            [[12345, "Medicine", "BAYER FARMA", 12, "2"]],
        )),
    ])
    parse_count = 0
    parse = imports.parse_excel_rows

    def counted_parse(content):
        nonlocal parse_count
        parse_count += 1
        return parse(content)

    monkeypatch.setattr(imports, "parse_excel_rows", counted_parse)
    import_reference_batch(db=db, source=source, selected_branch_ids=["1", "2"])
    assert parse_count == 2


def test_batch_candidate_keys_normalize_legacy_product_code():
    state = ManufacturerBatchState()
    summary = _manufacturer_summary()
    summary["manufacturer_candidates"] = 1
    _record_batch_candidates(
        state, {42: "12345"}, {42: {"BAYER FARMA"}}, summary, fillable=True,
    )
    assert state.products == {42: normalize_sku("12345")}
    assert state.candidates_by_sku == {normalize_sku("12345"): {"BAYER FARMA"}}


def test_partial_stock_candidates_still_block_conflicting_batch_fill():
    db = _db()
    product_id = _seed(db, "")
    source = ExcelReferenceSource([
        ReferenceFilePayload("stock", "stock.xlsx", _xlsx(
            ["sku", "name", "manufacturer", "stock", "branch_id"],
            [[12345, "Medicine", "Bayer Farma", 3, "1"],
             [None, "Invalid", "Bayer Farma", 2, "1"]],
        )),
        ReferenceFilePayload("cost", "cost.xlsx", _xlsx(
            ["sku", "name", "manufacturer", "cost", "branch_id"],
            [[12345, "Medicine", "Novartis", 12, "2"]],
        )),
    ])
    result = import_reference_batch(db=db, source=source, selected_branch_ids=["1", "2"])
    assert result["status"] == "partial"
    assert db.get(ProductExtra, product_id).manufacturer == ""
    assert db.scalar(select(func.count(BranchStock.id))) == 0
    assert db.scalar(select(BranchCost.cost).where(BranchCost.product_id == product_id)) == 12


def test_missing_extra_is_created_without_changing_unrelated_product_fields():
    db = _db()
    product_id = _seed(db, with_extra=False)
    job = _import(db, "stock", [[12345, "Medicine", "Bayer Farma", 7, "1"]])
    assert job.status == "success"
    assert db.get(ProductExtra, product_id).manufacturer == "BAYER FARMA"
    assert db.get(Product, product_id).cost == 17


def test_null_manufacturer_value_is_fillable_even_on_legacy_rows():
    db = _db()
    product_id = _seed(db, "")
    product = db.get(Product, product_id)
    db.get(ProductExtra, product_id).manufacturer = None
    summary = {
        "manufacturer_filled": 0, "manufacturer_already_present": 0,
        "manufacturer_conflicts": 0,
    }
    _apply_manufacturer_fallback(
        db, {product_id: product.code}, {product_id: {"BAYER FARMA"}}, summary,
    )
    db.commit()
    assert db.get(ProductExtra, product_id).manufacturer == "BAYER FARMA"
    assert summary["manufacturer_filled"] == 1


def test_partial_stock_import_does_not_fill_manufacturer(caplog):
    db = _db()
    product_id = _seed(db, "")
    with caplog.at_level(logging.INFO):
        job = _import(db, "stock", [
            [12345, "Medicine", "Bayer Farma", 3, "1"],
            [None, "Bad row", "Bayer Farma", 5, "1"],
        ])
    assert job.status == "partial"
    assert db.get(ProductExtra, product_id).manufacturer == ""
    assert db.scalar(select(func.count(BranchStock.id))) == 0
    assert "\"manufacturer_filled\": 0" in caplog.text


def test_products_reference_import_uses_same_missing_only_rule():
    db = _db()
    product_id = _seed(db, "Existing Maker")
    job = _import(db, "products", [[12345, "Updated Name", "Other Maker", "1"]])
    assert job.status == "success"
    assert db.get(Product, product_id).name == "Updated Name"
    assert db.get(ProductExtra, product_id).manufacturer == "Existing Maker"


def test_products_reference_import_fills_missing_manufacturer():
    db = _db()
    product_id = _seed(db, "-")
    job = _import(db, "products", [[12345, "Updated Name", "Bayer Farma", "1"]])
    assert job.status == "success"
    assert db.get(ProductExtra, product_id).manufacturer == "BAYER FARMA"


@pytest.mark.parametrize("incoming", [None, "", "   ", "-"])
def test_products_excel_blank_manufacturer_never_erases_existing(incoming):
    db = _db()
    product_id = _seed(db, "Existing Maker")
    import_products_excel(db=db, content=_xlsx(
        ["sku", "name", "stock", "manufacturer", "cost"],
        [[12345, "Medicine", 8, incoming, 20]],
    ))
    assert db.get(ProductExtra, product_id).manufacturer == "Existing Maker"


def test_products_excel_valid_manufacturer_keeps_existing_update_behavior():
    db = _db()
    product_id = _seed(db, "Existing Maker")
    import_products_excel(db=db, content=_xlsx(
        ["sku", "name", "stock", "manufacturer", "cost"],
        [[12345, "Medicine", 8, "New Maker", 20]],
    ))
    assert db.get(ProductExtra, product_id).manufacturer == "New Maker"


def test_reference_fallback_does_not_refresh_derived_normalization():
    db = _db()
    product_id = _seed(db, "")
    db.add(InternalProductNormalized(product_id=product_id, raw_name="Medicine", raw_manufacturer=""))
    db.commit()
    _import(db, "stock", [[12345, "Medicine", "Bayer Farma", 2, "1"]])
    assert db.get(ProductExtra, product_id).manufacturer == "BAYER FARMA"
    assert db.scalar(select(InternalProductNormalized.raw_manufacturer).where(InternalProductNormalized.product_id == product_id)) == ""


def test_reference_fallback_does_not_write_pricing_or_matching_tables():
    db = _db()
    _seed(db, "")
    writes = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        sql = statement.strip().lower()
        if sql.startswith(("insert", "update", "delete")) and any(
            table in sql for table in (
                "calculated_prices", "competitor_price_list_items", "competitors_prices",
                "source_goods_matches", "competitor_price_percentiles",
            )
        ):
            writes.append(sql)

    event.listen(db.get_bind(), "before_cursor_execute", capture)
    try:
        _import(db, "stock", [[12345, "Medicine", "Bayer Farma", 2, "1"]])
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)
    assert writes == []
