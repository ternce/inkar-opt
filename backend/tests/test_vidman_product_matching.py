from __future__ import annotations

from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.models import Base, Product, ProductExtra, VidmanCanonicalProduct, VidmanProductMatch, VidmanProductReviewQueue
from backend.app.services.vidman_normalization import parse_vidman_product
from backend.app.services.vidman_product_matching import (
    AUTO_MATCHED,
    MANUALLY_APPROVED,
    REVIEW_REQUIRED,
    build_product_indexes,
    decide_match,
    process_vidman_product_matches,
)
from backend.app.services.vidman_review_triage import (
    TIER_A_STRONG,
    TIER_B_GOOD,
    TIER_C_AMBIGUOUS,
    TIER_D_WEAK,
    process_review_triage,
    triage_review_item,
)


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
        canonical_signature=parsed.normalized_signature,
    )
    db.add(row)
    db.flush()
    return row


def _canonical_from_parsed(db, parsed, manufacturer: str = "Maker") -> VidmanCanonicalProduct:
    signature_parts = parsed.normalized_signature.split("|")
    normalized_manufacturer = parse_vidman_product("", manufacturer).normalized_manufacturer
    if normalized_manufacturer and signature_parts and signature_parts[-1] == normalized_manufacturer:
        signature_parts = signature_parts[:-1]
    row = VidmanCanonicalProduct(
        canonical_name=parsed.base_name,
        canonical_manufacturer=normalized_manufacturer,
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
        canonical_signature="|".join(signature_parts),
    )
    db.add(row)
    db.flush()
    return row


def _product(db, code: str, name: str, manufacturer: str = "Maker") -> Product:
    row = Product(code=code, name=name, cost=Decimal("1"))
    db.add(row)
    db.flush()
    db.add(ProductExtra(product_id=row.id, manufacturer=manufacturer))
    db.flush()
    return row


def _decision(db, canonical: VidmanCanonicalProduct):
    return decide_match(canonical, build_product_indexes(db))


def _identity(db, product: Product):
    indexes = build_product_indexes(db)
    return next(item for item in indexes.products if item.product_id == product.id)


def _review_match(db, canonical: VidmanCanonicalProduct, status: str = REVIEW_REQUIRED):
    row = VidmanProductMatch(
        canonical_product_id=canonical.id,
        product_id=None,
        status=status,
        match_type="test_review",
        confidence=Decimal("0"),
    )
    db.add(row)
    db.flush()
    return row


def test_same_name_same_dose_same_pack_matches():
    db = _session()
    canonical = _canonical(db, "L Cet 5mg N30", "Maker")
    product = _product(db, "SKU-1", "L Cet 5mg N30", "Maker")

    decision = _decision(db, canonical)

    assert decision.status == AUTO_MATCHED
    assert decision.product_id == product.id
    assert decision.confidence == Decimal("99")


def test_same_name_different_pack_does_not_auto_match():
    db = _session()
    canonical = _canonical(db, "L Cet 5mg N30", "Maker")
    _product(db, "SKU-1", "L Cet 5mg N100", "Maker")

    decision = _decision(db, canonical)

    assert decision.status != AUTO_MATCHED


def test_same_name_different_dose_does_not_auto_match():
    db = _session()
    canonical = _canonical(db, "L Thyroxine 50mg N30", "Maker")
    _product(db, "SKU-1", "L Thyroxine 100mg N30", "Maker")

    decision = _decision(db, canonical)

    assert decision.status != AUTO_MATCHED


def test_same_name_different_concentration_does_not_auto_match():
    db = _session()
    canonical = _canonical(db, "Nafazolin 0.5% 10ml", "Maker")
    _product(db, "SKU-1", "Nafazolin 1% 10ml", "Maker")

    decision = _decision(db, canonical)

    assert decision.status != AUTO_MATCHED


def test_same_name_different_volume_does_not_auto_match():
    db = _session()
    canonical = _canonical(db, "Sumamed Forte 200mg/5ml 15ml", "Maker")
    _product(db, "SKU-1", "Sumamed Forte 200mg/5ml 30ml", "Maker")

    decision = _decision(db, canonical)

    assert decision.status != AUTO_MATCHED


def test_compatible_manufacturer_formatting_can_match():
    db = _session()
    canonical = _canonical(db, "Drug 10mg N20", "Berlin-Chemie")
    product = _product(db, "SKU-1", "Drug 10mg N20", "Berlin Chemie")

    decision = _decision(db, canonical)

    assert decision.status == AUTO_MATCHED
    assert decision.product_id == product.id


