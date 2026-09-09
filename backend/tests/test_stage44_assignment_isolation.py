from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import main
from backend.app.db import Base
from backend.app.deps import ROLE_ADMIN
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
from backend.app.services.competitor_price_lists import list_competitor_price_lists, upsert_unified_price_list
from backend.app.services.competitor_read_models import refresh_price_list_item_counters
from backend.app.services.emit_worker import _recalculate_percentiles_for_emit_rows
from backend.app.services.pricing import resolve_competitor_price
from backend.app.services.price_sources import UnifiedPriceItem, UnifiedPriceList


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


def _override_auth():
    user = lambda: main.AppUser(id=1, username="admin", role=ROLE_ADMIN, is_active=True)
    main.app.dependency_overrides[main.get_current_user] = user
    main.app.dependency_overrides[main.require_write_access] = user


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


def _unified_price_list(
    *,
    account_id: str = "44",
    account_login: str = "aktau-account",
    price_list_id: str = "9001",
    branch_name: str = "Актау",
    competitor_name: str = "Global PLK",
    source_updated_at: str = "2026-09-04T10:00:00",
) -> UnifiedPriceList:
    return UnifiedPriceList(
        source="provisor",
        account_id=account_id,
        account_login=account_login,
        price_list_id=price_list_id,
        price_list_name=competitor_name,
        distributor_name=competitor_name,
        branch_id=price_list_id,
        branch_code=price_list_id,
        branch_name=branch_name,
        competitor_name=competitor_name,
        source_updated_at=source_updated_at,
    )


def _unified_item(
    *,
    account_id: str = "44",
    price_list_id: str = "9001",
    goods_id: int = 101,
    sku: str = "SKU-101",
    price: str = "10.00",
) -> UnifiedPriceItem:
    return UnifiedPriceItem(
        source="provisor",
        account_id=account_id,
        price_list_id=price_list_id,
        price_list_name="Global PLK",
        distributor_name="Global PLK",
        product_name="Product 101",
        manufacturer="Manufacturer",
        registration_number="REG",
        distributor_product_name="Product 101",
        distributor_product_id=sku,
        distributor_price=Decimal(price),
        stock=Decimal("1"),
        pack_quantity=Decimal("1"),
        expiry_date="2027-01-31",
        raw={"id": goods_id * 10, "goodsId": goods_id},
    )


def test_create_price_format_separates_available_from_assigned_plks(monkeypatch):
    Session = _session_factory()
    db = Session()
    seeded = _seed_pool(db)
    before_total = _assignment_count(db)
    before_existing = len(get_assigned_competitor_price_lists(db=db, price_format_id=int(seeded["assigned_pf"].id)))

    monkeypatch.setattr(main, "enqueue_percentile_preparation", lambda **_: {"status": "skipped"})
    main.app.dependency_overrides[main.get_db] = _override_db(Session)
    _override_auth()
    try:
        client = TestClient(main.app)
        created = client.post("/api/price-formats", json={"name": "NEW001", "branch": "Алматы", "priceListType": "ИПЛ"})
        assert created.status_code == 200, created.text
        format_code = created.json()["code"]
        available = client.get(f"/api/price-formats/{format_code}/competitor-price-lists")
        assert available.status_code == 200, available.text
        assigned = client.get(f"/api/price-formats/{format_code}/competitor-assignments?include_summary=1")
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


def test_new_provisor_plk_is_global_available_without_assignment():
    db = _session_factory()()
    owner = PriceFormat(code="OWNER-N", name="Owner", branch="Актау")
    target = PriceFormat(code="TARGET-N", name="Target", branch="Алматы")
    product = Product(code="SKU-101", name="Product 101", cost=100, provisor_goods_id=101)
    db.add_all([owner, target, product])
    db.commit()
    before_total = _assignment_count(db)

    price_list = upsert_unified_price_list(
        db=db,
        price_format_code=owner.code,
        price_list=_unified_price_list(branch_name="Актау", price_list_id="9001"),
        items=[_unified_item(price_list_id="9001")],
        status="updated",
        run_matching=True,
    )

    available = list_competitor_price_lists(db=db, price_format_code=target.code)
    by_id = {int(row["id"]): row for row in available}
    assert int(price_list.id) in by_id
    assert by_id[int(price_list.id)]["isSelected"] is False
    assert by_id[int(price_list.id)]["sourceUpdatedAt"] == "2026-09-04T10:00:00"
    assert by_id[int(price_list.id)]["lastSuccessAt"]
    assert _assignment_count(db) == before_total


def test_new_emit_source_is_global_available_without_assignment():
    db = _session_factory()()
    owner = PriceFormat(code="OWNER-O", name="Owner", branch="Атырау")
    target = PriceFormat(code="TARGET-O", name="Target", branch="Астана")
    db.add_all([owner, target])
    db.flush()
    emit = _seed_price_list(
        db,
        owner,
        source_type="provisor",
        source_key="emit:1106",
        name="Emit 1106",
        branch_id="1106",
    )
    db.commit()
    before_total = _assignment_count(db)

    available = list_competitor_price_lists(db=db, price_format_code=target.code)
    by_id = {int(row["id"]): row for row in available}

    assert int(emit.id) in by_id
    assert by_id[int(emit.id)]["isSelected"] is False
    assert _assignment_count(db) == before_total


