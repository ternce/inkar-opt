from __future__ import annotations

import io

from openpyxl import Workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import (
    CompetitorPriceList,
    CompetitorPriceListItem,
    ManualPriceListImport,
    ManualPriceListImportError,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    Product,
)
from backend.app.services.manual_price_list_import import (
    import_manual_price_list,
    list_manual_import_errors,
    list_manual_import_history,
    parse_manual_price_list,
    preview_manual_price_list,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(db):
    pf = PriceFormat(code="FMT", name="Format", branch="Almaty")
    db.add(pf)
    db.add_all(
        [
            Product(code="000123", name="Aspirin", cost=10),
            Product(code="SKU-2", name="Citramon", cost=12),
        ]
    )
    db.commit()
    return pf


def _xlsx(rows, *, second_sheet: bool = True) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Cover"
    ws.append(["ignored"])
    target = wb.create_sheet("Supplier") if second_sheet else ws
    target.append(["SKU", "Наименование", "Цена"])
    for row in rows:
        target.append(row)
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def test_preview_detects_header_on_non_first_xlsx_sheet_and_reports_duplicates():
    content = _xlsx(
        [
            ["000123", "Aspirin", "12,50"],
            ["SKU-2", "Citramon", 20],
            ["SKU-2", "Citramon", 20],
            ["SKU-3", "Bad", 0],
        ]
    )

    preview = preview_manual_price_list(content=content, filename="supplier.xlsx")

    assert preview["mode"] == "dry_run"
    assert preview["sheet"] == "Supplier"
    assert preview["totalRows"] == 4
    assert preview["validRows"] == 2
    assert preview["duplicateRows"] == 1
    assert preview["invalidRows"] == 1
    assert preview["sampleRows"][0]["sku"] == "000123"


def test_csv_cp1251_semicolon_parses_price_comma():
    content = "SKU;Наименование;Цена\n000123;Аспирин;13,75\n".encode("cp1251")

    parsed = parse_manual_price_list(content, "manual.csv")

    assert parsed.encoding == "cp1251"
    assert parsed.delimiter == ";"
    assert len(parsed.valid_rows) == 1
    assert str(parsed.valid_rows[0].price) == "13.7500"


def test_import_creates_stable_manual_source_and_preserves_assignment_on_reimport():
    db = _session()
    _seed(db)

    created = import_manual_price_list(
        db=db,
        price_format_code="FMT",
        content=_xlsx([["000123", "Aspirin", 12.5], ["SKU-2", "Citramon", 20]], second_sheet=False),
        filename="manual.xlsx",
        display_name="Manual Almaty",
        competitor_name="Supplier A",
        branch_name="Almaty",
    )
    price_list_id = created["id"]
    first_source_key = created["sourceKey"]
    row = db.get(CompetitorPriceList, price_list_id)
    assert row is not None
    assert row.source_type == "manual"
    assert first_source_key.startswith("manual:")
    assert created["persistedRows"] == 2

    assignment = PriceFormatCompetitorAssignment(
        price_format_id=row.price_format_id,
        competitor_price_list_id=row.id,
        is_active=True,
        coefficient=1.25,
    )
    db.add(assignment)
    db.commit()

    updated = import_manual_price_list(
        db=db,
        price_format_code="FMT",
        price_list_id=price_list_id,
        content=_xlsx([["SKU-2", "Citramon", 21]], second_sheet=False),
        filename="manual-new.xlsx",
        display_name="Manual Almaty",
    )

    assert updated["id"] == price_list_id
    assert updated["sourceKey"] == first_source_key
    items = db.execute(select(CompetitorPriceListItem).where(CompetitorPriceListItem.price_list_id == price_list_id)).scalars().all()
    assert len(items) == 1
    assert items[0].distributor_goods_id == "SKU-2"
    kept_assignment = db.get(PriceFormatCompetitorAssignment, assignment.id)
    assert kept_assignment is not None
    assert kept_assignment.is_active is True
    assert float(kept_assignment.coefficient) == 1.25


def test_failed_reimport_records_error_history_without_replacing_snapshot():
    db = _session()
    _seed(db)
    created = import_manual_price_list(
        db=db,
        price_format_code="FMT",
        content=_xlsx([["SKU-2", "Citramon", 20]], second_sheet=False),
        filename="manual.xlsx",
    )
    price_list_id = created["id"]

    failed = import_manual_price_list(
        db=db,
        price_format_code="FMT",
        price_list_id=price_list_id,
        content=_xlsx([["BAD", "No price", 0]], second_sheet=False),
        filename="bad.xlsx",
    )

    assert failed["ok"] is False
    assert failed["status"] == "error"
    items = db.execute(select(CompetitorPriceListItem).where(CompetitorPriceListItem.price_list_id == price_list_id)).scalars().all()
    assert len(items) == 1
    assert items[0].distributor_goods_id == "SKU-2"
    imports = list_manual_import_history(db=db, price_list_id=price_list_id)
    assert imports[0]["status"] == "error"
    assert imports[0]["preservedPreviousSnapshot"] is True


def test_conflicting_duplicate_sku_excluded_and_error_persisted():
    db = _session()
    _seed(db)
    result = import_manual_price_list(
        db=db,
        price_format_code="FMT",
        content=_xlsx([["SKU-2", "Citramon", 20], ["SKU-2", "Other", 21]], second_sheet=False),
        filename="conflict.xlsx",
    )

    assert result["ok"] is False
    assert result["conflictingDuplicateSkus"] == 1
    assert db.execute(select(CompetitorPriceListItem)).scalars().first() is None
    import_row = db.execute(select(ManualPriceListImport)).scalars().first()
    assert import_row is not None
    errors = list_manual_import_errors(db=db, import_id=import_row.id)
    assert {error["errorCode"] for error in errors} == {"duplicate_sku_conflict"}
    assert db.execute(select(ManualPriceListImportError)).scalars().first() is not None
