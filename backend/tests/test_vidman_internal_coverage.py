from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.models import (
    Base,
    InternalProductNormalized,
    Product,
    ProductExtra,
    VidmanCanonicalProduct,
    VidmanInternalCoverageAudit,
    VidmanInternalCoverageDecision,
    VidmanInternalCoverageQueue,
    VidmanInternalCoverageRejection,
    VidmanProductMatch,
)
from backend.app.services.vidman_internal_coverage import (
    COVERAGE_A_STRONG,
    MANUAL_NO_VIDMAN_MATCH,
    assert_expected_uncovered_count,
    internal_coverage_counts,
    process_internal_coverage,
)
from backend.app.services.vidman_internal_coverage_api import (
    approve_internal_coverage,
    internal_coverage_counters,
    internal_coverage_detail,
    list_internal_coverage,
    mark_internal_coverage_no_match,
    reject_internal_coverage_candidate,
)
from backend.app.services.vidman_normalization import parse_vidman_product
from backend.app.services.vidman_product_matching import AUTO_MATCHED, MANUALLY_APPROVED, REVIEW_REQUIRED


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _product(db, code: str, name: str, manufacturer: str = "Maker") -> Product:
    row = Product(code=code, name=name, cost=Decimal("1"))
    db.add(row)
    db.flush()
    parsed = parse_vidman_product(name, manufacturer)
    db.add(ProductExtra(product_id=row.id, manufacturer=manufacturer))
    db.add(
        InternalProductNormalized(
            product_id=row.id,
            raw_name=name,
            raw_manufacturer=manufacturer,
            normalized_name=parsed.normalized_name,
            base_name=parsed.base_name,
            normalized_manufacturer=parsed.normalized_manufacturer,
            dosage_value=parsed.dosage_value,
            dosage_unit=parsed.dosage_unit,
            concentration_value=parsed.concentration_value,
            concentration_unit=parsed.concentration_unit,
            volume_value=parsed.volume_value,
            volume_unit=parsed.volume_unit,
            weight_value=parsed.weight_value,
            weight_unit=parsed.weight_unit,
            pack_count=parsed.pack_count,
            dosage_form=parsed.dosage_form,
            variant_text=parsed.variant_text,
            identity_tokens_json=json.dumps(parsed.identity_tokens),
            normalized_signature=parsed.normalized_signature,
        )
    )
    db.flush()
    return row


def _canonical(db, name: str, manufacturer: str = "Maker") -> VidmanCanonicalProduct:
    parsed = parse_vidman_product(name, manufacturer)
    row = VidmanCanonicalProduct(
        canonical_name=parsed.base_name,
        canonical_manufacturer=parsed.normalized_manufacturer,
        base_name=parsed.base_name,
        dosage_value=parsed.dosage_value,
        dosage_unit=parsed.dosage_unit,
        concentration_value=parsed.concentration_value,
        concentration_unit=parsed.concentration_unit,
        volume_value=parsed.volume_value,
        volume_unit=parsed.volume_unit,
        weight_value=parsed.weight_value,
        weight_unit=parsed.weight_unit,
        pack_count=parsed.pack_count,
        dosage_form=parsed.dosage_form,
        canonical_signature=parsed.normalized_signature,
    )
    db.add(row)
    db.flush()
    return row


def test_counts_use_only_auto_and_manual_approved_coverage():
    db = _session()
    p1 = _product(db, "SKU-1", "Covered Drug 5mg N30")
    p2 = _product(db, "SKU-2", "Manual Drug 5mg N30")
    _product(db, "SKU-3", "Open Drug 5mg N30")
    c1 = _canonical(db, "Covered Drug 5mg N30")
    c2 = _canonical(db, "Manual Drug 5mg N30")
    db.add(VidmanProductMatch(canonical_product_id=c1.id, product_id=p1.id, status=AUTO_MATCHED))
    db.add(VidmanProductMatch(canonical_product_id=c2.id, product_id=p2.id, status=MANUALLY_APPROVED))
    db.commit()

    assert internal_coverage_counts(db) == (3, 2, 1)


def test_expected_count_guard_rejects_drift():
    db = _session()
    _product(db, "SKU-1", "Open Drug 5mg N30")

    with pytest.raises(ValueError):
        assert_expected_uncovered_count(db, expected=2315)


