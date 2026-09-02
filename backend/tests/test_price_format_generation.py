from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import main
from backend.app.db import Base
from backend.app.deps import ROLE_ADMIN
from backend.app.models import (
    BranchSapMapping,
    CalculatedPrice,
    PriceFormat,
    PriceFormatBranchCounter,
    PriceList,
    Product,
)
from backend.app.services.price_formats import (
    allocate_price_format_code,
    seed_default_sap_branch_mappings,
)
from backend.app.services.sap_export import build_sap_rows


IPL = "\u0418\u041f\u041b"
GPL = "\u0413\u041f\u041b"
ALMATY = "\u0410\u043b\u043c\u0430\u0442\u044b"
ESIK = "\u0415\u0441\u0438\u043a"


def _session_factory(engine=None):
    engine = engine or create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
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


def _generated_format(
    code: str,
    *,
    name: str | None = None,
    branch: str = ALMATY,
    price_list_type: str | None = None,
    sap_branch_code: str | None = None,
    sequence_number: int | None = None,
) -> PriceFormat:
    return PriceFormat(
        code=code,
        name=name or code,
        branch=branch,
        price_list_type=price_list_type,
        sap_branch_code=sap_branch_code,
        sequence_number=sequence_number,
    )


def test_create_price_format_generates_branch_scoped_shared_sequence(monkeypatch):
    Session = _session_factory()
    client = _client(Session, monkeypatch)
    try:
        first = client.post("/api/price-formats", json={"name": "IPL test", "branch": "Алматы", "priceListType": "ИПЛ"})
        second = client.post("/api/price-formats", json={"name": "GPL test", "branch": "Алматы", "priceListType": "ГПЛ"})
        third = client.post("/api/price-formats", json={"name": "Esik test", "branch": "Есик", "priceListType": "ИПЛ"})
    finally:
        main.app.dependency_overrides.clear()

    assert first.status_code == 200, first.text
    assert first.json()["code"] == "ИПЛ_1001_001"
    assert first.json()["priceListType"] == "ИПЛ"
    assert first.json()["sapBranchCode"] == "1001"
    assert first.json()["sequenceNumber"] == 1
    assert second.status_code == 200, second.text
    assert second.json()["code"] == "ГПЛ_1001_002"
    assert third.status_code == 200, third.text
    assert third.json()["code"] == "ИПЛ_1004_001"


def test_allocator_bootstraps_from_existing_generated_codes_when_counter_absent():
    Session = _session_factory()
    db = Session()
    db.add_all(
        [
            _generated_format(f"{IPL}_1001_001"),
            _generated_format(f"{GPL}_1001_002"),
            _generated_format(f"{IPL}_1001_005"),
        ]
    )
    db.commit()

    generated = allocate_price_format_code(db, branch=ALMATY, price_list_type=GPL)

    assert generated.code == f"{GPL}_1001_006"
    assert generated.sequence_number == 6
    assert db.get(PriceFormatBranchCounter, "1001").last_sequence == 6


def test_allocator_uses_shared_branch_sequence_across_price_list_types_when_counter_absent():
    Session = _session_factory()
    db = Session()
    db.add(_generated_format(f"{IPL}_1001_001"))
    db.commit()

    generated = allocate_price_format_code(db, branch=ALMATY, price_list_type=GPL)

    assert generated.code == f"{GPL}_1001_002"
    assert generated.sequence_number == 2


def test_allocator_starts_empty_branch_at_first_sequence():
    Session = _session_factory()
    db = Session()

    generated = allocate_price_format_code(db, branch=ALMATY, price_list_type=IPL)

    assert generated.code == f"{IPL}_1001_001"
    assert generated.sequence_number == 1


def test_allocator_ignores_legacy_codes_without_sap_branch_segment():
    Session = _session_factory()
    db = Session()
    db.add(_generated_format(f"{GPL}_008"))
    db.commit()

    generated = allocate_price_format_code(db, branch=ALMATY, price_list_type=IPL)

    assert generated.code == f"{IPL}_1001_001"
    assert generated.sequence_number == 1


