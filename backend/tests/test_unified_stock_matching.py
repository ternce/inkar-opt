from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import (
    BranchStock,
    CompetitorCodeMapping,
    PriceFormat,
    Product,
    ReferenceImportJob,
    ReferenceUpdateStatus,
    StockSnapshot,
)
from backend.app.services.competitors.code_mappings import (
    list_product_catalog_code_mappings,
    upsert_code_mapping,
)
from backend.app.services.references.catalog_registration import get_or_create_product
from backend.app.services.references.sap_balance import SapBalanceItem, SapBalancePayload
from backend.app.services.references.stock_snapshots import (
    CanonicalStockRow,
    import_sap_balance_payload,
    preview_sap_catalog_registration,
    publish_stock_snapshot,
)
from backend.app.services.references.types import canonical_branch_id


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _job(db, branch: str) -> ReferenceImportJob:
    row = ReferenceImportJob(data_type="stock", branch_ids_json=json.dumps([branch]), filename="fixture", status="running")
    db.add(row)
    db.flush()
    return row


def _publish(db, branch: str, rows: list[tuple[Product, str]], *, source: str = "manual", carried: int = 0):
    branch = canonical_branch_id(branch)
    snapshot = publish_stock_snapshot(
        db,
        branch_id=branch,
        source_type=source,
        effective_date=date(2026, 10, 21),
        rows=[CanonicalStockRow(product.code, product.code, product.id, Decimal(quantity), {}) for product, quantity in rows],
        import_job=_job(db, branch),
        metadata={"carried_forward_product_count": carried},
    )
    db.commit()
    return snapshot


def _sap_payload(material: str, name: str = "SAP-only product") -> SapBalancePayload:
    return SapBalancePayload(
        "1001",
        date(2026, 10, 21),
        (
            SapBalanceItem(material, "1001", "1000", "batch-a", Decimal("2.25"), {"materialText": name}),
            SapBalanceItem(material, "1001", "1000", "batch-b", Decimal("3.75"), {"materialText": name}),
        ),
        2,
    )


def test_sap_catalog_preview_requires_explicit_approval_and_rolls_back_unapproved_creation():
    db = _session()
    material = "7000001".zfill(18)
    payload = _sap_payload(material)
    preview = preview_sap_catalog_registration(db, payload)
    assert preview.as_dict() | {} == {
        "materialCount": 1,
        "existingProductCount": 0,
        "missingProductCount": 1,
        "missingProducts": [{"material": material, "materialText": "SAP-only product"}],
    }
    assert db.scalar(select(func.count(Product.id))) == 0
    assert db.scalar(select(func.count(BranchStock.id))) == 0
    assert db.scalar(select(func.count(StockSnapshot.id))) == 0
    assert db.scalar(select(func.count(ReferenceImportJob.id))) == 0
    assert db.scalar(select(func.count(ReferenceUpdateStatus.id))) == 0

    rejected = import_sap_balance_payload(
        db,
        branch_id="Алматы",
        payload=payload,
        user_name="operator",
        manual_precedence_hours=0,
        approve_catalog_registration=False,
    )
    assert rejected.status == "error"
    assert db.scalar(select(func.count(Product.id)).where(Product.code == material)) == 0
    assert db.scalar(select(func.count(BranchStock.id)).where(BranchStock.branch_id == "Алматы")) == 0

    accepted = import_sap_balance_payload(
        db,
        branch_id="Алматы",
        payload=payload,
        user_name="operator",
        manual_precedence_hours=0,
        approve_catalog_registration=True,
        payload_sha256="a" * 64,
    )
    assert accepted.status == "success"
    product = db.scalar(select(Product).where(Product.code == material))
    assert product is not None and product.name == "SAP-only product" and Decimal(str(product.cost)) == 0
    stock = db.scalar(select(BranchStock).where(BranchStock.branch_id == "Алматы"))
    assert Decimal(str(stock.stock)) == Decimal("6.0000")
    assert stock.source_type == "sap"
    assert json.loads(accepted.log_json)[0]["catalog_created_product_count"] == 1

    same_product, created = get_or_create_product(db, code=material, name="Excel-visible name")
    assert same_product.id == product.id and created is False
    db.close()


