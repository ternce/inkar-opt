from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from backend.app import main
from backend.app.db import Base
from backend.app.deps import require_admin_access
from backend.app.models import (
    AppUser,
    BranchStock,
    CompetitorCodeMapping,
    Product,
    ProductExtra,
    ReferenceImportJob,
    ReferenceUpdateStatus,
    SapMatchingSourceBatch,
    SapMatchingSourceItem,
    StockSnapshot,
    StockSnapshotItem,
)
from backend.app.services.provisor_goods import parse_goods_response
from backend.app.services import sap_provisor_matching
from backend.app.services.sap_provisor_matching import (
    SapProvisorQueueUnavailable,
    confirm_sap_provisor_mapping,
    import_sap_matching_rows,
    list_unmatched_sap_products,
)
from backend.app.services.references.sap_balance import SAP_PLANTS
from backend.app.timezone import now_kz_naive


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _admin() -> AppUser:
    return AppUser(id=1, username="sap-admin", role="admin", is_active=True)


@pytest.fixture(scope="module")
def postgres_feature_sessions():
    url = os.getenv("TEST_POSTGRES_URL", "").strip()
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required for PostgreSQL transaction validation")
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    admin = create_engine(url, poolclass=NullPool)
    schema = "sap_provisor_" + uuid.uuid4().hex
    with admin.begin() as connection:
        assert int(connection.scalar(select(1))) == 1
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(
        url,
        poolclass=NullPool,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    # Create only the tables touched by this feature. The empty competitor
    # tables are deliberately minimal because mapping propagation performs
    # only bounded SELECT/UPDATE statements against these columns.
    for table in (
        Product.__table__,
        ReferenceImportJob.__table__,
        SapMatchingSourceBatch.__table__,
        SapMatchingSourceItem.__table__,
        CompetitorCodeMapping.__table__,
    ):
        table.create(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE competitor_price_lists (id SERIAL PRIMARY KEY, source_type VARCHAR(64) NOT NULL)"
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE competitor_price_list_items (
                id SERIAL PRIMARY KEY,
                price_list_id INTEGER NOT NULL,
                product_id INTEGER,
                provisor_goods_id BIGINT,
                distributor_goods_id TEXT,
                normalized_name TEXT,
                normalized_manufacturer TEXT,
                match_key TEXT,
                match_type VARCHAR(64),
                matched_sku TEXT,
                match_score NUMERIC(18, 4)
            )
            """
        )
    try:
        yield sessionmaker(bind=engine)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.dispose()


def _active_snapshot(
    db,
    *,
    plant: str,
    branch: str,
    products: list[Product],
    age_days: int = 0,
    lgort: str = "1000",
    raw_by_product: dict[int, dict] | None = None,
):
    batch = SapMatchingSourceBatch(
        source_version=f"test-{plant}-{branch}-{lgort}",
        source_type="test",
        plant=plant,
        lgort=lgort,
        effective_date=now_kz_naive().date() - timedelta(days=age_days),
        payload_sha256=(f"{plant}-{branch}-{lgort}".encode().hex() + "0" * 64)[:64],
        active=True,
        record_count=len(products),
        imported_by="test",
    )
    db.add(batch)
    db.flush()
    db.add_all([
        SapMatchingSourceItem(
            batch_id=batch.id,
            material=product.code,
            normalized_sku=product.code,
            product_id=product.id,
            product_name=product.name,
            manufacturer="",
            identity_status="resolved",
            raw_json=json.dumps(
                (raw_by_product or {}).get(product.id, {})
                if (raw_by_product or {}).get(product.id, {}).get("carry_forward") is True
                else {
                    "plant": plant,
                    "lgort": lgort,
                    **(raw_by_product or {}).get(product.id, {}),
                }
            ),
        )
        for product in products
        if not (raw_by_product or {}).get(product.id, {}).get("carry_forward")
    ])
    db.flush()
    return batch


def test_unmatched_queue_uses_active_sap_population_deduplicates_and_excludes_both_mapping_paths():
    db = _session()
    unmatched = Product(code="000000000000000001", name="Аспирин 100 мг №20 таб", cost=1)
    second = Product(code="000000000000000002", name="Ибупрофен 200 мг №10 таб", cost=1)
    direct = Product(code="000000000000000003", name="Direct", cost=1, provisor_goods_id=300)
    canonical = Product(code="000000000000000004", name="Canonical", cost=1)
    outside = Product(code="000000000000000005", name="Not in SAP", cost=1)
    db.add_all([unmatched, second, direct, canonical, outside])
    db.flush()
    db.add(ProductExtra(product_id=unmatched.id, manufacturer="Bayer"))
    _active_snapshot(db, plant="1001", branch="Алматы", products=[unmatched, second, direct, canonical])
    _active_snapshot(db, plant="1004", branch="Есик", products=[unmatched])
    db.add(CompetitorCodeMapping(
        platform="provisor",
        source_external_key="400",
        source_match_key="provisor:400",
        our_product_id=canonical.id,
        our_sku=canonical.code,
        status="mapped",
    ))
    db.commit()

    first = list_unmatched_sap_products(db, page=1, page_size=25)
    assert first["pagination"]["total"] == 2
    assert [row["sapSku"] for row in first["items"]] == [unmatched.code, second.code]
    assert first["items"][0]["sapManufacturer"] == "Bayer"
    assert first["items"][0]["packageCount"] == 20
    assert set(first["sourceHealth"]["availablePlants"]) == {"1001", "1004"}
    assert "1002" in first["sourceHealth"]["missingPlants"]

    searched = list_unmatched_sap_products(db, q="000000000000000002", page=1, page_size=50)
    assert [row["productId"] for row in searched["items"]] == [second.id]


def test_historical_5266_import_is_idempotent_and_does_not_touch_manual_stock():
    db = _session()
    products = [Product(code=str(index).zfill(18), name=f"SAP {index}", cost=1) for index in range(1, 5267)]
    products[0].provisor_goods_id = 88001
    db.add_all(products)
    db.flush()
    db.add(CompetitorCodeMapping(
        platform="provisor", source_external_key="88002", source_match_key="provisor:88002",
        our_product_id=products[1].id, our_sku=products[1].code, status="mapped",
    ))
    manual_snapshot = StockSnapshot(
        branch_id="manual-branch", source_type="manual", effective_date=now_kz_naive().date(),
        status="active", item_count=1, mapped_product_count=1, content_fingerprint="manual",
        published_at=now_kz_naive(),
    )
    db.add(manual_snapshot)
    db.flush()
    db.add(BranchStock(
        branch_id="manual-branch", product_id=products[0].id, sku=products[0].code,
        stock=7, source_type="manual", snapshot_id=manual_snapshot.id,
    ))
    db.add(ReferenceUpdateStatus(
        branch_id="manual-branch", data_type="stock", status="fresh",
        active_stock_snapshot_id=manual_snapshot.id, active_source_type="manual",
    ))
    db.commit()
    before = (
        db.scalar(select(func.count()).select_from(BranchStock)),
        db.scalar(select(ReferenceUpdateStatus.active_stock_snapshot_id).where(
            ReferenceUpdateStatus.branch_id == "manual-branch"
        )),
    )
    rows = [{"material": product.code, "plant": "", "lgort": "1000", "raw": {}} for product in products]
    first = import_sap_matching_rows(
        db, rows=rows, source_version="historical-20261023", source_type="historical_sku_list",
        plant="", lgort="1000", effective_date=None, imported_by="admin", payload_sha256="a" * 64,
    )
    db.commit()
    second = import_sap_matching_rows(
        db, rows=rows, source_version="historical-20261023", source_type="historical_sku_list",
        plant="", lgort="1000", effective_date=None, imported_by="admin", payload_sha256="a" * 64,
    )
    db.commit()
    with pytest.raises(ValueError, match="different content"):
        import_sap_matching_rows(
            db, rows=rows, source_version="historical-20261023", source_type="historical_sku_list",
            plant="", lgort="1000", effective_date=None, imported_by="admin", payload_sha256="b" * 64,
        )
    db.rollback()

    assert first["recordCount"] == 5266
    assert first["resolvedCount"] == 5266
    assert second["idempotent"] is True
    assert db.scalar(select(func.count()).select_from(SapMatchingSourceBatch)) == 1
    assert db.scalar(select(func.count()).select_from(SapMatchingSourceItem)) == 5266
    assert db.get(Product, products[0].id).provisor_goods_id == 88001
    assert db.scalar(select(CompetitorCodeMapping).where(
        CompetitorCodeMapping.our_product_id == products[1].id
    )).source_match_key == "provisor:88002"
    after = (
        db.scalar(select(func.count()).select_from(BranchStock)),
        db.scalar(select(ReferenceUpdateStatus.active_stock_snapshot_id).where(
            ReferenceUpdateStatus.branch_id == "manual-branch"
        )),
    )
    assert after == before


def test_queue_uses_all_allowed_plants_only_warehouse_1000_and_preserves_material_zeros():
    db = _session()
    allowed = Product(code="000000000000000101", name="Allowed", cost=1)
    excluded_plant = Product(code="000000000000000102", name="Plant 1012", cost=1)
    excluded_warehouse = Product(code="000000000000000103", name="Warehouse 4000", cost=1)
    db.add_all([allowed, excluded_plant, excluded_warehouse])
    db.flush()
    for index, sap_plant in enumerate(SAP_PLANTS):
        _active_snapshot(
            db,
            plant=sap_plant.plant,
            branch=f"allowed-{index}",
            products=[allowed],
        )
    _active_snapshot(db, plant="1012", branch="excluded-plant", products=[excluded_plant])
    _active_snapshot(
        db,
        plant="1001",
        branch="excluded-warehouse",
        products=[excluded_warehouse],
        lgort="4000",
    )
    db.commit()

    queue = list_unmatched_sap_products(db)
    assert queue["sourceHealth"]["complete"] is True
    assert queue["sourceHealth"]["availablePlants"] == [row.plant for row in SAP_PLANTS]
    assert queue["pagination"]["total"] == 1
    assert queue["items"][0]["sapSku"] == "000000000000000101"


def test_queue_excludes_carried_forward_stock_not_returned_by_current_getbalance():
    db = _session()
    returned = Product(code="000000000000000111", name="Returned", cost=1)
    carried = Product(code="000000000000000112", name="Carried", cost=1)
    db.add_all([returned, carried])
    db.flush()
    _active_snapshot(
        db,
        plant="1001",
        branch="Алматы",
        products=[returned, carried],
        raw_by_product={
            returned.id: {"aggregation": "sap_material_batches_v1", "sap_rows": [{}]},
            carried.id: {"carry_forward": True, "reason": "not returned"},
        },
    )
    db.commit()

    queue = list_unmatched_sap_products(db)
    assert [row["productId"] for row in queue["items"]] == [returned.id]


def test_queue_reports_missing_identity_without_presenting_it_as_matchable():
    db = _session()
    product = Product(code="000000000000000114", name="Null evidence", cost=1)
    db.add(product)
    db.flush()
    batch = _active_snapshot(db, plant="1001", branch="Алматы", products=[product])
    db.query(SapMatchingSourceItem).filter(SapMatchingSourceItem.batch_id == batch.id).update(
        {SapMatchingSourceItem.product_id: None, SapMatchingSourceItem.identity_status: "missing"}
    )
    db.commit()

    queue = list_unmatched_sap_products(db)
    assert queue["pagination"]["total"] == 0
    assert queue["sourceHealth"]["missingIdentityCount"] == 1


def test_queue_reports_ambiguous_product_identity():
    db = _session()
    product = Product(code="000000000000000113", name="Ambiguous", cost=1)
    db.add(product)
    db.flush()
    batch = _active_snapshot(db, plant="1001", branch="shared-branch", products=[product])
    db.query(SapMatchingSourceItem).filter(SapMatchingSourceItem.batch_id == batch.id).update(
        {SapMatchingSourceItem.product_id: None, SapMatchingSourceItem.identity_status: "ambiguous"}
    )
    db.commit()

    queue = list_unmatched_sap_products(db)
    assert queue["pagination"]["total"] == 0
    assert queue["sourceHealth"]["ambiguousIdentityCount"] == 1


def test_missing_matching_source_cannot_silently_prove_empty_queue():
    db = _session()
    with pytest.raises(SapProvisorQueueUnavailable, match="ещё не импортирован"):
        list_unmatched_sap_products(db)

def test_historical_matching_source_does_not_expire_with_stock_freshness_rules():
    db = _session()
    unmatched = Product(code="000000000000000011", name="Stale unmatched", cost=1)
    db.add(unmatched)
    db.flush()
    _active_snapshot(db, plant="1001", branch="Алматы", products=[unmatched], age_days=10)
    db.commit()

    assert list_unmatched_sap_products(db)["pagination"]["total"] == 1


def test_queue_paginates_full_unmatched_population_with_supported_page_sizes():
    db = _session()
    products = [
        Product(code=str(index).zfill(18), name=f"Товар {index}", cost=1)
        for index in range(1, 28)
    ]
    db.add_all(products)
    db.flush()
    _active_snapshot(db, plant="1001", branch="Алматы", products=products)
    db.commit()

    first = list_unmatched_sap_products(db, page=1, page_size=25)
    second = list_unmatched_sap_products(db, page=2, page_size=25)
    assert first["pagination"] == {"page": 1, "pageSize": 25, "total": 27, "pageCount": 2}
    assert len(first["items"]) == 25
    assert len(second["items"]) == 2
    with pytest.raises(ValueError, match="25, 50 или 100"):
        list_unmatched_sap_products(db, page_size=30)


def test_official_goods_country_is_preserved_and_missing_fields_stay_empty():
    rows = parse_goods_response([
        {
            "id": 501,
            "name": {"name": "Товар"},
            "producer": {"name": "Производитель"},
            "country": {"name": "Казахстан"},
            "dose": {"name": "10 мг"},
        },
        {"id": 502, "name": {"name": "Без страны"}},
    ], limit=10)
    assert rows[0].country == "Казахстан"
    assert rows[1].country == ""
    assert rows[1].producer == ""


def test_admin_confirmation_uses_canonical_mapping_audit_and_removes_product_from_queue():
    db = _session()
    target = Product(code="000000000000000020", name="Цитрамон №10 таб", cost=1)
    remaining = Product(code="000000000000000021", name="Парацетамол №10 таб", cost=1)
    db.add_all([target, remaining])
    db.flush()
    _active_snapshot(db, plant="1001", branch="Алматы", products=[target, remaining])
    db.commit()

    result = confirm_sap_provisor_mapping(db, product_id=target.id, candidate={
        "goodsId": 9001,
        "sourceExternalKey": "9001",
        "sourceMatchKey": "provisor:9001",
        "sourceName": "Цитрамон/таблетки/10",
        "sourceManufacturer": "Производитель",
        "sourceDosageForm": "Таблетки",
    }, username="sap-admin")

    mapping = db.scalar(select(CompetitorCodeMapping).where(CompetitorCodeMapping.source_match_key == "provisor:9001"))
    assert result["goodsId"] == 9001
    assert db.get(Product, target.id).provisor_goods_id == 9001
    assert mapping.our_product_id == target.id
    assert mapping.status == "mapped"
    assert mapping.created_by == "sap-admin"
    assert mapping.approved_at is not None
    queue = list_unmatched_sap_products(db)
    assert [row["productId"] for row in queue["items"]] == [remaining.id]


def test_conflicts_are_rejected_without_overwriting_saved_state():
    db = _session()
    target = Product(code="000000000000000030", name="Target", cost=1)
    owner = Product(code="000000000000000031", name="Owner", cost=1, provisor_goods_id=777)
    db.add_all([target, owner])
    db.flush()
    _active_snapshot(db, plant="1001", branch="Алматы", products=[target, owner])
    db.commit()
    candidate = {
        "goodsId": 777,
        "sourceExternalKey": "777",
        "sourceMatchKey": "provisor:777",
        "sourceName": "Candidate",
    }

    with pytest.raises(ValueError, match="уже принадлежит"):
        confirm_sap_provisor_mapping(db, product_id=target.id, candidate=candidate, username="admin")
    db.rollback()
    assert db.get(Product, target.id).provisor_goods_id is None
    assert db.get(Product, owner.id).provisor_goods_id == 777
    assert db.scalar(select(CompetitorCodeMapping).where(CompetitorCodeMapping.source_match_key == "provisor:777")) is None


def test_approved_canonical_goods_owner_cannot_be_overwritten():
    db = _session()
    target = Product(code="000000000000000032", name="Target", cost=1)
    owner = Product(code="000000000000000033", name="Owner", cost=1)
    db.add_all([target, owner])
    db.flush()
    _active_snapshot(db, plant="1001", branch="Алматы", products=[target, owner])
    db.add(CompetitorCodeMapping(
        platform="provisor",
        source_external_key="778",
        source_match_key="provisor:778",
        our_product_id=owner.id,
        our_sku=owner.code,
        status="mapped",
    ))
    db.commit()

    with pytest.raises(ValueError, match="другим товаром"):
        confirm_sap_provisor_mapping(db, product_id=target.id, candidate={
            "goodsId": 778,
            "sourceExternalKey": "778",
            "sourceMatchKey": "provisor:778",
            "sourceName": "Owned",
        }, username="admin")
    db.rollback()
    assert db.get(Product, target.id).provisor_goods_id is None


def test_concurrent_admin_updates_for_one_goods_id_have_one_winner(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        products = [
            Product(code="000000000000000034", name="First", cost=1),
            Product(code="000000000000000035", name="Second", cost=1),
        ]
        db.add_all(products)
        db.flush()
        product_ids = [row.id for row in products]
        _active_snapshot(db, plant="1001", branch="Алматы", products=products)
        db.commit()

    goods_lock = threading.Lock()

    def transaction_lock(db, _goods_id):
        goods_lock.acquire()
        event.listen(db, "after_commit", lambda _session: goods_lock.release(), once=True)

    monkeypatch.setattr(sap_provisor_matching, "_lock_goods_identity", transaction_lock)
    barrier = threading.Barrier(2)
    successes: list[int] = []
    failures: list[str] = []

    def confirm(product_id: int):
        with sessions() as db:
            barrier.wait(timeout=5)
            try:
                confirm_sap_provisor_mapping(db, product_id=product_id, candidate={
                    "goodsId": 779,
                    "sourceExternalKey": "779",
                    "sourceMatchKey": "provisor:779",
                    "sourceName": "Concurrent",
                }, username="admin")
                successes.append(product_id)
            except ValueError as exc:
                db.rollback()
                failures.append(str(exc))

    threads = [threading.Thread(target=confirm, args=(product_id,)) for product_id in product_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert len(failures) == 1
    with sessions() as db:
        assert db.scalar(select(CompetitorCodeMapping).where(
            CompetitorCodeMapping.source_match_key == "provisor:779"
        )).our_product_id == successes[0]
    engine.dispose()


def test_postgres_advisory_locks_atomicity_and_canonical_integrity(
    postgres_feature_sessions, monkeypatch
):
    sessions = postgres_feature_sessions
    with sessions() as db:
        products = [
            Product(code=str(200 + index).zfill(18), name=f"PG product {index}", cost=1)
            for index in range(6)
        ]
        db.add_all(products)
        db.flush()
        product_ids = [int(row.id) for row in products]
        _active_snapshot(db, plant="1001", branch="pg-branch", products=products)
        db.commit()

    def candidate(goods_id: int) -> dict:
        return {
            "goodsId": goods_id,
            "sourceExternalKey": str(goods_id),
            "sourceMatchKey": f"provisor:{goods_id}",
            "sourceName": f"PG candidate {goods_id}",
        }

    def concurrent_confirm(assignments: list[tuple[int, int]]):
        barrier = threading.Barrier(len(assignments))
        successes: list[tuple[int, int]] = []
        failures: list[Exception] = []

        def run(product_id: int, goods_id: int):
            with sessions() as db:
                barrier.wait(timeout=10)
                try:
                    confirm_sap_provisor_mapping(
                        db,
                        product_id=product_id,
                        candidate=candidate(goods_id),
                        username=f"admin-{product_id}",
                    )
                    successes.append((product_id, goods_id))
                except Exception as exc:
                    db.rollback()
                    failures.append(exc)

        threads = [threading.Thread(target=run, args=assignment) for assignment in assignments]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads)
        return successes, failures

    # Different Goods IDs still serialize on the same product row.
    successes, failures = concurrent_confirm([
        (product_ids[0], 91001),
        (product_ids[0], 91002),
    ])
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)

    # The same Goods ID serializes on the transaction-scoped advisory lock.
    successes_same_goods, failures_same_goods = concurrent_confirm([
        (product_ids[1], 91003),
        (product_ids[2], 91003),
    ])
    assert len(successes_same_goods) == 1
    assert len(failures_same_goods) == 1
    assert isinstance(failures_same_goods[0], ValueError)

    with sessions() as db:
        for product_id, goods_id in [*successes, *successes_same_goods]:
            product = db.get(Product, product_id)
            mapping = db.scalar(select(CompetitorCodeMapping).where(
                CompetitorCodeMapping.source_match_key == f"provisor:{goods_id}"
            ))
            assert int(product.provisor_goods_id) == goods_id
            assert int(mapping.our_product_id) == product_id
            assert mapping.status == "mapped"
            assert mapping.approved_at is not None

    original_apply = sap_provisor_matching.apply_mapping_to_matching_items

    def fail_after_mapping_update(**_kwargs):
        raise RuntimeError("postgres failure before commit")

    monkeypatch.setattr(
        sap_provisor_matching,
        "apply_mapping_to_matching_items",
        fail_after_mapping_update,
    )
    with sessions() as db:
        with pytest.raises(RuntimeError, match="before commit"):
            confirm_sap_provisor_mapping(
                db,
                product_id=product_ids[3],
                candidate=candidate(91004),
                username="failing-admin",
            )
        db.rollback()
    monkeypatch.setattr(
        sap_provisor_matching,
        "apply_mapping_to_matching_items",
        original_apply,
    )
    with sessions() as db:
        assert db.get(Product, product_ids[3]).provisor_goods_id is None
        assert db.scalar(select(CompetitorCodeMapping).where(
            CompetitorCodeMapping.source_match_key == "provisor:91004"
        )) is None


def test_admin_routes_are_server_side_admin_only():
    dependencies = {
        route.path: {dependency.call for dependency in route.dependant.dependencies}
        for route in main.app.routes
        if hasattr(route, "dependant")
    }
    assert require_admin_access in dependencies["/api/admin/sap-provisor/unmatched"]
    assert require_admin_access in dependencies["/api/admin/sap-provisor/products/{product_id}/candidates"]
    assert require_admin_access in dependencies["/api/admin/sap-provisor/search"]
    assert require_admin_access in dependencies["/api/admin/sap-provisor/mappings"]
    assert require_admin_access in dependencies["/api/admin/sap-provisor/source/import"]
    assert require_admin_access in dependencies["/api/admin/sap-provisor/source/refresh"]
    with pytest.raises(HTTPException) as error:
        require_admin_access(AppUser(username="viewer", role="viewer", is_active=True))
    assert error.value.status_code == 403


def test_confirmation_endpoint_revalidates_official_goods_and_does_not_write_on_lookup_failure(monkeypatch):
    db = _session()
    target = Product(code="000000000000000040", name="Target", cost=1)
    db.add(target)
    db.flush()
    _active_snapshot(db, plant="1001", branch="Алматы", products=[target])
    db.commit()

    async def empty(**_kwargs):
        return []

    monkeypatch.setattr(main, "official_external_search", empty)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.post_admin_sap_provisor_mapping(
            payload={"productId": target.id, "goodsId": 123}, db=db, current_user=_admin()
        ))
    assert error.value.status_code == 422
    assert db.get(Product, target.id).provisor_goods_id is None
    assert db.scalar(select(CompetitorCodeMapping)) is None


def test_admin_official_search_has_no_local_fallback_and_returns_russian_error(monkeypatch):
    db = _session()

    async def unavailable(**_kwargs):
        from backend.app.services.provisor_goods import ProvisorGoodsError
        raise ProvisorGoodsError("secret upstream detail")

    monkeypatch.setattr(main, "official_external_search", unavailable)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.get_admin_sap_provisor_search(q="аспирин", limit=10, db=db, current_user=_admin()))
    assert error.value.status_code == 502
    assert error.value.detail == "Поиск в официальном каталоге Provisor недоступен"


def test_confirmation_endpoint_rolls_back_if_mapping_application_fails(monkeypatch):
    db = _session()
    target = Product(code="000000000000000041", name="Target", cost=1)
    db.add(target)
    db.flush()
    _active_snapshot(db, plant="1001", branch="Алматы", products=[target])
    db.commit()

    async def exact(**_kwargs):
        return [{
            "goodsId": 124,
            "sourceExternalKey": "124",
            "sourceMatchKey": "provisor:124",
            "sourceName": "Exact",
        }]

    def fail_after_flush(**_kwargs):
        raise RuntimeError("simulated mapping item failure")

    monkeypatch.setattr(main, "official_external_search", exact)
    monkeypatch.setattr(sap_provisor_matching, "apply_mapping_to_matching_items", fail_after_flush)
    with pytest.raises(RuntimeError, match="simulated"):
        asyncio.run(main.post_admin_sap_provisor_mapping(
            payload={"productId": target.id, "goodsId": 124}, db=db, current_user=_admin()
        ))
    assert db.get(Product, target.id).provisor_goods_id is None
    assert db.scalar(select(CompetitorCodeMapping).where(
        CompetitorCodeMapping.source_match_key == "provisor:124"
    )) is None
