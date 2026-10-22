from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app import main
from backend.app.db import Base
from backend.app.deps import get_db
from backend.app.models import (
    AppUser,
    CompetitorPriceList,
    MarkupTemplate,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    PricingRule,
    RoundingRule,
)
from backend.app.services.auth import hash_password
from backend.app.services.price_formats import GeneratedPriceFormatCode


def _session_factory(url: str):
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _override_db(Session):
    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    return override


def _seed_assignment_fixture(Session) -> dict[str, int]:
    with Session() as db:
        db.add_all(
            [
                AppUser(username="dev-admin", display_name="dev-admin", role="admin", is_active=True),
                AppUser(username="user-1", display_name="user-1", role="admin", is_active=True),
                AppUser(username="user-2", display_name="user-2", role="admin", is_active=True),
                AppUser(username="user-3", display_name="user-3", role="admin", is_active=True),
            ]
        )
        format_a = PriceFormat(code="CONC-A", name="CONC-A", branch="")
        format_b = PriceFormat(code="CONC-B", name="CONC-B", branch="")
        format_c = PriceFormat(code="CONC-C", name="CONC-C", branch="")
        format_same = PriceFormat(code="CONC-SAME", name="CONC-SAME", branch="")
        db.add_all([format_a, format_b, format_c, format_same])
        db.flush()
        plk_x = CompetitorPriceList(
            price_format_id=format_a.id,
            source_type="manual",
            source_key="concurrent:x",
            display_name="PLK X",
            supplier="PLK X",
            competitor_name="PLK X",
            price_coefficient=Decimal("1.00"),
            last_refresh_status="success_zero_items",
        )
        plk_y = CompetitorPriceList(
            price_format_id=format_b.id,
            source_type="manual",
            source_key="concurrent:y",
            display_name="PLK Y",
            supplier="PLK Y",
            competitor_name="PLK Y",
            price_coefficient=Decimal("1.00"),
            last_refresh_status="success_zero_items",
        )
        plk_z = CompetitorPriceList(
            price_format_id=format_c.id,
            source_type="manual",
            source_key="concurrent:z",
            display_name="PLK Z",
            supplier="PLK Z",
            competitor_name="PLK Z",
            price_coefficient=Decimal("1.00"),
            last_refresh_status="success_zero_items",
        )
        plk_same_1 = CompetitorPriceList(
            price_format_id=format_same.id,
            source_type="manual",
            source_key="concurrent:same:1",
            display_name="PLK Same 1",
            supplier="PLK Same 1",
            competitor_name="PLK Same 1",
            price_coefficient=Decimal("1.00"),
            last_refresh_status="success_zero_items",
        )
        plk_same_2 = CompetitorPriceList(
            price_format_id=format_same.id,
            source_type="manual",
            source_key="concurrent:same:2",
            display_name="PLK Same 2",
            supplier="PLK Same 2",
            competitor_name="PLK Same 2",
            price_coefficient=Decimal("1.00"),
            last_refresh_status="success_zero_items",
        )
        db.add_all([plk_x, plk_y, plk_z, plk_same_1, plk_same_2])
        db.commit()
        return {
            "format_a_id": int(format_a.id),
            "format_b_id": int(format_b.id),
            "format_c_id": int(format_c.id),
            "format_same_id": int(format_same.id),
            "plk_x_id": int(plk_x.id),
            "plk_y_id": int(plk_y.id),
            "plk_z_id": int(plk_z.id),
            "plk_same_1_id": int(plk_same_1.id),
            "plk_same_2_id": int(plk_same_2.id),
        }


def _active_assignment_ids(Session, price_format_id: int) -> list[int]:
    with Session() as db:
        return [
            int(row.competitor_price_list_id)
            for row in db.execute(
                select(PriceFormatCompetitorAssignment)
                .where(PriceFormatCompetitorAssignment.price_format_id == price_format_id)
                .where(PriceFormatCompetitorAssignment.is_active.is_(True))
                .order_by(PriceFormatCompetitorAssignment.competitor_price_list_id.asc())
            ).scalars()
        ]


