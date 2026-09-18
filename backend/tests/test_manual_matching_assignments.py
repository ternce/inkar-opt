from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app import main
from backend.app.deps import get_current_user
from backend.app.models import (
    AppUser, Base, CompetitorCodeMapping, CompetitorPriceList,
    CompetitorPriceListItem, ManualMatchingAssignment, Product,
)
from backend.app.services.manual_matching_assignments import (
    is_global_unmapped, manual_matching_counts, reconcile_manual_matching_assignments,
)


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(db, count=4):
    users = [AppUser(username=f"worker-{i}", role="pricing_manager", is_active=True) for i in (1, 2)]
    admin = AppUser(username="admin", role="admin", is_active=True)
    db.add_all([*users, admin])
    db.flush()
    products = [Product(code=f"SKU-{i}", name=f"Product {i}", cost=1) for i in range(count)]
    db.add_all(products)
    db.commit()
    return users, admin, products


def _owners(db):
    return dict(db.execute(select(
        ManualMatchingAssignment.product_id, ManualMatchingAssignment.assigned_user_id,
    ).order_by(ManualMatchingAssignment.product_id)).all())


@pytest.mark.parametrize("count,expected", [(4, (2, 2)), (5, (3, 2))])
def test_initial_split_even_odd_and_deterministic_tie_break(count, expected):
    db = _db()
    users, _, products = _seed(db, count)
    result = reconcile_manual_matching_assignments(db, (users[1].id, users[0].id))
    assert (result["active_user_a"], result["active_user_b"]) == expected
    assert _owners(db)[products[0].id] == min(user.id for user in users)
    assert reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))["created"] == 0


def test_completion_does_not_reshuffle_and_new_work_goes_to_less_loaded_owner():
    db = _db()
    users, _, products = _seed(db, 4)
    ids = (users[0].id, users[1].id)
    reconcile_manual_matching_assignments(db, ids)
    before = _owners(db)
    products[0].provisor_goods_id = 101
    db.commit()
    result = reconcile_manual_matching_assignments(db, ids)
    row = db.scalar(select(ManualMatchingAssignment).where(ManualMatchingAssignment.product_id == products[0].id))
    assert result["completed"] == 1
    assert row.status == "completed" and row.completed_at is not None
    new_product = Product(code="NEW", name="New", cost=1)
    db.add(new_product)
    db.commit()
    reconcile_manual_matching_assignments(db, ids)
    assert _owners(db)[new_product.id] == users[0].id
    assert all(_owners(db)[product_id] == owner for product_id, owner in before.items())


def test_unmap_reopens_same_owner_and_preserves_assignment_row():
    db = _db()
    users, _, products = _seed(db, 1)
    reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    assignment = db.scalar(select(ManualMatchingAssignment))
    original_id = assignment.id
    products[0].provisor_goods_id = 101
    db.commit()
    reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    products[0].provisor_goods_id = None
    db.commit()
    result = reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    assert result["reopened"] == 1
    assert assignment.id == original_id
    assert assignment.assigned_user_id == users[0].id
    assert assignment.status == "active"


def test_product_has_only_one_assignment():
    db = _db()
    users, _, products = _seed(db, 1)
    reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    db.add(ManualMatchingAssignment(product_id=products[0].id, assigned_user_id=users[1].id))
    with pytest.raises(IntegrityError):
        db.commit()


def test_no_candidate_product_is_assigned_and_counts_ignore_search():
    db = _db()
    users, admin, products = _seed(db, 1)
    reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    assert is_global_unmapped(db, products[0].id)
    assert manual_matching_counts(db, admin)["total_active"] == 1
    assert manual_matching_counts(db, users[0])["my_active"] == 1


def test_applicable_provisor_goods_id_is_not_an_active_manual_task():
    db = _db()
    users, admin, products = _seed(db, 1)
    products[0].provisor_goods_id = 12345
    db.commit()
    result = reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    assert result["created"] == 0
    assert not is_global_unmapped(db, products[0].id)
    assert db.scalar(select(func.count(ManualMatchingAssignment.id))) == 0
    try:
        client = _client(db, admin)
        catalog = client.get("/api/competitors/code-mappings/product-catalog?status=unmapped")
        assert catalog.status_code == 200
        assert catalog.json()["pagination"]["total"] == 0
        assert catalog.json()["metrics"][0]["unmapped"] == 0
        client = _client(db, users[0])
        assert client.get("/api/competitors/code-mappings/product-catalog").json()["items"] == []
    finally:
        main.app.dependency_overrides.clear()


def _client(db, user):
    def override_db():
        yield db

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(main.app)