def test_allocator_reconciles_metadata_parsed_codes_and_stale_counter():
    Session = _session_factory()
    db = Session()
    db.add_all(
        [
            PriceFormatBranchCounter(sap_branch_code="1001", last_sequence=3),
            _generated_format(f"{IPL}_1001_005"),
            _generated_format(
                "metadata-1001-007",
                price_list_type=GPL,
                sap_branch_code="1001",
                sequence_number=7,
            ),
        ]
    )
    db.commit()

    generated = allocate_price_format_code(db, branch=ALMATY, price_list_type=IPL)

    assert generated.code == f"{IPL}_1001_008"
    assert generated.sequence_number == 8
    assert db.get(PriceFormatBranchCounter, "1001").last_sequence == 8


def test_allocator_preserves_counter_when_it_is_ahead_of_existing_codes():
    Session = _session_factory()
    db = Session()
    db.add_all(
        [
            PriceFormatBranchCounter(sap_branch_code="1001", last_sequence=10),
            _generated_format(f"{IPL}_1001_005"),
        ]
    )
    db.commit()

    generated = allocate_price_format_code(db, branch=ALMATY, price_list_type=GPL)

    assert generated.code == f"{GPL}_1001_011"
    assert generated.sequence_number == 11


def test_generated_sequence_is_unique_per_sap_branch_even_across_types():
    Session = _session_factory()
    db = Session()
    db.add(
        _generated_format(
            f"{IPL}_1001_001",
            price_list_type=IPL,
            sap_branch_code="1001",
            sequence_number=1,
        )
    )
    db.commit()

    db.add(
        _generated_format(
            f"{GPL}_1001_001",
            price_list_type=GPL,
            sap_branch_code="1001",
            sequence_number=1,
        )
    )
    try:
        db.commit()
        assert False, "duplicate generated sequence should fail"
    except IntegrityError:
        db.rollback()


def test_same_sequence_is_allowed_for_different_sap_branches():
    Session = _session_factory()
    db = Session()
    db.add_all(
        [
            _generated_format(
                f"{IPL}_1001_001",
                price_list_type=IPL,
                sap_branch_code="1001",
                sequence_number=1,
            ),
            _generated_format(
                f"{IPL}_1004_001",
                branch=ESIK,
                price_list_type=IPL,
                sap_branch_code="1004",
                sequence_number=1,
            ),
        ]
    )

    db.commit()

    assert db.scalar(select(PriceFormat.id).where(PriceFormat.code == f"{IPL}_1001_001")) is not None
    assert db.scalar(select(PriceFormat.id).where(PriceFormat.code == f"{IPL}_1004_001")) is not None


def test_create_price_format_rejects_invalid_type_and_missing_mapping(monkeypatch):
    Session = _session_factory()
    client = _client(Session, monkeypatch)
    try:
        invalid = client.post("/api/price-formats", json={"name": "Bad type", "branch": "Алматы", "priceListType": "VIP"})
    finally:
        main.app.dependency_overrides.clear()

    assert invalid.status_code == 400
    assert "priceListType" in invalid.json()["detail"]

    db = Session()
    db.query(BranchSapMapping).delete()
    db.commit()
    try:
        allocate_price_format_code(db, branch="Unknown branch", price_list_type="ИПЛ")
        assert False, "missing mapping should fail"
    except ValueError as exc:
        assert "SAP branch mapping" in str(exc)


