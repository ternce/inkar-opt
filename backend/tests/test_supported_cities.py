from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import main
from backend.app.db import Base
from backend.app.deps import ROLE_ADMIN
from backend.app.models import CompetitorPriceList, PriceFormat
from backend.app.services.references.types import BRANCHES, USER_SELECTABLE_BRANCHES
from backend.app.services.regions import (
    CITIES,
    SUPPORTED_USER_CITY_NAMES,
    allowed_provisor_source_names_for_city_id,
    canonical_supported_city_name,
    city_id_from_branch,
    is_supported_user_city_name,
)


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
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


def _client(Session, monkeypatch):
    monkeypatch.setattr(main, "enqueue_percentile_preparation", lambda **_: {"status": "skipped"})
    main.app.dependency_overrides[main.get_db] = _override_db(Session)
    main.app.dependency_overrides[main.get_current_user] = lambda: main.AppUser(id=1, username="admin", role=ROLE_ADMIN, is_active=True)
    main.app.dependency_overrides[main.require_write_access] = lambda: main.AppUser(id=1, username="admin", role=ROLE_ADMIN, is_active=True)
    return TestClient(main.app)


def test_supported_user_city_list_is_exact_business_list():
    assert list(SUPPORTED_USER_CITY_NAMES) == [
        "Алматы",
        "Астана",
        "Шымкент",
        "Атырау",
        "Есик",
        "Караганда",
        "Костанай",
        "Семей",
        "Усть-Каменогорск",
        "Павлодар",
        "Актау",
        "Актобе",
    ]
    assert len(SUPPORTED_USER_CITY_NAMES) == 12


def test_unsupported_legacy_cities_are_not_user_selectable():
    hidden = {"Кызылорда", "Петропавловск", "Талдыкорган", "Уральск", "Орал", "Нур-Султан"}
    assert hidden.isdisjoint(set(SUPPORTED_USER_CITY_NAMES))
    assert hidden.isdisjoint({row["name"] for row in USER_SELECTABLE_BRANCHES})


def test_user_selectable_branch_endpoint_returns_only_supported_cities(monkeypatch):
    Session = _session_factory()
    client = _client(Session, monkeypatch)
    try:
        response = client.get("/api/references/branches")
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert [row["name"] for row in response.json()] == list(SUPPORTED_USER_CITY_NAMES)
    assert "Кызылорда" not in {row["name"] for row in response.json()}


def test_supported_city_aliases_are_deterministic():
    cases = {
        "Алматы": "Алматы",
        " г. Алматы ": "Алматы",
        "ALMATY": "Алматы",
        "nur-sultan": "Астана",
        "Нур Султан": "Астана",
        "Усть Каменогорск": "Усть-Каменогорск",
        "ust-kamenogorsk": "Усть-Каменогорск",
        "Aktau": "Актау",
        "Кызылорда": "",
    }
    assert {value: canonical_supported_city_name(value) for value in cases} == cases
    assert is_supported_user_city_name("г Астана")
    assert not is_supported_user_city_name("Петропавловск")


def test_historical_city_mapping_rows_are_preserved_for_compatibility():
    legacy_names = {city.name for city in CITIES}
    assert {"Кызылорда", "Петропавловск", "Талдыкорган", "Уральск", "Нур-Султан"}.issubset(legacy_names)
    assert {"Кызылорда", "Петропавловск", "Талдыкорган", "Уральск"}.issubset(
        {row["name"] for row in BRANCHES}
    )

    historical = PriceFormat(code="LEGACY", name="Legacy", branch="Кызылорда")
    assert historical.branch == "Кызылорда"


def test_source_region_helpers_keep_historical_city_data_readable():
    assert not is_supported_user_city_name("Кызылорда")
    assert city_id_from_branch("Кызылорда") == 9
    assert allowed_provisor_source_names_for_city_id(9) == {"provisor:158"}


def test_new_price_format_rejects_unsupported_branch_and_canonicalizes_alias(monkeypatch):
    Session = _session_factory()
    client = _client(Session, monkeypatch)
    try:
        rejected = client.post("/api/price-formats", json={"name": "Old City", "branch": "Кызылорда", "priceListType": "ИПЛ"})
        created = client.post("/api/price-formats", json={"name": "Alias", "branch": "aktau", "priceListType": "ИПЛ"})
    finally:
        main.app.dependency_overrides.clear()

    assert rejected.status_code == 400, rejected.text
    assert created.status_code == 200, created.text
    assert created.json()["code"] == "ИПЛ_1010_001"
    db = Session()
    assert db.scalar(select(PriceFormat.branch).where(PriceFormat.code == "ИПЛ_1010_001")) == "Актау"


def test_existing_historical_price_format_can_be_resaved_unchanged(monkeypatch):
    Session = _session_factory()
    db = Session()
    db.add(PriceFormat(code="LEGACY-PF", name="Legacy PF", branch="Кызылорда"))
    db.commit()
    client = _client(Session, monkeypatch)
    try:
        response = client.put("/api/price-formats/LEGACY-PF/settings", json={"name": "Legacy PF", "branch": "Кызылорда"})
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert Session().scalar(select(PriceFormat.branch).where(PriceFormat.code == "LEGACY-PF")) == "Кызылорда"


def test_manual_plk_creation_restricts_new_branch_but_historical_reimport_preserves_existing(monkeypatch):
    Session = _session_factory()
    db = Session()
    pf = PriceFormat(code="MANUAL-PF", name="Manual PF", branch="Кызылорда")
    historical = CompetitorPriceList(
        price_format_id=1,
        source_type="manual",
        source_key="manual:legacy",
        display_name="Legacy PLK",
        supplier="Legacy",
        region="branch:Кызылорда|competitor:Legacy",
        branch_name="Кызылорда",
        competitor_name="Legacy",
        account_id="manual",
        account_login="manual",
        external_price_list_id="manual:legacy",
    )
    db.add(pf)
    db.flush()
    historical.price_format_id = pf.id
    db.add(historical)
    db.commit()

    file_payload = {"file": ("manual.csv", b"sku,name,price\nSKU-1,One,10\n", "text/csv")}
    client = _client(Session, monkeypatch)
    try:
        rejected = client.post(
            "/api/price-formats/MANUAL-PF/competitor-price-lists/manual",
            data={"name": "New", "competitor": "New", "branch": "Кызылорда"},
            files=file_payload,
        )
        reimport = client.post(
            f"/api/competitor-price-lists/{historical.id}/manual/reimport",
            data={"name": "Legacy PLK", "competitor": "Legacy", "branch": "Кызылорда"},
            files={"file": ("manual.csv", b"sku,name,price\nSKU-1,One,11\n", "text/csv")},
        )
    finally:
        main.app.dependency_overrides.clear()

    assert rejected.status_code == 400, rejected.text
    assert reimport.status_code == 200, reimport.text
    assert Session().get(CompetitorPriceList, historical.id).branch_name == "Кызылорда"