def test_worker_queue_search_pagination_and_candidate_read_are_owned():
    db = _db()
    users, admin, products = _seed(db, 3)
    reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    try:
        client = _client(db, users[0])
        response = client.get("/api/competitors/code-mappings/product-catalog?status=unmapped&limit=1")
        assert response.status_code == 200
        assert response.json()["pagination"]["total"] == 2
        assert response.json()["taskCounts"]["my_active"] == 2
        assert client.get("/api/competitors/code-mappings/product-catalog?task_scope=all").status_code == 403
        assert client.get(f"/api/competitors/code-mappings/product-catalog/{products[1].id}/candidates").status_code == 403
        assert client.get("/api/competitors/code-mappings/product-catalog?q=SKU-1").json()["items"] == []
        assert client.get("/api/competitors/code-mappings/product-catalog?limit=1&page=2").status_code == 200
        client = _client(db, admin)
        assert client.get("/api/competitors/code-mappings/product-catalog").json()["pagination"]["total"] == 3
    finally:
        main.app.dependency_overrides.clear()


def test_legacy_reads_and_catalog_target_search_remain_available_to_worker():
    db = _db()
    users, _, products = _seed(db, 2)
    reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    try:
        client = _client(db, users[0])
        for path in (
            "/api/competitors/code-mappings",
            "/api/competitors/code-mappings/catalog-view",
        ):
            assert client.get(path).status_code == 200
        targets = client.get("/api/products/search?q=SKU-1")
        assert targets.status_code == 200
        assert [row["productId"] for row in targets.json()] == [products[1].id]
    finally:
        main.app.dependency_overrides.clear()


def test_wrong_owner_save_reject_unmap_and_auto_match_are_denied():
    db = _db()
    users, _, products = _seed(db, 2)
    reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    try:
        client = _client(db, users[0])
        save = client.post("/api/competitors/code-mappings", json={
            "platform": "provisor", "status": "mapped", "ourProductId": products[1].id,
            "sourceExternalKey": "555", "sourceMatchKey": "provisor:555",
        })
        assert save.status_code == 403
        reject = client.post("/api/competitors/code-mappings", json={
            "platform": "provisor", "status": "rejected", "productId": products[1].id,
            "sourceExternalKey": "555", "sourceMatchKey": "provisor:555",
        })
        assert reject.status_code == 403
        assert client.post("/api/competitors/code-mappings/product-catalog/auto-match", json={}).status_code == 403
        mapping = CompetitorCodeMapping(
            platform="provisor", source_external_key="777", source_match_key="provisor:777",
            source_name="External", status="mapped", our_product_id=products[1].id,
        )
        db.add(mapping)
        db.commit()
        assert client.post(f"/api/competitors/code-mappings/{mapping.id}/unmap").status_code == 403
        assert client.post(f"/api/competitors/code-mappings/{mapping.id}/reject").status_code == 403
    finally:
        main.app.dependency_overrides.clear()


def test_admin_reassign_and_worker_stale_save(monkeypatch):
    db = _db()
    users, admin, products = _seed(db, 1)
    reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    monkeypatch.setenv("MANUAL_MATCHING_WORKER_IDS", f"{users[0].id},{users[1].id}")
    try:
        client = _client(db, admin)
        response = client.post(f"/api/manual-matching/assignments/{products[0].id}/reassign", json={"assigned_user_id": users[1].id})
        assert response.status_code == 200
        assert _owners(db)[products[0].id] == users[1].id
        client = _client(db, users[0])
        assert client.post("/api/competitors/code-mappings", json={
            "platform": "provisor", "status": "mapped", "ourProductId": products[0].id,
            "sourceExternalKey": "5", "sourceMatchKey": "provisor:5",
        }).status_code == 403
        client = _client(db, users[1])
        first = client.post("/api/competitors/code-mappings", json={
            "platform": "provisor", "status": "mapped", "ourProductId": products[0].id,
            "sourceExternalKey": "5", "sourceMatchKey": "provisor:5",
        })
        assert first.status_code == 200
        stale = client.post("/api/competitors/code-mappings", json={
            "platform": "provisor", "status": "mapped", "ourProductId": products[0].id,
            "sourceExternalKey": "6", "sourceMatchKey": "provisor:6",
        })
        assert stale.status_code == 409
    finally:
        main.app.dependency_overrides.clear()