def test_settings_edit_persists_name_and_keeps_identity_immutable(monkeypatch):
    Session = _session_factory()
    db = Session()
    pf = PriceFormat(
        code="ИПЛ_1001_001",
        name="Old name",
        branch="Алматы",
        price_list_type="ИПЛ",
        sap_branch_code="1001",
        sequence_number=1,
    )
    db.add(pf)
    db.commit()
    client = _client(Session, monkeypatch)
    try:
        saved = client.put("/api/price-formats/ИПЛ_1001_001/settings", json={"name": "New name", "branch": "Алматы"})
        renamed_branch = client.put("/api/price-formats/ИПЛ_1001_001/settings", json={"name": "New name", "branch": "Есик"})
        renamed_type = client.put("/api/price-formats/ИПЛ_1001_001/settings", json={"name": "New name", "priceListType": "ГПЛ"})
        missing = client.put("/api/price-formats/NOPE/settings", json={"name": "Nope"})
    finally:
        main.app.dependency_overrides.clear()

    assert saved.status_code == 200, saved.text
    assert saved.json()["name"] == "New name"
    assert saved.json()["code"] == "ИПЛ_1001_001"
    assert renamed_branch.status_code == 400
    assert renamed_type.status_code == 400
    assert missing.status_code == 404
    assert Session().scalar(select(PriceFormat.name).where(PriceFormat.code == "ИПЛ_1001_001")) == "New name"


def test_delete_unused_format_and_reject_used_format(monkeypatch):
    Session = _session_factory()
    db = Session()
    unused = PriceFormat(code="UNUSED", name="Unused", branch="Алматы")
    used = PriceFormat(code="USED", name="Used", branch="Алматы")
    db.add_all([unused, used])
    db.flush()
    db.add(PriceList(number="PL-1", price_format_id=used.id))
    db.commit()

    client = _client(Session, monkeypatch)
    try:
        deleted = client.delete("/api/price-formats/UNUSED")
        blocked = client.delete("/api/price-formats/USED")
    finally:
        main.app.dependency_overrides.clear()

    assert deleted.status_code == 200, deleted.text
    assert Session().scalar(select(PriceFormat.id).where(PriceFormat.code == "UNUSED")) is None
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["dependencies"] == [{"table": "price_lists", "count": 1}]


def test_legacy_price_format_keeps_null_metadata(monkeypatch):
    Session = _session_factory()
    db = Session()
    db.add(PriceFormat(code="LEGACY", name="Legacy", branch="Алматы"))
    db.commit()
    client = _client(Session, monkeypatch)
    try:
        response = client.get("/api/price-formats")
    finally:
        main.app.dependency_overrides.clear()

    legacy = next(row for row in response.json() if row["code"] == "LEGACY")
    assert legacy["priceListType"] is None
    assert legacy["sapBranchCode"] is None
    assert legacy["sequenceNumber"] is None


def test_sap_rows_use_price_format_code():
    Session = _session_factory()
    session = Session()
    pf = PriceFormat(code="ИПЛ_1001_001", name="Display name", branch="Алматы")
    session.add(pf)
    session.flush()
    resolved = type("Resolved", (), {"price_format": pf, "price_list": type("PriceListRef", (), {"id": 1, "number": "PL-1"})()})()
    product = Product(code="000000000000001234", name="Product", cost=100)
    session.add(product)
    session.flush()
    session.add(CalculatedPrice(price_list_id=1, product_id=product.id, cost=100, base_price=100, final_price=123, applied_reason="", zone=""))
    session.commit()

    rows = build_sap_rows(session, [resolved])

    assert rows[0]["category"] == "ИПЛ_1001_001"


def test_allocator_is_unique_under_parallel_local_sessions(tmp_path):
    db_path = tmp_path / "price_formats.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Session = _session_factory(engine)
    with Session() as db:
        seed_default_sap_branch_mappings(db)
        db.commit()

    def create_one(index: int) -> str:
        with Session() as db:
            generated = allocate_price_format_code(db, branch="Алматы", price_list_type="ИПЛ" if index % 2 else "ГПЛ")
            db.add(
                PriceFormat(
                    code=generated.code,
                    name=f"Format {index}",
                    branch="Алматы",
                    price_list_type=generated.price_list_type,
                    sap_branch_code=generated.sap_branch_code,
                    sequence_number=generated.sequence_number,
                )
            )
            db.commit()
            return generated.code

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(create_one, range(1, 9)))

    assert len(codes) == len(set(codes))
    assert sorted(code.rsplit("_", 1)[1] for code in codes) == [f"{idx:03d}" for idx in range(1, 9)]