def test_processes_only_uncovered_products_and_writes_queue_only():
    db = _session()
    covered = _product(db, "SKU-1", "Covered Drug 5mg N30")
    uncovered = _product(db, "SKU-2", "Open Drug 5mg N30")
    c1 = _canonical(db, "Covered Drug 5mg N30")
    _canonical(db, "Open Drug 5mg N30")
    db.add(VidmanProductMatch(canonical_product_id=c1.id, product_id=covered.id, status=AUTO_MATCHED))
    db.commit()

    summary = process_internal_coverage(db, apply=True, rebuild=True, verify_expected_count=False)
    queue_rows = list(db.scalars(select(VidmanInternalCoverageQueue)))

    assert summary.processed == 1
    assert summary.written == 1
    assert queue_rows[0].product_id == uncovered.id
    assert db.scalar(select(VidmanProductMatch).where(VidmanProductMatch.product_id == covered.id)).status == AUTO_MATCHED


def test_strong_reverse_candidate_is_ranked_without_auto_approval():
    db = _session()
    product = _product(db, "SKU-1", "Reverse Drug 5mg N30")
    canonical = _canonical(db, "Reverse Drug 5mg N30")

    summary = process_internal_coverage(db, apply=True, rebuild=True, verify_expected_count=False)
    row = db.scalar(select(VidmanInternalCoverageQueue).where(VidmanInternalCoverageQueue.product_id == product.id))
    candidates = json.loads(row.candidate_rankings_json)

    assert summary.tier_counts[COVERAGE_A_STRONG] == 1
    assert row.top_candidate_canonical_id == canonical.id
    assert candidates[0]["canonical_product_id"] == canonical.id
    assert db.scalar(select(VidmanProductMatch).where(VidmanProductMatch.product_id == product.id)) is None


def test_occupied_canonical_is_not_recommended_to_uncovered_product():
    db = _session()
    covered = _product(db, "SKU-1", "Shared Drug 5mg N30")
    open_product = _product(db, "SKU-2", "Shared Drug 5mg N30")
    occupied = _canonical(db, "Shared Drug 5mg N30")
    alternate = _canonical(db, "Shared Drug 5mg N30 10ml")
    db.add(VidmanProductMatch(canonical_product_id=occupied.id, product_id=covered.id, status=MANUALLY_APPROVED))
    db.commit()

    process_internal_coverage(db, product_id=open_product.id, apply=True, rebuild=True, verify_expected_count=False)
    row = db.scalar(select(VidmanInternalCoverageQueue).where(VidmanInternalCoverageQueue.product_id == open_product.id))

    assert row.top_candidate_canonical_id == alternate.id


def test_internal_coverage_list_filters_uncovered_and_tier():
    db = _session()
    covered = _product(db, "SKU-1", "Covered List Drug 5mg N30")
    uncovered = _product(db, "SKU-2", "Open List Drug 5mg N30")
    c1 = _canonical(db, "Covered List Drug 5mg N30")
    _canonical(db, "Open List Drug 5mg N30")
    db.add(VidmanProductMatch(canonical_product_id=c1.id, product_id=covered.id, status=AUTO_MATCHED))
    db.commit()
    process_internal_coverage(db, apply=True, rebuild=True, verify_expected_count=False)

    result = list_internal_coverage(db, tier=COVERAGE_A_STRONG, page=1, limit=10)

    assert result["total"] == 1
    assert result["items"][0]["productId"] == uncovered.id
    assert result["items"][0]["alreadyCovered"] is False


def test_internal_coverage_detail_includes_mapping_context():
    db = _session()
    product = _product(db, "SKU-1", "Detail Reverse Drug 5mg N30")
    canonical = _canonical(db, "Detail Reverse Drug 5mg N30")
    db.add(VidmanProductMatch(canonical_product_id=canonical.id, status=REVIEW_REQUIRED))
    db.commit()
    process_internal_coverage(db, apply=True, rebuild=True, verify_expected_count=False)

    detail = internal_coverage_detail(db, product_id=product.id)

    assert detail["productId"] == product.id
    assert detail["candidates"][0]["canonicalProductId"] == canonical.id
    assert detail["candidates"][0]["mappingContext"]["matchStatus"] == REVIEW_REQUIRED


