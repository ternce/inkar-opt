from __future__ import annotations

from datetime import datetime, timedelta
import io

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import main
from backend.app.db import Base
from backend.app.deps import ROLE_ADMIN, ROLE_PRICING_MANAGER, get_db
from backend.app.models import AppUser, CalculatedPrice, PriceFormat, PriceList, Product, ProductExtra, UserBranchAssignment


def _client_with_reports_data(*, role: str = ROLE_PRICING_MANAGER, user_branch: str = "Астана"):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override_db():
        with Session() as db:
            yield db

    user = AppUser(id=1, username="reports-user", role=role, is_active=True)
    if role != ROLE_ADMIN:
        user.branches = [UserBranchAssignment(user_id=1, branch_id="2" if user_branch == "Астана" else user_branch.lower(), branch_name=user_branch)]

    main.app.dependency_overrides[get_db] = override_db
    main.app.dependency_overrides[main.get_current_user] = lambda: user

    now = datetime(2026, 9, 8, 9, 0, 0)
    ids: dict[str, int] = {}
    with Session() as db:
        long_name = "VIP/Very*Long[Sheet]:Name?WithForbiddenSymbolsAAAA"
        pf_vip = PriceFormat(code="AST-VIP", name=long_name, branch="Астана", sap_category="VIP", price_list_type="gpl")
        pf_cat1 = PriceFormat(code="AST-CAT1", name=long_name, branch="Астана", sap_category="1", price_list_type="gpl")
        pf_alm = PriceFormat(code="ALM-CAT1", name="Almaty Category 1", branch="Алматы", sap_category="1", price_list_type="gpl")
        db.add_all([pf_vip, pf_cat1, pf_alm])
        db.flush()
        ids.update({"pf_vip": pf_vip.id, "pf_cat1": pf_cat1.id, "pf_alm": pf_alm.id})

        pl_vip_old = PriceList(number="AST-VIP-OLD", price_format_id=pf_vip.id, status="generated", created_at=now - timedelta(days=2))
        pl_vip_prev = PriceList(number="AST-VIP-PREV", price_format_id=pf_vip.id, status="generated", created_at=now - timedelta(days=1))
        pl_vip_latest = PriceList(number="AST-VIP-LATEST", price_format_id=pf_vip.id, status="generated", created_at=now)
        pl_cat1_prev = PriceList(number="AST-CAT1-PREV", price_format_id=pf_cat1.id, status="generated", created_at=now - timedelta(days=1))
        pl_cat1_latest = PriceList(number="AST-CAT1-LATEST", price_format_id=pf_cat1.id, status="generated", created_at=now + timedelta(minutes=1))
        pl_alm = PriceList(number="ALM-LATEST", price_format_id=pf_alm.id, status="generated", created_at=now)
        db.add_all([pl_vip_old, pl_vip_prev, pl_vip_latest, pl_cat1_prev, pl_cat1_latest, pl_alm])
        db.flush()
        ids.update(
            {
                "pl_vip_old": pl_vip_old.id,
                "pl_vip_prev": pl_vip_prev.id,
                "pl_vip_latest": pl_vip_latest.id,
                "pl_cat1_prev": pl_cat1_prev.id,
                "pl_cat1_latest": pl_cat1_latest.id,
                "pl_alm": pl_alm.id,
            }
        )

        products = [
            Product(code="BOTH", name="Both Product", cost=10, top_rank=100),
            Product(code="VIPONLY", name="Vip Only", cost=10, top_rank=33),
            Product(code="CATONLY", name="Cat One Only", cost=10, top_rank=25),
            Product(code="D045", name="Decrease 045", cost=10),
            Product(code="D010", name="Decrease 010", cost=10),
            Product(code="UNCH", name="Unchanged Product", cost=10),
            Product(code="UP", name="Increase Product", cost=10),
            Product(code="ZERO", name="Zero Old", cost=10),
            Product(code="ALM", name="Almaty Product", cost=10),
        ]
        db.add_all(products)
        db.flush()
        by_code = {product.code: product for product in products}
        for product in products:
            db.add(ProductExtra(product_id=product.id, manufacturer=f"Maker {product.code}"))
        db.flush()

        def add_cp(pl: PriceList, code: str, final_price: float, zone: str = "right", rating_global: int | None = None):
            db.add(
                CalculatedPrice(
                    price_list_id=pl.id,
                    product_id=by_code[code].id,
                    cost=10,
                    base_price=200,
                    final_price=final_price,
                    competitor_price=995,
                    lowest_competitor_price=995,
                    zone=zone,
                    rating_global=rating_global,
                )
            )

        for code, old_price in {"BOTH": 1000, "VIPONLY": 800, "D045": 1000, "UNCH": 1000, "UP": 1000, "ZERO": 0}.items():
            add_cp(pl_vip_prev, code, old_price)
        for code, old_price in {"BOTH": 777, "D045": 777}.items():
            add_cp(pl_vip_old, code, old_price)
        for code, new_price, zone, rating in [
            ("BOTH", 990, "left", 10),
            ("VIPONLY", 790, "left", None),
            ("D045", 995.5, "right", None),
            ("UNCH", 1000, "right", None),
            ("UP", 1001, "right", None),
            ("ZERO", 0, "left", None),
        ]:
            add_cp(pl_vip_latest, code, new_price, zone, rating)

        for code, old_price in {"BOTH": 2000, "CATONLY": 500, "D010": 1000}.items():
            add_cp(pl_cat1_prev, code, old_price)
        for code, new_price, zone, rating in [
            ("BOTH", 1990, "left", None),
            ("CATONLY", 499, "left", None),
            ("D010", 999, "right", None),
        ]:
            add_cp(pl_cat1_latest, code, new_price, zone, rating)

        add_cp(pl_alm, "ALM", 990, "left")
        db.commit()

    return TestClient(main.app), ids


