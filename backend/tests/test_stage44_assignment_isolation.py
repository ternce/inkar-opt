from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import main
from backend.app.db import Base
from backend.app.models import (
    CompetitorPrice,
    CompetitorPriceList,
    CompetitorPriceListItem,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    Product,
)
from backend.app.services.competitor_assignments import get_assigned_competitor_price_lists
from backend.app.services.competitor_persist import _ensure_price_format
from backend.app.services.competitor_price_lists import list_competitor_price_lists
from backend.app.services.competitor_read_models import refresh_price_list_item_counters
from backend.app.services.emit_worker import _recalculate_percentiles_for_emit_rows
from backend.app.services.pricing import resolve_competitor_price


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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


def _seed_price_list(
    db,
    owner: PriceFormat,
    *,
    source_type: str,
    source_key: str,
    name: str,
    branch_id: str = "1108",
) -> CompetitorPriceList:
    row = CompetitorPriceList(
        price_format_id=owner.id,
        source_type=source_type,
        source_key=source_key,
        display_name=name,
        supplier=name,
        branch_id=branch_id,
        branch_code=branch_id,
        branch_name=name,
        competitor_name=name,
        account_id="4",
        account_login="stage44",
        external_price_list_id=branch_id,
        last_refresh_status="success",
    )
    db.add(row)
    db.flush()
    db.add(
        CompetitorPriceListItem(
            price_list_id=row.id,
            provisor_goods_id=101,
            filial_id=int(branch_id) if branch_id.isdigit() else None,
            name=f"{name} item",
            distributor_goods_id="SKU-101",
            distributor_goods_name=f"{name} item",
            distributor_price=Decimal("10.00"),
            stock=1,
        )
    )
    db.flush()
    refresh_price_list_item_counters(db=db, price_list_ids=[int(row.id)])
    return row


def _seed_pool(db):
    owner = PriceFormat(code="OLD", name="Old", branch="")
    assigned_pf = PriceFormat(code="ASSIGNED", name="Assigned", branch="")
    product = Product(code="SKU-101", name="Product 101", cost=100, provisor_goods_id=101)
    db.add_all([owner, assigned_pf, product])
    db.flush()
    provisor = _seed_price_list(
        db,
        owner,
        source_type="provisor",
        source_key="account:4:128",
        name="Provisor A",
        branch_id="128",
    )
    emit = _seed_price_list(
        db,
        owner,
        source_type="provisor",
        source_key="emit:1108",
        name="Emit A",
        branch_id="1108",
    )
    vidman = _seed_price_list(
        db,
        owner,
        source_type="vidman",
        source_key="account:4:main:719",
        name="Vidman Inkar",
        branch_id="719",
    )
    existing_assignment = PriceFormatCompetitorAssignment(
        price_format_id=assigned_pf.id,
        competitor_price_list_id=provisor.id,
        is_active=True,
        coefficient=1.0,
    )
    db.add(existing_assignment)
    db.commit()
    return {
        "owner": owner,
        "assigned_pf": assigned_pf,
        "product": product,
        "provisor": provisor,
        "emit": emit,
        "vidman": vidman,
    }


def _assignment_count(db) -> int:
    return int(db.scalar(select(func.count(PriceFormatCompetitorAssignment.id))) or 0)


def test_create_price_format_separates_available_from_assigned_plks(monkeypatch):
    Session = _session_factory()
    db = Session()
    seeded = _seed_pool(db)
    before_total = _assignment_count(db)
    before_existing = len(get_assigned_competitor_price_lists(db=db, price_format_id=int(seeded["assigned_pf"].id)))

    monkeypatch.setattr(main, "enqueue_percentile_preparation", lambda **_: {"status": "skipped"})
    main.app.dependency_overrides[main.get_db] = _override_db(Session)
    try:
        client = TestClient(main.app)
        created = client.post("/api/price-formats", json={"code": "NEW001", "name": "NEW001", "branch": ""})
        assert created.status_code == 200, created.text
        available = client.get("/api/price-formats/NEW001/competitor-price-lists")
        assert available.status_code == 200, available.text
        assigned = client.get("/api/price-formats/NEW001/competitor-assignments?include_summary=1")
        assert assigned.status_code == 200, assigned.text
    finally:
        main.app.dependency_overrides.clear()

    available_rows = available.json()
    assigned_payload = assigned.json()
    assert {row["id"] for row in available_rows} >= {seeded["provisor"].id, seeded["emit"].id, seeded["vidman"].id}
    assert all(row["isSelected"] is False for row in available_rows)
    assert assigned_payload["summary"]["activePhysicalPlkCount"] == 0
    assert assigned_payload["items"] == []

    check = Session()
    assert _assignment_count(check) == before_total
    assert len(get_assigned_competitor_price_lists(db=check, price_format_id=int(seeded["assigned_pf"].id))) == before_existing