def test_sap_catalog_validation_rejects_noncanonical_and_unstable_material_identity():
    db = _session()
    short = _sap_payload("123")
    try:
        preview_sap_catalog_registration(db, short)
        raise AssertionError("short SAP Material was accepted")
    except ValueError as exc:
        assert "18-digit" in str(exc)

    material = "7000002".zfill(18)
    conflicting = SapBalancePayload(
        "1001",
        date(2026, 10, 21),
        (
            SapBalanceItem(material, "1001", "1000", "a", Decimal("1"), {"materialText": "Name A"}),
            SapBalanceItem(material, "1001", "1000", "b", Decimal("1"), {"materialText": "Name B"}),
        ),
        2,
    )
    try:
        preview_sap_catalog_registration(db, conflicting)
        raise AssertionError("conflicting SAP materialText was accepted")
    except ValueError as exc:
        assert "conflicting materialText" in str(exc)
    db.close()


def test_active_assortment_uses_one_branch_snapshot_and_consistent_coverage_denominator():
    db = _session()
    mapped = Product(code="1".zfill(18), name="Mapped", cost=1, provisor_goods_id=101)
    review = Product(code="2".zfill(18), name="Review", cost=1)
    other_branch = Product(code="3".zfill(18), name="Other branch", cost=1)
    duplicate_identity = Product(code="4".zfill(18), name="Duplicate identity", cost=1, provisor_goods_id=101)
    db.add_all([mapped, review, other_branch, duplicate_identity])
    db.flush()
    upsert_code_mapping(
        db=db,
        platform="vidman",
        product=review,
        source_payload={"source_match_key": "vidman:review", "source_name": "Review candidate"},
        status="review",
    )
    _publish(db, "Алматы", [(mapped, "10"), (review, "2"), (other_branch, "0")], carried=1)
    _publish(db, "Астана", [(other_branch, "7")])

    almaty = list_product_catalog_code_mappings(
        db=db,
        assortment="active",
        branch_id="Алматы",
        platform="all",
        limit=1,
    )
    assert almaty["metrics"][0] | {} == {
        "platform": "all", "total": 2, "mapped": 1, "review": 1, "unmapped": 0,
        "rejected": 0, "noCandidates": 0, "coveragePercent": 50.0, "mappingCoveragePercent": 50.0,
    }
    assert almaty["pagination"] == {"page": 1, "pageSize": 1, "total": 2, "pageCount": 2}
    assert almaty["items"][0]["productId"] == mapped.id
    assert almaty["items"][0]["ambiguousProvisorIdentity"] is True
    assert almaty["ambiguousProvisorProductCount"] == 1
    assert almaty["stockScope"]["carriedForwardProductCount"] == 1
    assert {row["platform"]: row["mapped"] for row in almaty["sourceCoverage"]} == {"provisor": 1, "vidman": 0}

    astana = list_product_catalog_code_mappings(db=db, assortment="active", branch_id="Астана")
    assert astana["metrics"][0]["total"] == 1
    assert astana["items"][0]["productId"] == other_branch.id
    assert all(row["productId"] != mapped.id for row in astana["items"])

    union = list_product_catalog_code_mappings(db=db, assortment="active", all_branches=True)
    assert union["metrics"][0]["total"] == 3
    assert union["pagination"]["total"] == 3
    db.close()


def test_price_format_reference_branch_and_explicit_legacy_stock_scope():
    db = _session()
    active = Product(code="11".zfill(18), name="Active", cost=1)
    legacy = Product(code="12".zfill(18), name="Legacy", cost=1)
    db.add_all([active, legacy, PriceFormat(code="PF-AST", name="PF", branch="Алматы", reference_branch_id="Астана")])
    db.flush()
    _publish(db, "Астана", [(active, "4")])
    db.add(BranchStock(branch_id="Legacy", product_id=legacy.id, sku=legacy.code, stock=9, source_type="excel"))
    db.commit()

    by_format = list_product_catalog_code_mappings(
        db=db, assortment="active", branch_id="Алматы", format_code="PF-AST"
    )
    assert by_format["stockScope"]["branchId"] == canonical_branch_id("Астана")
    assert [row["productId"] for row in by_format["items"]] == [active.id]

    legacy_on = list_product_catalog_code_mappings(db=db, assortment="active", branch_id="Legacy")
    assert legacy_on["metrics"][0]["total"] == 1
    assert legacy_on["stockScope"]["legacyStockIncluded"] is True
    legacy_off = list_product_catalog_code_mappings(
        db=db, assortment="active", branch_id="Legacy", include_legacy_stock=False
    )
    assert legacy_off["metrics"][0]["total"] == 0
    db.close()
