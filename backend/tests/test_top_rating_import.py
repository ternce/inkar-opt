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
from backend.app.models import Product, ProductRating
from backend.app.services.references.ratings import import_top_rating_excel
from backend.app.services.sku import normalize_sku


def _xlsx(rows: list[list[object]], *, sheet_name: str = "Отчет") -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet_name
    for row in rows:
        worksheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
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


def _upload(client: TestClient, data_type: str, content: bytes, *, filename: str = "Топ.xlsm"):
    return client.post(
        f"/api/references/import?data_type={data_type}&branch_ids=101,202",
        files={"file": (filename, content, "application/vnd.ms-excel.sheet.macroEnabled.12")},
    )


def test_global_top_rating_import_accepts_template_xlsm_and_reports_all_categories():
    client, Session = _client()
    existing_sku = normalize_sku("1004044")
    invalid_rank_sku = normalize_sku("1017855")
    db = Session()
    db.add_all(
        [
            Product(code=existing_sku, name="Existing", cost=10),
            Product(code=invalid_rank_sku, name="Invalid rank product", cost=10),
        ]
    )
    db.commit()
    db.close()

    content = _xlsx(
        [
            ["№ рейтинга", "Полное имя товара (Vi-Ortis)", "Материал "],
            [1, "Existing name", 1004044],
            [2, "Missing name", "9999999"],
            [3, "Duplicate name", existing_sku],
            [0, "Invalid rank", "1017855"],
        ]
    )

    response = _upload(client, "rating_global", content)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "partial"
    assert payload["summary"] == {
        "total_rows": 4,
        "matched": 1,
        "not_found": 1,
        "duplicates": 1,
        "invalid_rows": 1,
        "updated": 1,
    }
    assert {(item["row"], item["type"]) for item in payload["errors"]} == {
        (3, "not_found"),
        (4, "duplicate"),
        (5, "invalid"),
    }

    db = Session()
    try:
        assert db.scalar(select(func.count(Product.id))) == 2
        existing = db.scalar(select(Product).where(Product.code == existing_sku))
        assert existing is not None
        assert existing.top_rank == 1
        ratings = db.scalars(
            select(ProductRating).where(ProductRating.product_id == existing.id).where(ProductRating.rating_type == "global")
        ).all()
        assert {(row.branch_id, row.rating) for row in ratings} == {("101", 1), ("202", 1)}
        assert db.scalar(select(func.count(ProductRating.id))) == 2
    finally:
        db.close()


def test_local_top_rating_import_does_not_overwrite_global_rating_or_top_rank():
    client, Session = _client()
    sku = normalize_sku("1003359")
    db = Session()
    product = Product(code=sku, name="Product", cost=10, top_rank=7)
    db.add(product)
    db.flush()
    db.add(ProductRating(branch_id="101", product_id=product.id, sku=sku, rating_type="global", rating=7))
    db.commit()
    product_id = product.id
    db.close()

    response = _upload(
        client,
        "rating_local",
        _xlsx([[" № РЕЙТИНГА ", "Полное имя товара (Vi-Ortis)", "Материал "], [12, "Product", "1003359"]]),
        filename="Топ.xlsx",
    )

    assert response.status_code == 200
    assert response.json()["summary"]["updated"] == 1
    db = Session()
    try:
        product = db.get(Product, product_id)
        assert product.top_rank == 7
        global_rating = db.scalar(
            select(ProductRating).where(ProductRating.product_id == product_id).where(ProductRating.rating_type == "global")
        )
        assert global_rating.rating == 7
        local_ratings = db.scalars(
            select(ProductRating).where(ProductRating.product_id == product_id).where(ProductRating.rating_type == "local")
        ).all()
        assert {(row.branch_id, row.rating) for row in local_ratings} == {("101", 12), ("202", 12)}
    finally:
        db.close()


@pytest.mark.parametrize(
    ("filename", "sheet_name", "expected_detail"),
    [
        ("Топ.csv", "Отчет", "Поддерживаются только файлы .xlsx и .xlsm"),
        ("Топ.xlsx", "Другой лист", 'Не найден лист "Отчет"'),
    ],
)
def test_top_rating_import_rejects_wrong_extension_or_missing_report_sheet(filename, sheet_name, expected_detail):
    client, _Session = _client()
    content = _xlsx([["№ рейтинга", "Материал"], [1, "1004044"]], sheet_name=sheet_name)

    response = _upload(client, "rating_global", content, filename=filename)

    assert response.status_code == 400
    assert response.json() == {"detail": expected_detail}


@pytest.mark.parametrize("rank", [None, 0, -1, 1.5, "abc"])
def test_top_rating_import_rejects_invalid_rank(rank):
    client, Session = _client()
    sku = normalize_sku("1004044")
    db = Session()
    db.add(Product(code=sku, name="Product", cost=10))
    db.commit()
    db.close()

    response = _upload(client, "rating_global", _xlsx([["№ рейтинга", "Материал "], [rank, "1004044"]]))

    assert response.status_code == 200
    assert response.json()["summary"]["invalid_rows"] == 1
    db = Session()
    try:
        assert db.scalar(select(func.count(ProductRating.id))) == 0
        assert db.scalar(select(Product.top_rank).where(Product.code == sku)) is None
    finally:
        db.close()


def test_top_rating_import_rolls_back_all_updates_on_database_error(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    sku = normalize_sku("1004044")
    db.add(Product(code=sku, name="Product", cost=10))
    db.commit()
    original_rollback = db.rollback
    rollback_called = False

    def fail_commit():
        raise SQLAlchemyError("forced commit failure")

    def track_rollback():
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(db, "commit", fail_commit)
    monkeypatch.setattr(db, "rollback", track_rollback)

    with pytest.raises(SQLAlchemyError, match="forced commit failure"):
        import_top_rating_excel(
            db=db,
            data_type="rating_global",
            branch_ids=["101"],
            content=_xlsx([["№ рейтинга", "Материал "], [1, "1004044"]]),
            filename="Топ.xlsm",
        )

    assert rollback_called is True
    db.close()
    check = Session()
    try:
        assert check.scalar(select(Product.top_rank).where(Product.code == sku)) is None
        assert check.scalar(select(func.count(ProductRating.id))) == 0
    finally:
        check.close()
