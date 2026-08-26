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
    VidmanProductMatch,
    VidmanProductMatchAudit,
    VidmanProductReviewQueue,
    VidmanRejectedCandidate,
)
from backend.app.services.vidman_normalization import parse_vidman_product
from backend.app.services.vidman_product_matching import (
    MANUALLY_APPROVED,
    MANUAL_UNMATCHED,
    REVIEW_REQUIRED,
    process_vidman_product_matches,
)
from backend.app.services.vidman_review_api import (
    approve_review_match,
    list_review_queue,
    mark_review_unmatched,
    reject_review_candidate,
    review_detail,
    search_internal_products,
)
from backend.app.services.vidman_review_triage import TIER_A_STRONG, TIER_B_GOOD, process_review_triage


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


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
        canonical_signature=f"{parsed.normalized_signature}|{len(db.identity_map)}",
    )
    db.add(row)
    db.flush()
    return row


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
            normalized_signature=parsed.normalized_signature,
        )
    )
    db.flush()
    return row


def _review_row(db, canonical: VidmanCanonicalProduct, product: Product, tier: str = TIER_A_STRONG) -> VidmanProductReviewQueue:
    db.add(
        VidmanProductMatch(
            canonical_product_id=canonical.id,
            status=REVIEW_REQUIRED,
            match_type="candidate_generation",
            confidence=Decimal("82"),
            candidate_count=1,
        )
    )
    row = VidmanProductReviewQueue(
        canonical_product_id=canonical.id,
        tier=tier,
        top_candidate_product_id=product.id,
        top_candidate_score=Decimal("88"),
        score_gap=Decimal("28"),
        candidate_count=1,
        shared_structural_fields=3,
        review_reason="test candidate",
        candidate_rankings_json=json.dumps(
            [
                {
                    "product_id": product.id,
                    "rank": 1,
                    "score": "88",
                    "name_score": "32",
                    "manufacturer_score": "20",
                    "structural_score": "27",
                    "variant_score": "5",
                    "shared_structural_fields": ["dosage", "pack_count", "form"],
                    "missing_fields": {"vidman": [], "internal": []},
                    "conflicts": [],
                    "reason": "test",
                }
            ]
        ),
    )
    db.add(row)
    db.commit()
    return row


def test_review_list_tier_filter_and_pagination():
    db = _session()
    p1 = _product(db, "SKU-1", "Drug One 5mg N30")
    p2 = _product(db, "SKU-2", "Drug Two 5mg N30")
    c1 = _canonical(db, "Drug One 5mg N30")
    c2 = _canonical(db, "Drug Two 5mg N30")
    _review_row(db, c1, p1, TIER_A_STRONG)
    _review_row(db, c2, p2, TIER_B_GOOD)

    first_page = list_review_queue(db, tier=TIER_A_STRONG, page=1, limit=1)

    assert first_page["total"] == 1
    assert first_page["items"][0]["canonicalProductId"] == c1.id
    assert first_page["items"][0]["candidateCount"] == 1


def test_review_detail_returns_candidate_evidence():
    db = _session()
    product = _product(db, "SKU-1", "Detail Drug 5mg N30")
    canonical = _canonical(db, "Detail Drug 5mg N30")
    _review_row(db, canonical, product)

    detail = review_detail(db, canonical_product_id=canonical.id)

    assert detail["vidman"]["canonicalName"] == canonical.canonical_name
    assert detail["candidates"][0]["productId"] == product.id
    assert detail["candidates"][0]["sharedFields"] == ["dosage", "pack_count", "form"]


def test_approve_valid_candidate_and_preserve_on_rerun():
    db = _session()
    product = _product(db, "SKU-1", "Approve Drug 5mg N30")
    canonical = _canonical(db, "Approve Drug 5mg N30")
    _review_row(db, canonical, product)

    match = approve_review_match(db, canonical_product_id=canonical.id, product_id=product.id, actor="tester")
    summary = process_vidman_product_matches(db, apply=True, rebuild_auto=True)
    stored = db.scalar(select(VidmanProductMatch).where(VidmanProductMatch.canonical_product_id == canonical.id))

    assert match.status == MANUALLY_APPROVED
    assert summary.manual_preserved == 1
    assert stored.product_id == product.id
    assert db.scalar(select(VidmanProductMatchAudit).where(VidmanProductMatchAudit.action == "approve")) is not None


def test_duplicate_approval_is_idempotent():
    db = _session()
    product = _product(db, "SKU-1", "Stable Drug 5mg N30")
    other = _product(db, "SKU-2", "Other Drug 5mg N30")
    canonical = _canonical(db, "Stable Drug 5mg N30")
    _review_row(db, canonical, product)

    approve_review_match(db, canonical_product_id=canonical.id, product_id=product.id)
    second = approve_review_match(db, canonical_product_id=canonical.id, product_id=other.id)

    assert second.status == MANUALLY_APPROVED
    assert second.product_id == product.id


def test_reject_candidate_persists_and_is_excluded_from_detail_and_triage_rerun():
    db = _session()
    product = _product(db, "SKU-1", "Reject Drug 5mg")
    canonical = _canonical(db, "Reject Drug 5mg N30")
    _review_row(db, canonical, product)

    reject_review_candidate(db, canonical_product_id=canonical.id, product_id=product.id, reason="wrong pack")
    detail = review_detail(db, canonical_product_id=canonical.id)
    process_review_triage(db, canonical_id=canonical.id, apply_triage=True, rebuild=True)
    queue = db.scalar(select(VidmanProductReviewQueue).where(VidmanProductReviewQueue.canonical_product_id == canonical.id))

    assert detail["candidates"] == []
    assert db.scalar(select(VidmanRejectedCandidate).where(VidmanRejectedCandidate.product_id == product.id)) is not None
    assert product.id not in [item.get("product_id") for item in json.loads(queue.candidate_rankings_json)]


def test_mark_unmatched_uses_manual_status():
    db = _session()
    product = _product(db, "SKU-1", "No Match Drug 5mg N30")
    canonical = _canonical(db, "No Match Drug 5mg N30")
    _review_row(db, canonical, product)

    match = mark_review_unmatched(db, canonical_product_id=canonical.id, reason="no internal product")

    assert match.status == MANUAL_UNMATCHED
    assert match.product_id is None


def test_nonexistent_product_rejected():
    db = _session()
    canonical = _canonical(db, "Missing Drug 5mg N30")

    with pytest.raises(ValueError):
        approve_review_match(db, canonical_product_id=canonical.id, product_id=999)


def test_internal_product_search_returns_normalized_identity():
    db = _session()
    product = _product(db, "SKU-LOOK", "Lookup Drug 10mg N20", "Lookup Maker")

    results = search_internal_products(db, q="LOOK", limit=5)

    assert results[0]["productId"] == product.id
    assert results[0]["normalized"]["pack"] == 20
