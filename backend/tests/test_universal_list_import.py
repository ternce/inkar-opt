from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.deps import get_db
from backend.app.main import app
from backend.app.models import BusinessList, BusinessListItem, ListItem, PriceFormat, Product, UniversalList, UniversalListPriceFormat


def _client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app), Session


def _xlsx(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    for row in rows:
        worksheet.append(row)
    bio = io.BytesIO()
    workbook.save(bio)
    return bio.getvalue()


def _upload(client: TestClient, rows: list[list[object]], list_type: str):
    return client.post(
        "/lists/import",
        data={"list_type": list_type},
        files={"file": ("list.xlsx", _xlsx(rows), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )


def _seed_products(Session):
    db = Session()
    try:
        db.add_all(
            [
                Product(code="12345", name="Product 12345", cost=1),
                Product(code="A-77", name="Article A77", cost=1),
                Product(code="PC-9", name="Product Code 9", cost=1),
                Product(code="000000000000000222", name="Padded Product", cost=1),
            ]
        )
        db.commit()
    finally:
        db.close()


def test_critical_list_with_one_zero_values():
    client, Session = _client()
    _seed_products(Session)

    response = _upload(client, [["Material", "Value"], ["12345", 1], ["A-77", 0]], "critical")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["processed"] == 2
    list_id = payload["id"]
    details = client.get(f"/lists/{list_id}").json()
    values = {item["sku"]: item["value"]["is_critical"] for item in details["items"]}
    assert values == {"12345": True, "A-77": False}


def test_markup_list_normalizes_20():
    client, Session = _client()
    _seed_products(Session)

    response = _upload(client, [["SKU", "Markup"], ["12345", 20]], "markup")

    assert response.status_code == 200
    details = client.get(f"/lists/{response.json()['id']}").json()
    assert details["items"][0]["value"]["markup_percent"] == 20.0
    assert details["items"][0]["value"]["display"] == "20%"


def test_markup_list_normalizes_20_percent():
    client, Session = _client()
    _seed_products(Session)

    response = _upload(client, [["SKU", "Markup"], ["12345", "20%"]], "markup")

    assert response.status_code == 200
    details = client.get(f"/lists/{response.json()['id']}").json()
    assert details["items"][0]["value"]["markup_percent"] == 20.0
    assert details["items"][0]["value"]["display"] == "20%"


def test_markup_list_normalizes_fraction():
    client, Session = _client()
    _seed_products(Session)

    response = _upload(client, [["SKU", "Markup"], ["12345", 0.2]], "markup")

    assert response.status_code == 200
    details = client.get(f"/lists/{response.json()['id']}").json()
    assert details["items"][0]["value"]["markup_percent"] == 20.0
    assert details["items"][0]["value"]["display"] == "20%"


def test_markup_list_normalizes_one_as_one_percent():
    client, Session = _client()
    _seed_products(Session)

    response = _upload(client, [["SKU", "Markup"], ["12345", 1]], "markup")

    assert response.status_code == 200
    details = client.get(f"/lists/{response.json()['id']}").json()
    assert details["items"][0]["value"]["markup_percent"] == 1.0
    assert details["items"][0]["value"]["display"] == "1%"


def test_markup_list_normalizes_integer_percents():
    client, Session = _client()
    _seed_products(Session)

    response = _upload(client, [["SKU", "Markup"], ["12345", 2], ["A-77", 20]], "markup")

    assert response.status_code == 200
    details = client.get(f"/lists/{response.json()['id']}").json()
    values = {item["sku"]: item["value"]["markup_percent"] for item in details["items"]}
    assert values == {"12345": 2.0, "A-77": 20.0}


def test_shuffled_column_order_and_product_code_lookup():
    client, Session = _client()
    _seed_products(Session)

    response = _upload(client, [["Value", "Manufacturer", "Product Code"], [1, "Maker", "PC-9"]], "exclusion")

    assert response.status_code == 200
    details = client.get(f"/lists/{response.json()['id']}").json()
    assert details["items"][0]["sku"] == "PC-9"
    assert details["items"][0]["manufacturer"] == "Maker"
    assert details["items"][0]["value"]["is_excluded"] is True


def test_duplicate_products_last_row_wins():
    client, Session = _client()
    _seed_products(Session)

    response = _upload(client, [["Material", "Critical"], ["12345", 0], ["12345", 1]], "critical")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["processed"] == 1
    assert payload["summary"]["duplicates"] == 1
    details = client.get(f"/lists/{payload['id']}").json()
    assert details["items"][0]["value"]["is_critical"] is True
    assert details["errors"][0]["code"] == "duplicate_row"


def test_unknown_sku_reported():
    client, Session = _client()
    _seed_products(Session)

    response = _upload(client, [["SKU", "Value"], ["UNKNOWN", 1]], "critical")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["not_found"] == 1
    assert payload["summary"]["processed"] == 0
    assert payload["errors"][0]["code"] == "product_not_found"


def test_empty_file_rejected():
    client, _Session = _client()

    response = _upload(client, [], "critical")

    assert response.status_code == 400
    assert "file without headers" in response.json()["detail"] or "empty file" in response.json()["detail"]


def test_file_without_headers_rejected():
    client, _Session = _client()

    response = _upload(client, [[None, None], ["12345", 1]], "critical")

    assert response.status_code == 400
    assert "file without headers" in response.json()["detail"]


def test_upload_size_limit_rejected(monkeypatch):
    client, _Session = _client()
    monkeypatch.setenv("LIST_IMPORT_MAX_UPLOAD_SIZE_MB", "1")

    response = client.post(
        "/lists/import",
        data={"list_type": "critical"},
        files={"file": ("list.xlsx", b"x" * (1024 * 1024 + 1), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

    assert response.status_code == 400
    assert "LIST_IMPORT_MAX_UPLOAD_SIZE_MB=1" in response.json()["detail"]


def test_row_limit_rejected_and_does_not_persist(monkeypatch):
    client, Session = _client()
    _seed_products(Session)
    monkeypatch.setenv("LIST_IMPORT_MAX_ROWS", "1")

    response = _upload(client, [["SKU", "Value"], ["12345", 1], ["A-77", 1]], "critical")

    assert response.status_code == 400
    assert "LIST_IMPORT_MAX_ROWS=1" in response.json()["detail"]
    db = Session()
    try:
        assert db.scalar(select(func.count(BusinessList.id))) == 0
        assert db.scalar(select(func.count(BusinessListItem.id))) == 0
    finally:
        db.close()


def test_database_error_rolls_back_and_returns_normalized_error(monkeypatch):
    client, Session = _client()
    _seed_products(Session)

    def broken_commit(self):
        raise SQLAlchemyError("forced commit failure")

    monkeypatch.setattr(Session.class_, "commit", broken_commit)

    response = _upload(client, [["SKU", "Value"], ["12345", 1]], "critical")

    assert response.status_code == 500
    assert response.json()["detail"] == "database error during list import"
    db = Session()
    try:
        assert db.scalar(select(func.count(BusinessList.id))) == 0
        assert db.scalar(select(func.count(BusinessListItem.id))) == 0
    finally:
        db.close()


def test_delete_list_removes_items():
    client, Session = _client()
    _seed_products(Session)
    response = _upload(client, [["SKU", "Value"], ["12345", 1]], "critical")
    list_id = response.json()["id"]

    delete_response = client.delete(f"/lists/{list_id}")

    assert delete_response.status_code == 200
    db = Session()
    try:
        assert db.scalar(select(BusinessList).where(BusinessList.id == list_id)) is None
        assert db.scalar(select(BusinessListItem).where(BusinessListItem.business_list_id == list_id)) is None
    finally:
        db.close()


def test_lists_management_patch_accepts_dd_mm_yyyy_and_format_codes():
    client, Session = _client()
    db = Session()
    try:
        price_format_1 = PriceFormat(code="0001", name="Format 1", branch="A")
        price_format_2 = PriceFormat(code="003", name="Format 3", branch="B")
        universal_list = UniversalList(code="OLD", name="Old", type="fixed_price", status="active")
        db.add_all([price_format_1, price_format_2, universal_list])
        db.commit()
        list_id = universal_list.id
    finally:
        db.close()

    response = client.patch(
        f"/api/lists-management/{list_id}",
        json={
            "name": "Критичка 1",
            "code": "01",
            "type": "critical_markup",
            "active": False,
            "startDate": "13.06.2026",
            "endDate": "14.06.2026",
            "formatCodes": ["0001", "003"],
        },
    )

    assert response.status_code == 200
    db = Session()
    try:
        saved = db.get(UniversalList, list_id)
        assert saved is not None
        assert saved.name == "Критичка 1"
        assert saved.code == "01"
        assert saved.type != "fixed_price"
        assert saved.start_date.isoformat() == "2026-06-13"
        assert saved.end_date.isoformat() == "2026-06-14"
        assert db.scalar(select(func.count(UniversalListPriceFormat.id))) == 2
    finally:
        db.close()


def test_lists_management_patch_bad_date_returns_json_400():
    client, Session = _client()
    db = Session()
    try:
        universal_list = UniversalList(code="01", name="List", type="critical_markup", status="active")
        db.add(universal_list)
        db.commit()
        list_id = universal_list.id
    finally:
        db.close()

    response = client.patch(f"/api/lists-management/{list_id}", json={"startDate": "13/06/2026"})

    assert response.status_code == 400
    assert response.json() == {"detail": "date must be YYYY-MM-DD or DD.MM.YYYY"}


def test_lists_management_import_excel_adds_items_to_existing_list():
    client, Session = _client()
    _seed_products(Session)
    create_response = client.post(
        "/api/lists-management",
        json={"code": "01", "name": "Критичка 1", "type": "critical_markup", "active": True},
    )
    assert create_response.status_code == 200
    list_id = create_response.json()["id"]

    response = client.post(
        f"/api/lists-management/{list_id}/import-excel",
        files={
            "file": (
                "critical.xlsx",
                _xlsx([["SKU", "Value"], ["12345", "20%"], ["UNKNOWN", "10%"], ["A-77", 0.3]]),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["list_id"] == list_id
    assert payload["summary"]["total_rows"] == 3
    assert payload["summary"]["processed"] == 2
    assert payload["summary"]["not_found"] == 1
    assert payload["item_count"] == 2

    card = client.get(f"/api/lists-management/{list_id}").json()
    assert card["itemsCount"] == 2
    values = {item["sku"]: item["value"] for item in card["items"]}
    assert values == {"12345": 20.0, "A-77": 30.0}
    db = Session()
    try:
        assert db.scalar(select(func.count(BusinessList.id))) == 0
        assert db.scalar(select(func.count(ListItem.id)).where(ListItem.universal_list_id == list_id)) == 2
    finally:
        db.close()


@pytest.mark.parametrize(
    ("list_type", "raw_value", "expected_value", "expected_display"),
    [
        ("critical_markup", 1, 1.0, "1%"),
        ("fixed_markup", 1, 1.0, "1%"),
        ("fixed_markup", 33, 33.0, "33%"),
        ("fixed_markup", 0.2, 20.0, "20%"),
        ("fixed_price", 2500, 2500.0, "2500"),
        ("fixed_price", 120.5, 120.5, "120.5"),
        ("min_price", 1200, 1200.0, "1200"),
        ("max_price", 3000, 3000.0, "3000"),
        ("min_markup", 2, 2.0, "2%"),
        ("max_markup", 0.2, 20.0, "20%"),
        ("percentile_override", 0.2, 20.0, "20%"),
        ("exclude_from_pricing", 1, 1.0, "Да"),
        ("exclude_from_pricing", "нет", 0.0, "Нет"),
        ("no_bend", "да", 1.0, "Да"),
        ("no_bend", 0, 0.0, "Нет"),
    ],
)
def test_lists_management_import_excel_normalizes_by_selected_list_type(list_type, raw_value, expected_value, expected_display):
    client, Session = _client()
    _seed_products(Session)
    create_response = client.post(
        "/api/lists-management",
        json={"code": f"UL-{list_type}", "name": list_type, "type": list_type, "active": True},
    )
    assert create_response.status_code == 200
    list_id = create_response.json()["id"]

    response = client.post(
        f"/api/lists-management/{list_id}/import-excel",
        files={
            "file": (
                "list.xlsx",
                _xlsx([["Material", "Value"], ["12345", raw_value]]),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 200
    assert response.json()["summary"]["processed"] == 1
    card = client.get(f"/api/lists-management/{list_id}").json()
    assert card["items"][0]["value"] == expected_value
    assert card["items"][0]["valueDisplay"] == expected_display
    db = Session()
    try:
        saved = db.get(UniversalList, list_id)
        assert saved is not None
        assert saved.type == list_type
    finally:
        db.close()
