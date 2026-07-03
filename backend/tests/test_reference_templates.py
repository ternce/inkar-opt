from __future__ import annotations

import io

import pytest
from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from openpyxl import Workbook

from backend.app.db import Base
from backend.app.models import BranchCost, BranchStock
from backend.app.services.references.imports import import_reference_excel
from backend.app.services.references.parsers import parse_excel_rows
from backend.app.services.references.templates import (
    RATING_TEMPLATE_TYPES,
    REFERENCE_TEMPLATE_COLUMNS,
    build_reference_template,
)
from backend.app.services.references.types import REFERENCE_TYPES


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.mark.parametrize(
        ("data_type", "expected_headers"),
    [
        ("cost", ["Материал", "Артикул", "Производитель", "Учетная себ"]),
        ("stock", ["Материал", "Артикул", "Производитель", "Остаток"]),
    ],
)
def test_product_reference_templates_use_canonical_headers(data_type, expected_headers):
    content = build_reference_template(data_type)

    rows, headers = parse_excel_rows(content)

    assert REFERENCE_TEMPLATE_COLUMNS[data_type] == expected_headers
    assert {"sku", "manufacturer", "name", data_type}.issubset(headers)
    assert len(rows) == 1


@pytest.mark.parametrize(
    "data_type",
    [row["code"] for row in REFERENCE_TYPES if row["code"] not in RATING_TEMPLATE_TYPES],
)
def test_reference_templates_import_without_column_mapping_errors(data_type):
    db = _session()
    content = build_reference_template(data_type)

    job = import_reference_excel(
        db=db,
        data_type=data_type,
        branch_ids=["1"],
        content=content,
        filename=f"{data_type}_import_template.xlsx",
        user_name="test",
    )

    assert job.status == "success"
    assert job.rows_success == 1
    assert job.rows_failed == 0


def _xlsx(headers: list[str], row: list[object]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    ws.append(row)
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def test_cost_file_with_short_accounting_cost_header_imports_successfully():
    db = _session()
    content = _xlsx(
        ["Материал", "Артикул", "Производитель", "Учетная себ"],
        ["000000000001234567", "Пример товара", "Пример производителя", 1250.75],
    )

    job = import_reference_excel(
        db=db,
        data_type="cost",
        branch_ids=["1"],
        content=content,
        filename="cost.xlsx",
        user_name="test",
    )

    row = db.execute(select(BranchCost)).scalars().one()
    assert job.status == "success"
    assert job.rows_success == 1
    assert row.sku == "000000000001234567"
    assert row.cost == 1250.75


@pytest.mark.parametrize("stock_header", ["Кол-во(MB-52)", "Остаток"])
def test_stock_file_with_supported_stock_header_imports_successfully(stock_header):
    db = _session()
    content = _xlsx(
        ["Материал", "Артикул", "Производитель", stock_header],
        ["000000000001234567", "Пример товара", "Пример производителя", 24],
    )

    job = import_reference_excel(
        db=db,
        data_type="stock",
        branch_ids=["1"],
        content=content,
        filename="stock.xlsx",
        user_name="test",
    )

    row = db.execute(select(BranchStock)).scalars().one()
    assert job.status == "success"
    assert job.rows_success == 1
    assert row.sku == "000000000001234567"
    assert row.stock == 24


@pytest.mark.parametrize("stock_header", ["Остаток", "Остатки", "Кол-во", "Кол-во(MB-52)", "Кол-во(MB-17)", "Кол-во(Любой текст)"])
def test_stock_import_accepts_stock_header_with_optional_warehouse_suffix(stock_header):
    rows, headers = parse_excel_rows(
        _xlsx(["Материал", "Артикул", "Производитель", stock_header], ["000000000001234567", "Пример товара", "Пример производителя", 24])
    )

    assert "stock" in headers
    assert rows[0]["stock"] == 24


@pytest.mark.parametrize("data_type", sorted(RATING_TEMPLATE_TYPES))
def test_rating_templates_are_not_generated(data_type):
    with pytest.raises(ValueError):
        build_reference_template(data_type)