def test_concurrent_assignments_do_not_share_process_memory(tmp_path, monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setattr(main, "enqueue_percentile_preparation", lambda **_: {"status": "skipped"})
    Session = _session_factory(f"sqlite:///{tmp_path / 'assignments.db'}")
    ids = _seed_assignment_fixture(Session)
    main.app.dependency_overrides[get_db] = _override_db(Session)
    try:
        client = TestClient(main.app)

        def assign(format_code: str, source_id: int, username: str = "dev-admin"):
            return client.post(
                f"/api/price-formats/{format_code}/competitor-assignments",
                json={"sourceId": source_id, "coefficient": 1.0},
                headers={"X-Dev-User": username},
            )

        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(
                pool.map(
                    lambda args: assign(*args),
                    [
                        ("CONC-A", ids["plk_x_id"], "user-1"),
                        ("CONC-B", ids["plk_y_id"], "user-2"),
                        ("CONC-C", ids["plk_z_id"], "user-3"),
                    ],
                )
            )

        assert [response.status_code for response in responses] == [200, 200, 200]
        assert _active_assignment_ids(Session, ids["format_a_id"]) == [ids["plk_x_id"]]
        assert _active_assignment_ids(Session, ids["format_b_id"]) == [ids["plk_y_id"]]
        assert _active_assignment_ids(Session, ids["format_c_id"]) == [ids["plk_z_id"]]

        with ThreadPoolExecutor(max_workers=2) as pool:
            same_format_responses = list(
                pool.map(
                    lambda source_id: assign("CONC-SAME", source_id),
                    [ids["plk_same_1_id"], ids["plk_same_2_id"]],
                )
            )

        assert [response.status_code for response in same_format_responses] == [200, 200]
        assert _active_assignment_ids(Session, ids["format_same_id"]) == [ids["plk_same_1_id"], ids["plk_same_2_id"]]

        duplicate = client.post(
            "/api/price-formats/CONC-SAME/competitor-assignments",
            json={"sourceId": ids["plk_same_1_id"], "coefficient": 1.0},
        )
        assert duplicate.status_code == 409, duplicate.text
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_production_rejects_untrusted_x_dev_user_header(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("DEV_CURRENT_USER", "prod-admin")
    monkeypatch.delenv("TRUST_X_DEV_USER_IN_PROD", raising=False)
    Session = _session_factory(f"sqlite:///{tmp_path / 'auth.db'}")
    with Session() as db:
        db.add(
            AppUser(
                username="prod-admin",
                display_name="prod-admin",
                role="admin",
                is_active=True,
                password_hash=hash_password("strong-password"),
            )
        )
        db.commit()

    main.app.dependency_overrides[get_db] = _override_db(Session)
    try:
        client = TestClient(main.app)
        assert client.get("/api/current-user").status_code == 401
        login = client.post("/api/auth/login", json={"username": "prod-admin", "password": "strong-password"})
        assert login.status_code == 200
        assert client.get("/api/current-user").json()["username"] == "prod-admin"
        blocked = client.get("/api/current-user", headers={"X-Dev-User": "another_user"})
        assert blocked.status_code == 403
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_duplicate_price_format_create_returns_409(tmp_path, monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setattr(main, "enqueue_percentile_preparation", lambda **_: {"status": "skipped"})
    monkeypatch.setattr(
        main,
        "allocate_price_format_code",
        lambda *_args, **_kwargs: GeneratedPriceFormatCode(
            code="DUP-PF",
            price_list_type="ИПЛ",
            sap_branch_code="1001",
            sequence_number=1,
        ),
    )
    Session = _session_factory(f"sqlite:///{tmp_path / 'duplicates.db'}")
    with Session() as db:
        db.add(AppUser(username="dev-admin", display_name="dev-admin", role="admin", is_active=True))
        db.add(PriceFormat(code="DUP-PF", name="DUP-PF", branch=""))
        db.commit()

    main.app.dependency_overrides[get_db] = _override_db(Session)
    try:
        client = TestClient(main.app)
        response = client.post(
            "/api/price-formats",
            json={"name": "DUP-PF", "branch": "Алматы", "priceListType": "ИПЛ"},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "price format code already exists"
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_duplicate_pricing_rule_entities_return_409(tmp_path, monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    Session = _session_factory(f"sqlite:///{tmp_path / 'pricing-rule-duplicates.db'}")
    with Session() as db:
        db.add(AppUser(username="dev-admin", display_name="dev-admin", role="admin", is_active=True))
        db.add(PricingRule(code="DUP-RULE", name="DUP-RULE"))
        db.add(MarkupTemplate(code="DUP-MARKUP", name="DUP-MARKUP"))
        db.add(RoundingRule(code="DUP-ROUND", name="DUP-ROUND", mode="math", precision=2))
        db.commit()

    main.app.dependency_overrides[get_db] = _override_db(Session)
    try:
        client = TestClient(main.app)
        pricing_rule = client.post("/api/pricing-rules", json={"code": "DUP-RULE", "name": "DUP-RULE"})
        markup = client.post("/api/pricing-rules/markup-templates", json={"code": "DUP-MARKUP", "name": "DUP-MARKUP", "rows": []})
        rounding = client.post("/api/pricing-rules/rounding-rules", json={"code": "DUP-ROUND", "name": "DUP-ROUND", "mode": "math", "precision": 2})

        assert pricing_rule.status_code == 409, pricing_rule.text
        assert markup.status_code == 409, markup.text
        assert rounding.status_code == 409, rounding.text
    finally:
        main.app.dependency_overrides.pop(get_db, None)