def test_conflicting_manufacturer_does_not_auto_match():
    db = _session()
    canonical = _canonical(db, "Drug 10mg N20", "Maker A")
    _product(db, "SKU-1", "Drug 10mg N20", "Maker B")

    decision = _decision(db, canonical)

    assert decision.status != AUTO_MATCHED


def test_multi_strength_conflict_does_not_auto_match():
    db = _session()
    canonical = _canonical(db, "Edarbi Clo 40mg+12.5mg N28", "Maker")
    _product(db, "SKU-1", "Edarbi Clo 40mg+25mg N28", "Maker")

    decision = _decision(db, canonical)

    assert decision.status != AUTO_MATCHED


def test_missing_manufacturer_can_match_when_identity_is_strong_and_unique():
    db = _session()
    canonical = _canonical(db, "Strong Drug 20mg 10ml N5", "")
    product = _product(db, "SKU-1", "Strong Drug 20mg 10ml N5", "Maker")

    decision = _decision(db, canonical)

    assert decision.status == REVIEW_REQUIRED
    assert decision.product_id != product.id


def test_duplicate_internal_identity_requires_review():
    db = _session()
    canonical = _canonical(db, "Drug 10mg N20", "Maker")
    _product(db, "SKU-1", "Drug 10mg N20", "Maker")
    _product(db, "SKU-2", "Drug 10mg N20", "Maker")

    decision = _decision(db, canonical)

    assert decision.status == REVIEW_REQUIRED
    assert decision.candidate_count == 2


def test_manual_approved_mapping_is_preserved_on_rerun():
    db = _session()
    canonical = _canonical(db, "Drug 10mg N20", "Maker")
    product = _product(db, "SKU-1", "Drug 10mg N20", "Maker")
    manual_product = _product(db, "SKU-MANUAL", "Manual Drug 1mg N1", "Maker")
    db.add(
        VidmanProductMatch(
            canonical_product_id=canonical.id,
            product_id=manual_product.id,
            status=MANUALLY_APPROVED,
            match_type="manual",
            confidence=Decimal("100"),
        )
    )
    db.commit()

    summary = process_vidman_product_matches(db, apply=True, rebuild_auto=True)
    stored = db.get(VidmanProductMatch, 1)

    assert summary.manual_preserved == 1
    assert stored.product_id == manual_product.id
    assert stored.product_id != product.id


def test_case_a_exact_name_vidman_dosage_internal_missing_requires_review():
    db = _session()
    canonical = _canonical_from_parsed(db, parse_vidman_product("Risk Drug 5mg", "Maker"), "Maker")
    _product(db, "SKU-1", "Risk Drug", "Maker")

    decision = _decision(db, canonical)

    assert decision.status == REVIEW_REQUIRED
    assert decision.candidates[0][3]["missing_on_internal"] == ["dosage"]


def test_case_b_exact_name_one_shared_field_pack_missing_requires_review():
    db = _session()
    canonical = _canonical_from_parsed(db, parse_vidman_product("Risk Drug 5mg N30", "Maker"), "Maker")
    _product(db, "SKU-1", "Risk Drug 5mg", "Maker")

    decision = _decision(db, canonical)

    assert decision.status == REVIEW_REQUIRED
    assert "pack_count" in decision.candidates[0][3]["missing_on_internal"]
    assert decision.candidates[0][3]["shared_structural_fields"] == ["dosage"]


def test_case_c_exact_name_same_manufacturer_dosage_pack_auto_allowed():
    db = _session()
    canonical = _canonical_from_parsed(db, parse_vidman_product("Risk Drug 5mg N30", "Maker"), "Maker")
    product = _product(db, "SKU-1", "Risk Drug 5mg N30", "Maker")

    decision = _decision(db, canonical)

    assert decision.status == AUTO_MATCHED
    assert decision.product_id == product.id
    assert decision.confidence == Decimal("97")


def test_case_d_exact_name_same_manufacturer_dosage_pack_form_auto_allowed():
    db = _session()
    canonical = _canonical_from_parsed(db, parse_vidman_product("Risk Drug 5mg N30 табл", "Maker"), "Maker")
    product = _product(db, "SKU-1", "Risk Drug 5mg N30 табл", "Maker")

    decision = _decision(db, canonical)

    assert decision.status == AUTO_MATCHED
    assert decision.product_id == product.id
    assert decision.confidence == Decimal("98")


def test_case_h_variant_token_only_on_one_side_requires_review():
    db = _session()
    parsed = parse_vidman_product("Device Model N2", "Maker")
    canonical = _canonical_from_parsed(db, parsed, "Maker")
    canonical.canonical_signature = f"{parsed.normalized_signature}|variant:0108"
    db.flush()
    _product(db, "SKU-1", "Device Model N2", "Maker")

    decision = _decision(db, canonical)

    assert decision.status == REVIEW_REQUIRED
    assert decision.candidates[0][3]["unshared_variant_identity"] is True