def test_new_global_plk_does_not_participate_until_selected():
    db = _session_factory()()
    owner = PriceFormat(code="OWNER-P", name="Owner", branch="")
    target = PriceFormat(code="TARGET-P", name="Target", branch="")
    product = Product(code="SKU-101", name="Product 101", cost=100, provisor_goods_id=101)
    db.add_all([owner, target, product])
    db.commit()

    upsert_unified_price_list(
        db=db,
        price_format_code=owner.code,
        price_list=_unified_price_list(price_list_id="9002"),
        items=[_unified_item(price_list_id="9002", price="9.00")],
        status="updated",
        run_matching=True,
    )

    resolved = resolve_competitor_price(db, int(target.id), int(product.id), allowed_provisor_sources=set())
    assert resolved.competitor_price is None


def test_refreshing_selected_source_reenqueues_selected_price_formats(monkeypatch):
    import backend.app.services.competitor_price_lists as service

    db = _session_factory()()
    owner = PriceFormat(code="OWNER-Q", name="Owner", branch="")
    selected = PriceFormat(code="SELECTED-Q", name="Selected", branch="")
    unselected = PriceFormat(code="UNSELECTED-Q", name="Unselected", branch="")
    product = Product(code="SKU-101", name="Product 101", cost=100, provisor_goods_id=101)
    db.add_all([owner, selected, unselected, product])
    db.commit()
    source = upsert_unified_price_list(
        db=db,
        price_format_code=owner.code,
        price_list=_unified_price_list(price_list_id="9003"),
        items=[_unified_item(price_list_id="9003", price="10.00")],
        status="updated",
        run_matching=True,
    )
    db.add(
        PriceFormatCompetitorAssignment(
            price_format_id=selected.id,
            competitor_price_list_id=source.id,
            is_active=True,
            coefficient=1.0,
        )
    )
    db.commit()
    enqueued: list[int] = []
    rebuilt: list[int] = []
    monkeypatch.setattr(service, "enqueue_percentile_preparation", lambda *, price_format_id, **_: enqueued.append(int(price_format_id)) or {"status": "pending"})
    monkeypatch.setattr(service, "rebuild_competitor_prices_for_selected", lambda *, price_format_id, **_: rebuilt.append(int(price_format_id)) or {"ok": True})

    upsert_unified_price_list(
        db=db,
        price_format_code=owner.code,
        price_list=_unified_price_list(price_list_id="9003", source_updated_at="2026-09-04T11:00:00"),
        items=[_unified_item(price_list_id="9003", price="11.00")],
        status="updated",
        run_matching=True,
    )

    assert rebuilt == [int(selected.id)]
    assert enqueued == [int(selected.id)]
    assert int(unselected.id) not in rebuilt


def test_refreshing_unselected_source_does_not_alter_unrelated_price_format(monkeypatch):
    import backend.app.services.competitor_price_lists as service

    db = _session_factory()()
    owner = PriceFormat(code="OWNER-R", name="Owner", branch="")
    unrelated = PriceFormat(code="UNRELATED-R", name="Unrelated", branch="")
    product = Product(code="SKU-101", name="Product 101", cost=100, provisor_goods_id=101)
    db.add_all([owner, unrelated, product])
    db.commit()
    selected_source = _seed_price_list(db, owner, source_type="provisor", source_key="account:1:plk:1", name="Selected R", branch_id="1")
    db.add(
        PriceFormatCompetitorAssignment(
            price_format_id=unrelated.id,
            competitor_price_list_id=selected_source.id,
            is_active=True,
            coefficient=1.0,
        )
    )
    db.commit()
    calls: list[int] = []
    monkeypatch.setattr(service, "rebuild_competitor_prices_for_selected", lambda *, price_format_id, **_: calls.append(int(price_format_id)) or {"ok": True})
    monkeypatch.setattr(service, "enqueue_percentile_preparation", lambda **_: {"status": "pending"})

    upsert_unified_price_list(
        db=db,
        price_format_code=owner.code,
        price_list=_unified_price_list(price_list_id="9004"),
        items=[_unified_item(price_list_id="9004", price="12.00")],
        status="updated",
        run_matching=True,
    )

    assert calls == []


def test_account_region_does_not_limit_newly_parsed_plk_availability():
    db = _session_factory()()
    owner = PriceFormat(code="OWNER-S", name="Owner", branch="Актау")
    target = PriceFormat(code="TARGET-S", name="Target", branch="Атырау")
    product = Product(code="SKU-101", name="Product 101", cost=100, provisor_goods_id=101)
    db.add_all([owner, target, product])
    db.commit()

    price_list = upsert_unified_price_list(
        db=db,
        price_format_code=owner.code,
        price_list=_unified_price_list(account_login="aktau-login", branch_name="Алматы", price_list_id="9005"),
        items=[_unified_item(price_list_id="9005")],
        status="updated",
        run_matching=True,
    )

    available = list_competitor_price_lists(db=db, price_format_code=target.code, region="Атырау")
    by_id = {int(row["id"]): row for row in available}
    assert int(price_list.id) in by_id
    assert by_id[int(price_list.id)]["visibleForFormatBranch"] is False
    assert by_id[int(price_list.id)]["branchMismatchReason"].startswith("branch_mismatch:")


def test_emit_refresh_recalculates_global_catalog_and_never_creates_new_assignments():
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
    assert sorted(result["assigned_price_format_ids"]) == sorted([int(seeded["owner"].id), int(seeded["assigned_pf"].id)])
    assert result["assignment_propagation"] == {}
    assert result["warnings"] == []


def test_explicit_assignment_and_unassignment_still_work(monkeypatch):
    Session = _session_factory()
    db = Session()
    seeded = _seed_pool(db)
    db.add(PriceFormat(code="EXPLICIT44", name="Explicit", branch=""))
    db.commit()

    monkeypatch.setattr(main, "enqueue_percentile_preparation", lambda **_: {"status": "skipped"})
    main.app.dependency_overrides[main.get_db] = _override_db(Session)
    _override_auth()
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