def _clear_overrides():
    main.app.dependency_overrides.pop(get_db, None)
    main.app.dependency_overrides.pop(main.get_current_user, None)


def _contexts(ids: dict[str, int]):
    return [
        {"priceFormatId": ids["pf_vip"], "priceListId": ids["pl_vip_latest"]},
        {"priceFormatId": ids["pf_cat1"], "priceListId": ids["pl_cat1_latest"]},
    ]


def test_report_contexts_return_latest_generated_price_list_per_format():
    client, ids = _client_with_reports_data()
    try:
        response = client.get("/api/reports/contexts?branch=Astana")
        assert response.status_code == 200
        payload = response.json()

        by_code = {row["priceFormat"]["code"]: row for row in payload}
        assert set(by_code) == {"AST-CAT1", "AST-VIP"}
        assert by_code["AST-VIP"]["latestPriceList"]["id"] == ids["pl_vip_latest"]
        assert by_code["AST-CAT1"]["latestPriceList"]["id"] == ids["pl_cat1_latest"]
        assert by_code["AST-VIP"]["priceFormat"]["sapCategory"] == "VIP"
        assert [row["number"] for row in by_code["AST-VIP"]["priceLists"]][:2] == ["AST-VIP-LATEST", "AST-VIP-PREV"]
    finally:
        _clear_overrides()

def test_combined_rank_1_query_uses_left_zone_and_selected_formats():
    client, ids = _client_with_reports_data()
    try:
        response = client.post("/api/reports/rank-1/query", json={"branch": "Astana", "contexts": _contexts(ids), "page": 1, "limit": 20})
        assert response.status_code == 200
        payload = response.json()
        by_material_format = {(row["material"], row["priceFormatCode"]): row for row in payload["items"]}

        assert set(by_material_format) == {("BOTH", "AST-CAT1"), ("BOTH", "AST-VIP"), ("CATONLY", "AST-CAT1"), ("VIPONLY", "AST-VIP"), ("ZERO", "AST-VIP")}
        assert by_material_format[("BOTH", "AST-VIP")]["top1500"] == 10
        assert by_material_format[("BOTH", "AST-CAT1")]["top1500"] == 100
        assert by_material_format[("CATONLY", "AST-CAT1")]["customerCategory"] == "1"
        assert by_material_format[("VIPONLY", "AST-VIP")]["customerCategory"] == "VIP"
        assert "oldPrice" not in by_material_format[("BOTH", "AST-VIP")]
        assert payload["summary"]["totalRank1"] == 5
        assert payload["context"]["selectedFormatCount"] == 2
    finally:
        _clear_overrides()


def test_combined_decreases_compare_previous_prices_within_same_price_format():
    client, ids = _client_with_reports_data()
    try:
        response = client.post("/api/reports/decreases/query", json={"branch": "Astana", "contexts": _contexts(ids), "page": 1, "limit": 20})
        assert response.status_code == 200
        payload = response.json()
        rows = {(row["material"], row["priceFormatCode"]): row for row in payload["items"]}

        assert set(rows) == {("BOTH", "AST-CAT1"), ("BOTH", "AST-VIP"), ("CATONLY", "AST-CAT1"), ("D010", "AST-CAT1"), ("D045", "AST-VIP"), ("VIPONLY", "AST-VIP")}
        assert rows[("BOTH", "AST-VIP")]["oldPrice"] == 1000.0
        assert rows[("BOTH", "AST-VIP")]["oldPrice"] != 777.0
        assert rows[("BOTH", "AST-CAT1")]["oldPrice"] == 2000.0
        assert rows[("BOTH", "AST-CAT1")]["oldPrice"] != 1000.0
        assert rows[("D045", "AST-VIP")]["decreasePercent"] == -0.0045
        assert rows[("D010", "AST-CAT1")]["decreasePercent"] == -0.001
        assert payload["summary"]["totalDecreaseKzt"] == -36.5
        previous = {ctx["priceFormatCode"]: ctx["previousPriceListNumber"] for ctx in payload["context"]["contexts"]}
        assert previous == {"AST-VIP": "AST-VIP-PREV", "AST-CAT1": "AST-CAT1-PREV"}

        searched = client.post("/api/reports/decreases/query", json={"branch": "Astana", "contexts": _contexts(ids), "q": "Maker D010", "page": 1, "limit": 20}).json()
        assert searched["total"] == 1
        assert searched["items"][0]["material"] == "D010"
    finally:
        _clear_overrides()