def test_ensure_price_format_keeps_provisor_and_vidman_plks_available_without_assignment():
    db = _session_factory()()
    seeded = _seed_pool(db)
    before_total = _assignment_count(db)

    ensured = _ensure_price_format(db, "ENSURE44")
    db.commit()

    assert _assignment_count(db) == before_total
    assert get_assigned_competitor_price_lists(db=db, price_format_id=int(ensured.id)) == []
    available = list_competitor_price_lists(db=db, price_format_code="ENSURE44")
    assert {row["id"] for row in available} >= {seeded["provisor"].id, seeded["vidman"].id}
    assert all(row["isSelected"] is False for row in available)


def test_emit_refresh_recalculates_only_existing_assignments_and_never_creates_new_ones():
    db = _session_factory()()
    seeded = _seed_pool(db)
    before_total = _assignment_count(db)

    result = _recalculate_percentiles_for_emit_rows(
        db,
        price_list_ids=[int(seeded["emit"].id)],
        price_format_code="OLD",
        scope_to_price_list_ids=True,
    )

    assert _assignment_count(db) == before_total
    assert result["assigned_price_format_ids"] == []
    assert result["assignment_propagation"] == {}
    assert {row["code"] for row in result["warnings"]} == {"emit_no_active_format_assignment"}


def test_explicit_assignment_and_unassignment_still_work(monkeypatch):
    Session = _session_factory()
    db = Session()
    seeded = _seed_pool(db)
    db.add(PriceFormat(code="EXPLICIT44", name="Explicit", branch=""))
    db.commit()

    monkeypatch.setattr(main, "enqueue_percentile_preparation", lambda **_: {"status": "skipped"})
    main.app.dependency_overrides[main.get_db] = _override_db(Session)
    try:
        client = TestClient(main.app)
        assigned = client.post(
            "/api/price-formats/EXPLICIT44/competitor-assignments",
            json={"sourceId": int(seeded["provisor"].id), "coefficient": 1.0},
        )
        assert assigned.status_code == 200, assigned.text
        rows = client.get("/api/price-formats/EXPLICIT44/competitor-assignments").json()
        assert [int(row["id"]) for row in rows] == [seeded["provisor"].id]

        deleted = client.delete(f"/api/price-formats/EXPLICIT44/competitor-assignments/{seeded['provisor'].id}")
        assert deleted.status_code == 200, deleted.text
        rows_after_delete = client.get("/api/price-formats/EXPLICIT44/competitor-assignments").json()
        assert rows_after_delete == []
    finally:
        main.app.dependency_overrides.clear()


def test_zero_assignment_pricing_does_not_fall_back_to_available_competitors():
    db = _session_factory()()
    seeded = _seed_pool(db)
    pf = PriceFormat(code="ZERO44", name="Zero", branch="")
    db.add(pf)
    db.flush()
    source_name = f"{seeded['provisor'].source_type}:{seeded['provisor'].source_key}"
    db.add_all(
        [
            CompetitorPrice(price_format_id=pf.id, product_id=None, source_name=source_name, supplier="Provisor A"),
            CompetitorPrice(price_format_id=pf.id, product_id=seeded["product"].id, source_name=source_name, source_price=50),
        ]
    )
    db.commit()

    resolved = resolve_competitor_price(db, int(pf.id), int(seeded["product"].id), allowed_provisor_sources=set())

    assert resolved.competitor_price is None