def test_worker_unmap_reopens_task_for_same_owner():
    db = _db()
    users, _, products = _seed(db, 1)
    reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    try:
        client = _client(db, users[0])
        saved = client.post("/api/competitors/code-mappings", json={
            "platform": "provisor", "status": "mapped", "ourProductId": products[0].id,
            "sourceExternalKey": "505", "sourceMatchKey": "provisor:505",
        })
        assert saved.status_code == 200
        assignment = db.scalar(select(ManualMatchingAssignment))
        assert assignment.status == "completed"
        assert assignment.completed_by_user_id == users[0].id
        response = client.post(f"/api/competitors/code-mappings/{saved.json()['id']}/unmap")
        assert response.status_code == 200
        assert assignment.status == "active"
        assert assignment.assigned_user_id == users[0].id
        assert client.get("/api/competitors/code-mappings/product-catalog?status=unmapped").json()["pagination"]["total"] == 1
    finally:
        main.app.dependency_overrides.clear()


def test_worker_rejects_candidate_on_own_task_without_completing_product():
    db = _db()
    users, _, products = _seed(db, 1)
    reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    try:
        client = _client(db, users[0])
        result = client.post("/api/competitors/code-mappings", json={
            "platform": "provisor", "status": "rejected", "productId": products[0].id,
            "sourceExternalKey": "909", "sourceMatchKey": "provisor:909",
        })
        assert result.status_code == 200
        assert db.scalar(select(ManualMatchingAssignment.status)) == "active"
        assert is_global_unmapped(db, products[0].id)
    finally:
        main.app.dependency_overrides.clear()


def test_admin_filters_show_completed_history_and_unassigned_tasks():
    db = _db()
    users, admin, products = _seed(db, 3)
    reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    owner_id = _owners(db)[products[0].id]
    products[0].provisor_goods_id = 999
    db.commit()
    reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    new_product = Product(code="UNASSIGNED", name="Unassigned", cost=1)
    db.add(new_product)
    db.commit()
    try:
        client = _client(db, admin)
        by_owner = client.get(f"/api/competitors/code-mappings/product-catalog?task_scope=user&assignee_user_id={owner_id}")
        assert by_owner.status_code == 200
        assert any(row["productId"] == products[0].id and row["assignmentStatus"] == "completed" for row in by_owner.json()["items"])
        unassigned = client.get("/api/competitors/code-mappings/product-catalog?task_scope=unassigned")
        assert [row["productId"] for row in unassigned.json()["items"]] == [new_product.id]
        assert unassigned.json()["taskCounts"]["unassigned_active"] == 1
    finally:
        main.app.dependency_overrides.clear()


def test_admin_reconcile_endpoint_requires_admin_and_explicit_workers(monkeypatch):
    db = _db()
    users, admin, _ = _seed(db, 2)
    monkeypatch.delenv("MANUAL_MATCHING_WORKER_IDS", raising=False)
    try:
        client = _client(db, users[0])
        assert client.post("/api/manual-matching/reconcile", json={
            "worker_a_id": users[0].id, "worker_b_id": users[1].id,
        }).status_code == 403
        client = _client(db, admin)
        result = client.post("/api/manual-matching/reconcile", json={
            "worker_a_id": users[0].id, "worker_b_id": users[1].id,
        })
        assert result.status_code == 200
        assert result.json()["created"] == 2
        assert client.post("/api/manual-matching/reconcile", json={
            "worker_a_id": users[0].id, "worker_b_id": users[1].id,
        }).json()["created"] == 0
    finally:
        main.app.dependency_overrides.clear()


def test_assignment_survives_price_list_item_replacement():
    db = _db()
    users, _, products = _seed(db, 1)
    reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    before = _owners(db)
    price_list = CompetitorPriceList(source_type="provisor", source_key="test", price_date=date.today())
    db.add(price_list)
    db.flush()
    item = CompetitorPriceListItem(price_list_id=price_list.id, provisor_goods_id=123, name="Candidate")
    db.add(item)
    db.commit()
    db.delete(item)
    db.commit()
    db.add(CompetitorPriceListItem(price_list_id=price_list.id, provisor_goods_id=123, name="Candidate 2"))
    db.commit()
    reconcile_manual_matching_assignments(db, (users[0].id, users[1].id))
    assert _owners(db) == before


def test_concurrent_reconciliation_cannot_duplicate_assignment(tmp_path):
    db_path = tmp_path / "assignments.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 10})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        users, _, products = _seed(db, 1)
        ids = (users[0].id, users[1].id)
        product_id = products[0].id

    def run():
        with Session(engine) as db:
            try:
                reconcile_manual_matching_assignments(db, ids)
            except (IntegrityError, OperationalError):
                db.rollback()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: run(), range(2)))
    with Session(engine) as db:
        assert db.scalar(select(func.count(ManualMatchingAssignment.id)).where(
            ManualMatchingAssignment.product_id == product_id,
        )) == 1