def test_reverse_approve_review_required_canonical():
    db = _session()
    product = _product(db, "SKU-1", "Approve Reverse Drug 5mg N30")
    canonical = _canonical(db, "Approve Reverse Drug 5mg N30")
    db.add(VidmanProductMatch(canonical_product_id=canonical.id, status=REVIEW_REQUIRED))
    db.commit()

    match = approve_internal_coverage(db, product_id=product.id, canonical_product_id=canonical.id, actor="tester")

    assert match.status == MANUALLY_APPROVED
    assert match.product_id == product.id
    assert match.matched_by == "manual_reverse"
    assert db.scalar(select(VidmanInternalCoverageAudit).where(VidmanInternalCoverageAudit.action == "approve")) is not None


def test_reverse_approve_is_idempotent_for_same_manual_mapping():
    db = _session()
    product = _product(db, "SKU-1", "Idempotent Reverse Drug 5mg N30")
    canonical = _canonical(db, "Idempotent Reverse Drug 5mg N30")
    db.add(VidmanProductMatch(canonical_product_id=canonical.id, product_id=product.id, status=MANUALLY_APPROVED))
    db.commit()

    match = approve_internal_coverage(db, product_id=product.id, canonical_product_id=canonical.id)

    assert match.status == MANUALLY_APPROVED
    assert match.product_id == product.id


def test_reverse_approve_blocks_auto_match_to_different_product():
    db = _session()
    product = _product(db, "SKU-1", "Open Reverse Drug 5mg N30")
    other = _product(db, "SKU-2", "Other Reverse Drug 5mg N30")
    canonical = _canonical(db, "Open Reverse Drug 5mg N30")
    db.add(VidmanProductMatch(canonical_product_id=canonical.id, product_id=other.id, status=AUTO_MATCHED))
    db.commit()

    with pytest.raises(ValueError):
        approve_internal_coverage(db, product_id=product.id, canonical_product_id=canonical.id)


def test_reverse_approve_blocks_manual_match_to_different_product():
    db = _session()
    product = _product(db, "SKU-1", "Open Manual Reverse Drug 5mg N30")
    other = _product(db, "SKU-2", "Other Manual Reverse Drug 5mg N30")
    canonical = _canonical(db, "Open Manual Reverse Drug 5mg N30")
    db.add(VidmanProductMatch(canonical_product_id=canonical.id, product_id=other.id, status=MANUALLY_APPROVED))
    db.commit()

    with pytest.raises(ValueError):
        approve_internal_coverage(db, product_id=product.id, canonical_product_id=canonical.id)


def test_reverse_reject_persists_and_excludes_future_rerun():
    db = _session()
    product = _product(db, "SKU-1", "Reject Reverse Drug 5mg N30")
    canonical = _canonical(db, "Reject Reverse Drug 5mg N30")
    process_internal_coverage(db, apply=True, rebuild=True, verify_expected_count=False)

    reject_internal_coverage_candidate(db, product_id=product.id, canonical_product_id=canonical.id, reason="wrong item")
    process_internal_coverage(db, product_id=product.id, apply=True, rebuild=True, verify_expected_count=False)
    row = db.scalar(select(VidmanInternalCoverageQueue).where(VidmanInternalCoverageQueue.product_id == product.id))

    assert db.scalar(select(VidmanInternalCoverageRejection).where(VidmanInternalCoverageRejection.product_id == product.id)) is not None
    assert canonical.id not in [item["canonical_product_id"] for item in json.loads(row.candidate_rankings_json)]


def test_mark_no_vidman_match_persists_decision_and_counters():
    db = _session()
    product = _product(db, "SKU-1", "No Vidman Match Drug 5mg N30")
    _canonical(db, "No Vidman Match Drug 5mg N30")
    process_internal_coverage(db, apply=True, rebuild=True, verify_expected_count=False)

    decision = mark_internal_coverage_no_match(db, product_id=product.id, reason="not sold by Vidman")
    counters = internal_coverage_counters(db)
    reviewed = list_internal_coverage(db, review_status="reviewed")

    assert decision.status == MANUAL_NO_VIDMAN_MATCH
    assert counters["manualNoVidmanMatch"] == 1
    assert reviewed["items"][0]["coverageStatus"] == MANUAL_NO_VIDMAN_MATCH


def test_reverse_approve_blocked_after_manual_no_match():
    db = _session()
    product = _product(db, "SKU-1", "Blocked No Match Drug 5mg N30")
    canonical = _canonical(db, "Blocked No Match Drug 5mg N30")
    db.add(VidmanInternalCoverageDecision(product_id=product.id, status=MANUAL_NO_VIDMAN_MATCH))
    db.commit()

    with pytest.raises(ValueError):
        approve_internal_coverage(db, product_id=product.id, canonical_product_id=canonical.id)
