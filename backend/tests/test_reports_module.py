from __future__ import annotations

from datetime import date, datetime, timedelta
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
    current_activation_date = date(2026, 9, 25)
    previous_activation_date = date(2026, 9, 24)
    ids: dict[str, int] = {}
    with Session() as db:
        long_name = "VIP/Very*Long[Sheet]:Name?WithForbiddenSymbolsAAAA"
        pf_vip = PriceFormat(code="AST-VIP", name=long_name, branch="Астана", sap_category="VIP", price_list_type="gpl")
        pf_cat1 = PriceFormat(code="AST-CAT1", name=long_name, branch="Астана", sap_category="1", price_list_type="gpl")
        pf_alm = PriceFormat(code="ALM-CAT1", name="Almaty Category 1", branch="Алматы", sap_category="1", price_list_type="gpl")
        pf_gap = PriceFormat(code="AST-GAP", name="Astana Missing Previous Date", branch="Астана", sap_category="GAP", price_list_type="gpl")
        pf_manual = PriceFormat(code="AST-MANUAL", name="Astana Historical Comparison", branch="Астана", sap_category="HIST", price_list_type="gpl")
        db.add_all([pf_vip, pf_cat1, pf_alm, pf_gap, pf_manual])
        db.flush()
        ids.update({"pf_vip": pf_vip.id, "pf_cat1": pf_cat1.id, "pf_alm": pf_alm.id, "pf_gap": pf_gap.id, "pf_manual": pf_manual.id})

        pl_vip_old = PriceList(number="AST-VIP-OLD", price_format_id=pf_vip.id, activation_date=date(2026, 9, 23), status="generated", created_at=now - timedelta(days=2))
        pl_vip_prev_early = PriceList(number="AST-VIP-PREV-EARLY", price_format_id=pf_vip.id, activation_date=previous_activation_date, status="generated", created_at=now - timedelta(days=1, hours=1))
        pl_vip_prev = PriceList(number="AST-VIP-PREV", price_format_id=pf_vip.id, activation_date=previous_activation_date, status="generated", created_at=now - timedelta(days=1))
        pl_vip_current_a = PriceList(number="AST-VIP-CURRENT-A", price_format_id=pf_vip.id, activation_date=current_activation_date, status="generated", created_at=now - timedelta(hours=2))
        pl_vip_current_b = PriceList(number="AST-VIP-CURRENT-B", price_format_id=pf_vip.id, activation_date=current_activation_date, status="generated", created_at=now - timedelta(hours=1))
        pl_vip_latest = PriceList(number="AST-VIP-LATEST", price_format_id=pf_vip.id, activation_date=current_activation_date, status="generated", created_at=now)
        pl_cat1_prev = PriceList(number="AST-CAT1-PREV", price_format_id=pf_cat1.id, activation_date=previous_activation_date, status="generated", created_at=now - timedelta(days=1))
        pl_cat1_latest = PriceList(number="AST-CAT1-LATEST", price_format_id=pf_cat1.id, activation_date=current_activation_date, status="generated", created_at=now + timedelta(minutes=1))
        pl_alm = PriceList(number="ALM-LATEST", price_format_id=pf_alm.id, activation_date=current_activation_date, status="generated", created_at=now)
        gap_previous_activation_date = date(2026, 9, 26)
        gap_current_activation_date = date(2026, 9, 29)
        pl_gap_prev_low = PriceList(number="AST-GAP-26-A", price_format_id=pf_gap.id, activation_date=gap_previous_activation_date, status="generated", created_at=now - timedelta(days=2))
        pl_gap_prev_high = PriceList(number="AST-GAP-26-B", price_format_id=pf_gap.id, activation_date=gap_previous_activation_date, status="generated", created_at=now - timedelta(days=2))
        pl_gap_prev_stale = PriceList(number="AST-GAP-26-STALE", price_format_id=pf_gap.id, activation_date=gap_previous_activation_date, status="generated", created_at=now - timedelta(days=3))
        pl_gap_current_low = PriceList(number="AST-GAP-29-A", price_format_id=pf_gap.id, activation_date=gap_current_activation_date, status="generated", created_at=now + timedelta(hours=1))
        pl_gap_current_high = PriceList(number="AST-GAP-29-B", price_format_id=pf_gap.id, activation_date=gap_current_activation_date, status="generated", created_at=now + timedelta(hours=1))
        pl_manual_26 = PriceList(number="AST-MANUAL-26", price_format_id=pf_manual.id, activation_date=date(2026, 9, 26), status="generated", created_at=now)
        pl_manual_28 = PriceList(number="AST-MANUAL-28", price_format_id=pf_manual.id, activation_date=date(2026, 9, 28), status="generated", created_at=now + timedelta(days=1))
        pl_manual_29_old = PriceList(number="AST-MANUAL-29-A", price_format_id=pf_manual.id, activation_date=date(2026, 9, 29), status="generated", created_at=now + timedelta(days=2))
        pl_manual_29 = PriceList(number="AST-MANUAL-29-B", price_format_id=pf_manual.id, activation_date=date(2026, 9, 29), status="generated", created_at=now + timedelta(days=2))
        pl_manual_30 = PriceList(number="AST-MANUAL-30", price_format_id=pf_manual.id, activation_date=date(2026, 9, 30), status="generated", created_at=now + timedelta(days=3))
        db.add_all([pl_vip_old, pl_vip_prev_early, pl_vip_prev, pl_vip_current_a, pl_vip_current_b, pl_vip_latest, pl_cat1_prev, pl_cat1_latest, pl_alm, pl_gap_prev_low, pl_gap_prev_high, pl_gap_prev_stale, pl_gap_current_low, pl_gap_current_high, pl_manual_26, pl_manual_28, pl_manual_29_old, pl_manual_29, pl_manual_30])
        db.flush()
        ids.update(
            {
                "pl_vip_old": pl_vip_old.id,
                "pl_vip_prev_early": pl_vip_prev_early.id,
                "pl_vip_prev": pl_vip_prev.id,
                "pl_vip_current_a": pl_vip_current_a.id,
                "pl_vip_current_b": pl_vip_current_b.id,
                "pl_vip_latest": pl_vip_latest.id,
                "pl_cat1_prev": pl_cat1_prev.id,
                "pl_cat1_latest": pl_cat1_latest.id,
                "pl_alm": pl_alm.id,
                "pl_gap_prev_low": pl_gap_prev_low.id,
                "pl_gap_prev_high": pl_gap_prev_high.id,
                "pl_gap_prev_stale": pl_gap_prev_stale.id,
                "pl_gap_current_low": pl_gap_current_low.id,
                "pl_gap_current_high": pl_gap_current_high.id,
                "pl_manual_26": pl_manual_26.id,
                "pl_manual_28": pl_manual_28.id,
                "pl_manual_29_old": pl_manual_29_old.id,
                "pl_manual_29": pl_manual_29.id,
                "pl_manual_30": pl_manual_30.id,
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
            Product(code="DATECASE", name="Activation Date Comparison", cost=10),
            Product(code="PREVONLY", name="Previous Only", cost=10),
            Product(code="CURRONLY", name="Current Only", cost=10),
            Product(code="GAP", name="Gap Date Product", cost=10),
            Product(code="HIST", name="Historical Comparison Product", cost=10),
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

        add_cp(pl_vip_prev_early, "DATECASE", 5500)
        for code, old_price in {"BOTH": 1000, "VIPONLY": 800, "D045": 1000, "UNCH": 1000, "UP": 1000, "ZERO": 0, "DATECASE": 5400, "PREVONLY": 700}.items():
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
            ("DATECASE", 5000, "right", None),
            ("CURRONLY", 600, "right", None),
        ]:
            add_cp(pl_vip_latest, code, new_price, zone, rating)
        add_cp(pl_vip_current_a, "DATECASE", 5100)
        add_cp(pl_vip_current_b, "DATECASE", 5000)

        for code, old_price in {"BOTH": 2000, "CATONLY": 500, "D010": 1000}.items():
            add_cp(pl_cat1_prev, code, old_price)
        for code, new_price, zone, rating in [
            ("BOTH", 1990, "left", None),
            ("CATONLY", 499, "left", None),
            ("D010", 999, "right", None),
        ]:
            add_cp(pl_cat1_latest, code, new_price, zone, rating)

        add_cp(pl_alm, "ALM", 990, "left")
        add_cp(pl_gap_prev_low, "GAP", 710, "right")
        add_cp(pl_gap_prev_high, "GAP", 700, "right")
        add_cp(pl_gap_prev_stale, "GAP", 720, "right")
        add_cp(pl_gap_current_low, "GAP", 650, "right")
        add_cp(pl_gap_current_high, "GAP", 640, "left")
        add_cp(pl_manual_26, "HIST", 900, "left")
        add_cp(pl_manual_28, "HIST", 850, "left")
        add_cp(pl_manual_29_old, "HIST", 825, "left")
        add_cp(pl_manual_29, "HIST", 800, "left")
        add_cp(pl_manual_30, "HIST", 700, "left")
        db.commit()

    return TestClient(main.app), ids


def _clear_overrides():
    main.app.dependency_overrides.pop(get_db, None)
    main.app.dependency_overrides.pop(main.get_current_user, None)


def _contexts(ids: dict[str, int]):
    return [
        {"priceFormatId": ids["pf_vip"]},
        {"priceFormatId": ids["pf_cat1"]},
    ]


def test_report_contexts_return_latest_generated_price_list_per_format():
    client, ids = _client_with_reports_data()
    try:
        response = client.get("/api/reports/contexts?branch=Astana")
        assert response.status_code == 200
        payload = response.json()

        by_code = {row["priceFormat"]["code"]: row for row in payload}
        assert set(by_code) == {"AST-CAT1", "AST-GAP", "AST-MANUAL", "AST-VIP"}
        assert by_code["AST-VIP"]["latestPriceList"]["id"] == ids["pl_vip_latest"]
        assert by_code["AST-CAT1"]["latestPriceList"]["id"] == ids["pl_cat1_latest"]
        assert by_code["AST-VIP"]["priceFormat"]["sapCategory"] == "VIP"
        assert by_code["AST-VIP"]["availableActivationDates"] == ["2026-09-25", "2026-09-24", "2026-09-23"]
        assert [row["number"] for row in by_code["AST-VIP"]["latestPriceListsByActivationDate"]] == ["AST-VIP-LATEST", "AST-VIP-PREV", "AST-VIP-OLD"]
    finally:
        _clear_overrides()

def test_combined_rank_1_query_uses_left_zone_and_selected_formats():
    client, ids = _client_with_reports_data()
    try:
        response = client.post("/api/reports/rank-1/query", json={"branch": "Astana", "activationDate": "2026-09-25", "contexts": _contexts(ids), "page": 1, "limit": 20})
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
        response = client.post("/api/reports/decreases/query", json={"branch": "Astana", "activationDate": "2026-09-25", "contexts": _contexts(ids), "page": 1, "limit": 20})
        assert response.status_code == 200
        payload = response.json()
        rows = {(row["material"], row["priceFormatCode"]): row for row in payload["items"]}

        assert set(rows) == {("BOTH", "AST-CAT1"), ("BOTH", "AST-VIP"), ("CATONLY", "AST-CAT1"), ("D010", "AST-CAT1"), ("D045", "AST-VIP"), ("DATECASE", "AST-VIP"), ("VIPONLY", "AST-VIP")}
        assert rows[("BOTH", "AST-VIP")]["oldPrice"] == 1000.0
        assert rows[("BOTH", "AST-VIP")]["oldPrice"] != 777.0
        assert rows[("BOTH", "AST-CAT1")]["oldPrice"] == 2000.0
        assert rows[("BOTH", "AST-CAT1")]["oldPrice"] != 1000.0
        assert rows[("D045", "AST-VIP")]["decreasePercent"] == -0.0045
        assert rows[("D010", "AST-CAT1")]["decreasePercent"] == -0.001
        assert rows[("DATECASE", "AST-VIP")]["oldPrice"] == 5400.0
        assert rows[("DATECASE", "AST-VIP")]["newPrice"] == 5000.0
        assert rows[("DATECASE", "AST-VIP")]["decreaseKzt"] == -400.0
        assert "PREVONLY" not in {row[0] for row in rows}
        assert "CURRONLY" not in {row[0] for row in rows}
        assert payload["summary"]["totalDecreaseKzt"] == -436.5
        previous = {ctx["priceFormatCode"]: ctx["previousPriceListNumber"] for ctx in payload["context"]["contexts"]}
        assert previous == {"AST-VIP": "AST-VIP-PREV", "AST-CAT1": "AST-CAT1-PREV"}
        contexts = {ctx["priceFormatCode"]: ctx for ctx in payload["context"]["contexts"]}
        assert contexts["AST-VIP"]["currentPriceListId"] == ids["pl_vip_latest"]
        assert contexts["AST-VIP"]["previousPriceListId"] == ids["pl_vip_prev"]
        assert contexts["AST-VIP"]["currentActivationDate"] == "2026-09-25"
        assert contexts["AST-VIP"]["previousActivationDate"] == "2026-09-24"

        searched = client.post("/api/reports/decreases/query", json={"branch": "Astana", "activationDate": "2026-09-25", "contexts": _contexts(ids), "q": "Maker D010", "page": 1, "limit": 20}).json()
        assert searched["total"] == 1
        assert searched["items"][0]["material"] == "D010"
    finally:
        _clear_overrides()


def test_reports_use_previous_available_date_and_latest_versions_for_query_and_export():
    client, ids = _client_with_reports_data()
    context = [{"priceFormatId": ids["pf_gap"], "priceListId": ids["pl_gap_current_low"]}]
    try:
        rank = client.post(
            "/api/reports/rank-1/query",
            json={"branch": "Astana", "activationDate": "2026-09-29", "contexts": context},
        )
        decreases = client.post(
            "/api/reports/decreases/query",
            json={"branch": "Astana", "activationDate": "2026-09-29", "contexts": context},
        )
        exported = client.post(
            "/api/reports/decreases/export.xlsx",
            json={"branch": "Astana", "activationDate": "2026-09-29", "contexts": context},
        )
    finally:
        _clear_overrides()

    assert rank.status_code == 200
    assert rank.json()["items"][0]["material"] == "GAP"
    rank_context = rank.json()["context"]["contexts"][0]
    assert rank_context["currentPriceListId"] == ids["pl_gap_current_high"]
    assert rank_context["currentActivationDate"] == "2026-09-29"
    assert rank_context["previousPriceListId"] == ids["pl_gap_prev_high"]
    assert rank_context["previousActivationDate"] == "2026-09-26"
    assert decreases.status_code == 200
    assert decreases.json()["items"][0]["material"] == "GAP"
    assert decreases.json()["items"][0]["oldPrice"] == 700.0
    assert decreases.json()["items"][0]["newPrice"] == 640.0
    assert exported.status_code == 200
    workbook = load_workbook(io.BytesIO(exported.content), data_only=True)
    export_row = next(workbook.active.iter_rows(min_row=2, values_only=True))
    assert export_row[2] == "GAP"
    assert export_row[5:8] == (640, 700, -60)


def test_decreases_are_empty_when_no_earlier_price_list_exists():
    client, ids = _client_with_reports_data()
    context = [{"priceFormatId": ids["pf_vip"], "priceListId": ids["pl_vip_old"]}]
    try:
        response = client.post(
            "/api/reports/decreases/query",
            json={"branch": "Astana", "contexts": context},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["context"]["contexts"][0]["previousPriceListId"] is None


def test_manual_comparisons_use_exact_dates_and_latest_versions():
    client, ids = _client_with_reports_data()
    cases = [
        ("2026-09-29", "2026-09-30", 800.0, 700.0, ids["pl_manual_29"], ids["pl_manual_30"]),
        ("2026-09-28", "2026-09-29", 850.0, 800.0, ids["pl_manual_28"], ids["pl_manual_29"]),
        ("2026-09-26", "2026-09-29", 900.0, 800.0, ids["pl_manual_26"], ids["pl_manual_29"]),
    ]
    try:
        for previous_date, current_date, old_price, new_price, previous_id, current_id in cases:
            response = client.post(
                "/api/reports/decreases/query",
                json={
                    "branch": "Astana",
                    "currentActivationDate": current_date,
                    "previousActivationDate": previous_date,
                    "contexts": [{"priceFormatId": ids["pf_manual"]}],
                },
            )
            assert response.status_code == 200
            payload = response.json()
            assert [(row["material"], row["oldPrice"], row["newPrice"]) for row in payload["items"]] == [("HIST", old_price, new_price)]
            context = payload["context"]["contexts"][0]
            assert context["comparisonMode"] == "manual"
            assert context["requestedPreviousActivationDate"] == previous_date
            assert context["requestedCurrentActivationDate"] == current_date
            assert context["previousPriceListId"] == previous_id
            assert context["currentPriceListId"] == current_id
            assert context["comparisonAvailable"] is True
    finally:
        _clear_overrides()


def test_manual_missing_previous_is_unavailable_without_fallback_and_rank_still_works():
    client, ids = _client_with_reports_data()
    body = {
        "branch": "Astana",
        "currentActivationDate": "2026-09-29",
        "previousActivationDate": "2026-09-28",
        "contexts": [
            {"priceFormatId": ids["pf_manual"]},
            {"priceFormatId": ids["pf_gap"]},
        ],
    }
    try:
        rank = client.post("/api/reports/rank-1/query", json=body)
        decreases = client.post("/api/reports/decreases/query", json=body)
    finally:
        _clear_overrides()

    assert rank.status_code == 200
    assert {(row["material"], row["priceFormatCode"]) for row in rank.json()["items"]} == {
        ("HIST", "AST-MANUAL"),
        ("GAP", "AST-GAP"),
    }
    assert decreases.status_code == 200
    assert [(row["material"], row["priceFormatCode"]) for row in decreases.json()["items"]] == [("HIST", "AST-MANUAL")]
    contexts = {row["priceFormatCode"]: row for row in decreases.json()["context"]["contexts"]}
    assert contexts["AST-MANUAL"]["comparisonAvailable"] is True
    assert contexts["AST-GAP"]["previousPriceListId"] is None
    assert contexts["AST-GAP"]["previousAvailable"] is False
    assert contexts["AST-GAP"]["comparisonAvailable"] is False
    assert "28.09.2026" in contexts["AST-GAP"]["warnings"][0]


def test_manual_date_validation_and_current_date_alias_compatibility():
    client, ids = _client_with_reports_data()
    context = [{"priceFormatId": ids["pf_manual"]}]
    try:
        equal_aliases = client.post(
            "/api/reports/rank-1/query",
            json={"branch": "Astana", "activationDate": "2026-09-30", "currentActivationDate": "2026-09-30", "contexts": context},
        )
        conflict = client.post(
            "/api/reports/rank-1/query",
            json={"branch": "Astana", "activationDate": "2026-09-29", "currentActivationDate": "2026-09-30", "contexts": context},
        )
        equal_dates = client.post(
            "/api/reports/decreases/query",
            json={"branch": "Astana", "currentActivationDate": "2026-09-30", "previousActivationDate": "2026-09-30", "contexts": context},
        )
        reversed_dates = client.post(
            "/api/reports/decreases/query",
            json={"branch": "Astana", "currentActivationDate": "2026-09-29", "previousActivationDate": "2026-09-30", "contexts": context},
        )
        malformed = client.post(
            "/api/reports/decreases/query",
            json={"branch": "Astana", "currentActivationDate": "30.09.2026", "previousActivationDate": "2026-09-29", "contexts": context},
        )
    finally:
        _clear_overrides()

    assert equal_aliases.status_code == 200
    assert equal_aliases.json()["context"]["comparisonMode"] == "auto"
    assert conflict.status_code == 400
    assert "must be equal" in conflict.json()["detail"]
    assert equal_dates.status_code == 400
    assert reversed_dates.status_code == 400
    assert malformed.status_code == 400
    assert "currentActivationDate" in malformed.json()["detail"]


def test_manual_decrease_query_and_export_use_the_same_pair():
    client, ids = _client_with_reports_data()
    body = {
        "branch": "Astana",
        "currentActivationDate": "2026-09-30",
        "previousActivationDate": "2026-09-29",
        "contexts": [{"priceFormatId": ids["pf_manual"]}],
    }
    try:
        queried = client.post("/api/reports/decreases/query", json=body)
        exported = client.post("/api/reports/decreases/export.xlsx", json=body)
    finally:
        _clear_overrides()

    assert queried.status_code == 200
    assert queried.json()["items"][0]["oldPrice"] == 800.0
    assert queried.json()["items"][0]["newPrice"] == 700.0
    assert exported.status_code == 200
    workbook = load_workbook(io.BytesIO(exported.content), data_only=True)
    export_row = next(workbook.active.iter_rows(min_row=2, values_only=True))
    assert export_row[2] == "HIST"
    assert export_row[5:8] == (700, 800, -100)


def test_combined_reports_support_pagination_and_selected_price_list_override():
    client, ids = _client_with_reports_data()
    try:
        page_1 = client.post("/api/reports/rank-1/query", json={"branch": "Astana", "activationDate": "2026-09-25", "contexts": _contexts(ids), "page": 1, "limit": 2}).json()
        page_2 = client.post("/api/reports/rank-1/query", json={"branch": "Astana", "activationDate": "2026-09-25", "contexts": _contexts(ids), "page": 2, "limit": 2}).json()
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
        response = client.post("/api/reports/rank-1/export.xlsx", json={"branch": "Astana", "activationDate": "2026-09-25", "contexts": _contexts(ids)})
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

        decrease_response = client.post("/api/reports/decreases/export.xlsx", json={"branch": "Astana", "activationDate": "2026-09-25", "contexts": _contexts(ids)})
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
        vip_sheet = next(sheet for sheet in decrease_wb.worksheets if any(row[2].value == "DATECASE" for row in sheet.iter_rows(min_row=2)))
        date_row = next(row for row in vip_sheet.iter_rows(min_row=2) if row[2].value == "DATECASE")
        assert date_row[5].value == 5000
        assert date_row[6].value == 5400
        assert date_row[7].value == -400
    finally:
        _clear_overrides()