def test_triage_dominant_candidate_without_conflicts_is_tier_a():
    db = _session()
    canonical = _canonical_from_parsed(db, parse_vidman_product("Risk Drug 5mg N30 табл", "Maker"), "Maker")
    top = _product(db, "SKU-1", "Risk Drug 5mg N30 табл", "Maker")
    weak = _product(db, "SKU-2", "Other Product 10mg N10 капс", "Other")

    item = triage_review_item(canonical, [_identity(db, top), _identity(db, weak)])

    assert item.tier == TIER_A_STRONG
    assert item.top_candidate_product_id == top.id
    assert item.score_gap >= 18


def test_triage_close_top_candidates_are_tier_c():
    db = _session()
    canonical = _canonical_from_parsed(db, parse_vidman_product("Risk Drug 5mg N30 табл", "Maker"), "Maker")
    first = _product(db, "SKU-1", "Risk Drug 5mg N30 табл", "Maker")
    second = _product(db, "SKU-2", "Risk Drug 5mg N30 табл Plus", "Maker")

    item = triage_review_item(canonical, [_identity(db, first), _identity(db, second)])

    assert item.tier == TIER_C_AMBIGUOUS


def test_triage_hard_conflict_is_never_tier_a():
    db = _session()
    canonical = _canonical_from_parsed(db, parse_vidman_product("Risk Drug 5mg N30 табл", "Maker"), "Maker")
    conflict = _product(db, "SKU-1", "Risk Drug 10mg N30 табл", "Maker")

    item = triage_review_item(canonical, [_identity(db, conflict)])

    assert item.tier != TIER_A_STRONG
    assert item.hard_conflicts


def test_triage_exact_manufacturer_strong_structure_large_gap_is_a_or_b():
    db = _session()
    canonical = _canonical_from_parsed(db, parse_vidman_product("Risk Drug 5mg N30", "Maker"), "Maker")
    top = _product(db, "SKU-1", "Risk Drug 5mg N30", "Maker")
    weak = _product(db, "SKU-2", "Noise 50ml", "Other")

    item = triage_review_item(canonical, [_identity(db, top), _identity(db, weak)])

    assert item.tier in {TIER_A_STRONG, TIER_B_GOOD}
    assert item.top_candidate_product_id == top.id


def test_triage_missing_manufacturer_strong_structure_not_tier_a():
    db = _session()
    canonical = _canonical_from_parsed(db, parse_vidman_product("Risk Drug 5mg N30 табл", "Maker"), "Maker")
    candidate = _product(db, "SKU-1", "Risk Drug 5mg N30 табл", "")

    item = triage_review_item(canonical, [_identity(db, candidate)])

    assert item.tier != TIER_A_STRONG


def test_triage_duplicate_internal_identity_is_ambiguous():
    db = _session()
    canonical = _canonical(db, "Drug 10mg N20", "Maker")
    first = _product(db, "SKU-1", "Drug 10mg N20", "Maker")
    second = _product(db, "SKU-2", "Drug 10mg N20", "Maker")

    item = triage_review_item(canonical, [_identity(db, first), _identity(db, second)])

    assert item.tier == TIER_C_AMBIGUOUS


def test_review_triage_excludes_manual_approved_and_is_idempotent():
    db = _session()
    review_canonical = _canonical(db, "Review Drug 5mg N30", "Maker")
    manual_canonical = _canonical(db, "Manual Drug 5mg N30", "Maker")
    _product(db, "SKU-1", "Review Drug 5mg N30", "Maker")
    manual_product = _product(db, "SKU-2", "Manual Drug 5mg N30", "Maker")
    _review_match(db, review_canonical, REVIEW_REQUIRED)
    db.add(
        VidmanProductMatch(
            canonical_product_id=manual_canonical.id,
            product_id=manual_product.id,
            status=MANUALLY_APPROVED,
            match_type="manual",
            confidence=Decimal("100"),
        )
    )
    db.commit()

    first = process_review_triage(db, apply_triage=True, rebuild=True)
    second = process_review_triage(db, apply_triage=True, rebuild=True)

    assert first.written == 1
    assert second.written == 1
    assert db.query(VidmanProductReviewQueue).count() == 1
    assert db.query(VidmanProductReviewQueue).one().canonical_product_id == review_canonical.id
    assert db.get(VidmanProductMatch, 2).status == MANUALLY_APPROVED