def test_combined_reports_support_pagination_and_selected_price_list_override():
    client, ids = _client_with_reports_data()
    try:
        page_1 = client.post("/api/reports/rank-1/query", json={"branch": "Astana", "contexts": _contexts(ids), "page": 1, "limit": 2}).json()
        page_2 = client.post("/api/reports/rank-1/query", json={"branch": "Astana", "contexts": _contexts(ids), "page": 2, "limit": 2}).json()
        assert page_1["total"] == 5
        assert len(page_1["items"]) == 2
        assert len(page_2["items"]) == 2
        assert page_1["items"] != page_2["items"]

        override = [{"priceFormatId": ids["pf_vip"], "priceListId": ids["pl_vip_old"]}]
        old_response = client.post("/api/reports/rank-1/query", json={"branch": "Astana", "contexts": override, "page": 1, "limit": 20})
        assert old_response.status_code == 200
        assert old_response.json()["total"] == 0
    finally:
        _clear_overrides()


def test_report_context_authorization_and_branch_validation():
    client, ids = _client_with_reports_data()
    try:
        denied = client.post("/api/reports/rank-1/query", json={"branch": "Astana", "contexts": [{"priceFormatId": ids["pf_alm"], "priceListId": ids["pl_alm"]}]})
        assert denied.status_code == 403
    finally:
        _clear_overrides()

    admin_client, admin_ids = _client_with_reports_data(role=ROLE_ADMIN)
    try:
        mixed = admin_client.post("/api/reports/rank-1/query", json={"branch": "Astana", "contexts": [{"priceFormatId": admin_ids["pf_alm"], "priceListId": admin_ids["pl_alm"]}]})
        assert mixed.status_code == 400

        wrong_price_list = admin_client.post("/api/reports/rank-1/query", json={"branch": "Astana", "contexts": [{"priceFormatId": admin_ids["pf_vip"], "priceListId": admin_ids["pl_cat1_latest"]}]})
        assert wrong_price_list.status_code == 400
    finally:
        _clear_overrides()


def test_combined_exports_create_one_sanitized_sheet_per_selected_format():
    client, ids = _client_with_reports_data()
    try:
        response = client.post("/api/reports/rank-1/export.xlsx", json={"branch": "Astana", "contexts": _contexts(ids)})
        assert response.status_code == 200
        wb = load_workbook(io.BytesIO(response.content), data_only=True)
        assert len(wb.sheetnames) == 2
        assert all(len(name) <= 31 for name in wb.sheetnames)
        assert all(not set(name) & set("\\/?*[]:") for name in wb.sheetnames)
        assert wb.sheetnames[0] != wb.sheetnames[1]
        for sheet in wb.worksheets:
            assert list(next(sheet.iter_rows(values_only=True))) == [label for _key, label in main.REPORT_RANK_1_HEADERS]
            assert sheet.freeze_panes == "A2"
            assert sheet.auto_filter.ref == f"A1:G{sheet.max_row}"
            if sheet.max_row > 1:
                assert "-43F" in sheet["F2"].number_format

        decrease_response = client.post("/api/reports/decreases/export.xlsx", json={"branch": "Astana", "contexts": _contexts(ids)})
        assert decrease_response.status_code == 200
        decrease_wb = load_workbook(io.BytesIO(decrease_response.content), data_only=True)
        assert len(decrease_wb.sheetnames) == 2
        for sheet in decrease_wb.worksheets:
            assert list(next(sheet.iter_rows(values_only=True))) == [label for _key, label in main.REPORT_DECREASE_HEADERS]
            assert sheet.freeze_panes == "A2"
            assert sheet.auto_filter.ref == f"A1:J{sheet.max_row}"
            if sheet.max_row > 1:
                assert "-43F" in sheet["F2"].number_format
                assert "-43F" in sheet["G2"].number_format
                assert "-43F" in sheet["H2"].number_format
                assert sheet["I2"].number_format == "0.0%"
    finally:
        _clear_overrides()
