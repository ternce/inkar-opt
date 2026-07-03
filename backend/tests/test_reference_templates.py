from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
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
        ("cost", ["SKU", "Производитель", "Наименование", "Себестоимость"]),
        ("stock", ["SKU", "Производитель", "Наименование", "Остаток"]),
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


@pytest.mark.parametrize("data_type", sorted(RATING_TEMPLATE_TYPES))
def test_rating_templates_are_not_generated(data_type):
    with pytest.raises(ValueError):
        build_reference_template(data_type)
