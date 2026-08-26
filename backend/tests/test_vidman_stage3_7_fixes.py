from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.models import (
    Base,
    InternalProductNormalized,
    Product,
    ProductExtra,
    VidmanCanonicalProduct,
    VidmanInternalCoverageQueue,
    VidmanInternalCoverageRejection,
    VidmanProductMatch,
)
from backend.app.services.vidman_internal_coverage import COVERAGE_A_STRONG, _candidate_canonicals, build_canonical_indexes, process_internal_coverage
from backend.app.services.vidman_normalization import parse_vidman_product
from backend.app.services.vidman_product_matching import AUTO_MATCHED, MANUALLY_APPROVED, ProductIdentity, _evidence, _structural_conflicts
from backend.app.services.vidman_review_triage import score_review_candidate


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
        canonical_signature=f"{parsed.normalized_signature}|{len(db.identity_map)}",
    )
    db.add(row)
    db.flush()
    return row


def _identity(db, product: Product) -> ProductIdentity:
    normalized = db.scalar(select(InternalProductNormalized).where(InternalProductNormalized.product_id == product.id))
    assert normalized is not None
    parsed = parse_vidman_product(normalized.raw_name, normalized.raw_manufacturer)
    return ProductIdentity(product_id=product.id, code=product.code, name=product.name, manufacturer=normalized.raw_manufacturer, parsed=parsed)


def test_harmless_route_form_suffix_scores_as_same_name_when_structure_matches():
    db = _session()
    canonical = _canonical(db, "Монкаста жев 5mg N28 табл", "KRKA d.d. Novo mesto")
    product = _product(db, "SKU-1", "Монкаста 5mg N28 табл", "KRKA d.d.")
    identity = _identity(db, product)

    scored = score_review_candidate(canonical, identity)

    assert scored.evidence["comparison_name_exact"] is True
    assert scored.name_score == Decimal("32")
    assert scored.evidence["shared_structural_fields"] == ["dosage", "dosage_form", "pack_count"]


def test_meaningful_variant_remains_distinct():
    db = _session()
    canonical = _canonical(db, "Device Model 0108-01 N1", "Maker")
    product = _product(db, "SKU-1", "Device Model 0108 N1", "Maker")
    identity = _identity(db, product)

    evidence = _evidence(canonical, identity, _structural_conflicts(canonical, identity))

    assert evidence["comparison_name_exact"] is False
    assert evidence["unshared_variant_identity"] is True


def test_krka_legal_location_suffix_is_manufacturer_alias_exact():
    db = _session()
    canonical = _canonical(db, "Drug 5mg N28 табл", "KRKA d.d. Novo mesto")
    product = _product(db, "SKU-1", "Drug 5mg N28 табл", "KRKA")
    identity = _identity(db, product)

    scored = score_review_candidate(canonical, identity)

    assert scored.evidence["manufacturer_exact"] is False
    assert scored.evidence["manufacturer_alias_exact"] is True
    assert scored.manufacturer_score == Decimal("16")


def test_unrelated_manufacturers_remain_conflict():
    db = _session()
    canonical = _canonical(db, "Drug 5mg N28 табл", "Maker A")
    product = _product(db, "SKU-1", "Drug 5mg N28 табл", "Maker B")
    identity = _identity(db, product)

    conflicts = _structural_conflicts(canonical, identity)

    assert "manufacturer" in conflicts


def test_dosage_as_weight_representation_is_structural_match_for_solid_forms():
    db = _session()
    canonical = _canonical(db, "Natalcid 0.25g N10 супп", "Maker")
    product = _product(db, "SKU-1", "Natalcid 0.25 N10 супп", "Maker")
    identity = _identity(db, product)

    evidence = _evidence(canonical, identity, _structural_conflicts(canonical, identity))

    assert "dosage" in evidence["shared_structural_fields"]
    assert "dosage" not in evidence["missing_on_vidman"]
    assert "weight" not in evidence["missing_on_internal"]


def test_missing_field_remains_missing_not_match():
    db = _session()
    canonical = _canonical(db, "Risk Drug 5mg N30 табл", "Maker")
    product = _product(db, "SKU-1", "Risk Drug N30 табл", "Maker")
    identity = _identity(db, product)

    evidence = _evidence(canonical, identity, _structural_conflicts(canonical, identity))

    assert "dosage" in evidence["missing_on_internal"]
    assert "dosage" not in evidence["shared_structural_fields"]


def test_hard_pack_conflict_never_strong():
    db = _session()
    product = _product(db, "SKU-1", "Risk Drug 5mg N100 табл", "Maker")
    _canonical(db, "Risk Drug 5mg N30 табл", "Maker")

    process_internal_coverage(db, apply=True, rebuild=True, verify_expected_count=False)
    row = db.scalar(select(VidmanInternalCoverageQueue).where(VidmanInternalCoverageQueue.product_id == product.id))

    assert row.tier != COVERAGE_A_STRONG
    assert "pack_count" in row.hard_conflicts_json


def test_hard_dosage_conflict_never_strong():
    db = _session()
    product = _product(db, "SKU-1", "Risk Drug 10mg N30 табл", "Maker")
    _canonical(db, "Risk Drug 5mg N30 табл", "Maker")

    process_internal_coverage(db, apply=True, rebuild=True, verify_expected_count=False)
    row = db.scalar(select(VidmanInternalCoverageQueue).where(VidmanInternalCoverageQueue.product_id == product.id))

    assert row.tier != COVERAGE_A_STRONG
    assert "dosage" in row.hard_conflicts_json


def test_valid_candidate_outside_old_token_bucket_is_recovered_by_fallback():
    db = _session()
    product = _product(db, "SKU-1", "Target Product 5mg N30 табл", "KRKA")
    for index in range(270):
        _canonical(db, f"Target Noise {index} 5mg N30 табл", "Other Maker")
    target = _canonical(db, "Target Product chew 5mg N30 табл", "KRKA d.d. Novo mesto")

    candidates = _candidate_canonicals(_identity(db, product), build_canonical_indexes(db))

    assert target.id in {candidate.id for candidate in candidates}


def test_covered_product_excluded_from_reverse_queue():
    db = _session()
    covered = _product(db, "SKU-1", "Covered Drug 5mg N30 табл", "Maker")
    canonical = _canonical(db, "Covered Drug 5mg N30 табл", "Maker")
    db.add(VidmanProductMatch(canonical_product_id=canonical.id, product_id=covered.id, status=AUTO_MATCHED))
    db.commit()

    process_internal_coverage(db, apply=True, rebuild=True, verify_expected_count=False)

    assert db.scalar(select(VidmanInternalCoverageQueue).where(VidmanInternalCoverageQueue.product_id == covered.id)) is None


def test_manual_approval_preserved_by_reverse_rebuild():
    db = _session()
    product = _product(db, "SKU-1", "Manual Drug 5mg N30 табл", "Maker")
    canonical = _canonical(db, "Manual Drug 5mg N30 табл", "Maker")
    db.add(VidmanProductMatch(canonical_product_id=canonical.id, product_id=product.id, status=MANUALLY_APPROVED))
    db.commit()

    process_internal_coverage(db, apply=True, rebuild=True, verify_expected_count=False)

    stored = db.scalar(select(VidmanProductMatch).where(VidmanProductMatch.canonical_product_id == canonical.id))
    assert stored.status == MANUALLY_APPROVED
    assert stored.product_id == product.id


def test_manual_rejection_preserved_by_reverse_rebuild():
    db = _session()
    product = _product(db, "SKU-1", "Rejectable Drug 5mg N30 табл", "Maker")
    canonical = _canonical(db, "Rejectable Drug 5mg N30 табл", "Maker")
    db.add(VidmanInternalCoverageRejection(product_id=product.id, canonical_product_id=canonical.id, reason="operator rejected"))
    db.commit()

    process_internal_coverage(db, apply=True, rebuild=True, verify_expected_count=False)
    row = db.scalar(select(VidmanInternalCoverageQueue).where(VidmanInternalCoverageQueue.product_id == product.id))

    assert canonical.id not in [item["canonical_product_id"] for item in json.loads(row.candidate_rankings_json)]
    assert db.scalar(select(VidmanInternalCoverageRejection).where(VidmanInternalCoverageRejection.product_id == product.id)) is not None
