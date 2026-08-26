from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher

from sqlalchemy import delete, func, inspect, select
from sqlalchemy.orm import Session

from backend.app.models import (
    InternalProductNormalized,
    Product,
    VidmanCanonicalProduct,
    VidmanInternalCoverageDecision,
    VidmanInternalCoverageQueue,
    VidmanInternalCoverageRejection,
    VidmanProductMatch,
)
from backend.app.services.vidman_product_matching import (
    AUTO_MATCHED,
    MANUALLY_APPROVED,
    ProductIdentity,
    _decimal_token,
    _evidence,
    _manufacturer_core,
    _manufacturer_compatible,
    _structural_conflicts,
    _tokens,
)
from backend.app.services.vidman_review_triage import score_review_candidate
from backend.app.timezone import now_kz_naive


EXPECTED_UNCOVERED_INTERNAL_PRODUCTS = 2271

COVERAGE_A_STRONG = "COVERAGE_A_STRONG"
COVERAGE_B_GOOD = "COVERAGE_B_GOOD"
COVERAGE_C_AMBIGUOUS = "COVERAGE_C_AMBIGUOUS"
COVERAGE_D_WEAK = "COVERAGE_D_WEAK"
COVERAGE_NO_CANDIDATE = "COVERAGE_NO_CANDIDATE"
MANUAL_NO_VIDMAN_MATCH = "MANUAL_NO_VIDMAN_MATCH"

COVERAGE_TIERS = {
    COVERAGE_A_STRONG,
    COVERAGE_B_GOOD,
    COVERAGE_C_AMBIGUOUS,
    COVERAGE_D_WEAK,
    COVERAGE_NO_CANDIDATE,
}


@dataclass(frozen=True)
class CanonicalIndexes:
    canonicals: list[VidmanCanonicalProduct]
    by_signature: dict[str, list[VidmanCanonicalProduct]]
    by_base_name: dict[str, list[VidmanCanonicalProduct]]
    by_token: dict[str, list[VidmanCanonicalProduct]]
    by_manufacturer_core: dict[str, list[VidmanCanonicalProduct]]
    by_structural_token: dict[str, list[VidmanCanonicalProduct]]


@dataclass(frozen=True)
class RankedCoverageCandidate:
    canonical: VidmanCanonicalProduct
    rank: int
    score: Decimal
    name_score: Decimal
    manufacturer_score: Decimal
    structural_score: Decimal
    variant_score: Decimal
    conflict_penalty: Decimal
    evidence: dict[str, object]
    reason: str

    def as_json(self) -> dict[str, object]:
        return {
            "canonical_product_id": self.canonical.id,
            "canonical_name": self.canonical.canonical_name,
            "canonical_manufacturer": self.canonical.canonical_manufacturer,
            "canonical_signature": self.canonical.canonical_signature,
            "rank": self.rank,
            "score": str(self.score),
            "name_score": str(self.name_score),
            "manufacturer_score": str(self.manufacturer_score),
            "structural_score": str(self.structural_score),
            "variant_score": str(self.variant_score),
            "conflict_penalty": str(self.conflict_penalty),
            "shared_structural_fields": self.evidence.get("shared_structural_fields", []),
            "conflicts": self.evidence.get("hard_conflicts") or self.evidence.get("conflicts") or [],
            "missing_fields": {
                "vidman": self.evidence.get("missing_on_vidman", []),
                "internal": self.evidence.get("missing_on_internal", []),
            },
            "manufacturer_match": self.evidence.get("manufacturer_match", False),
            "manufacturer_compatible": self.evidence.get("manufacturer_compatible", False),
            "exact_name": self.evidence.get("exact_name", False),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CoverageQueueItem:
    product_id: int
    tier: str
    top_candidate_canonical_id: int | None
    top_candidate_score: Decimal | None
    second_candidate_score: Decimal | None
    score_gap: Decimal | None
    candidate_count: int
    shared_structural_fields: int
    hard_conflicts: tuple[str, ...]
    coverage_reason: str
    ranked_candidates: tuple[RankedCoverageCandidate, ...]


@dataclass
class InternalCoverageSummary:
    total_internal_products: int = 0
    covered_internal_products: int = 0
    uncovered_internal_products: int = 0
    processed: int = 0
    written: int = 0
    tier_counts: dict[str, int] = field(default_factory=lambda: {tier: 0 for tier in sorted(COVERAGE_TIERS)})
    candidate_counts: list[int] = field(default_factory=list)
    top1_scores: list[Decimal] = field(default_factory=list)
    score_gaps: list[Decimal] = field(default_factory=list)
    manufacturer_exact_count: int = 0
    zero_hard_conflict_count: int = 0
    elapsed_seconds: float = 0
    rows_per_sec: float = 0
    samples: list[dict[str, object]] = field(default_factory=list)

    def analytics(self) -> dict[str, object]:
        def median(values: list[int | Decimal]) -> float:
            return round(float(statistics.median(values)), 2) if values else 0

        def average(values: list[int | Decimal]) -> float:
            return round(float(sum(values) / len(values)), 2) if values else 0

        def buckets(values: list[Decimal]) -> dict[str, int]:
            result = {"90+": 0, "80-89": 0, "70-79": 0, "60-69": 0, "<60": 0}
            for value in values:
                numeric = float(value)
                if numeric >= 90:
                    result["90+"] += 1
                elif numeric >= 80:
                    result["80-89"] += 1
                elif numeric >= 70:
                    result["70-79"] += 1
                elif numeric >= 60:
                    result["60-69"] += 1
                else:
                    result["<60"] += 1
            return result

        return {
            "TOTAL_INTERNAL_PRODUCTS": self.total_internal_products,
            "COVERED_INTERNAL_PRODUCTS": self.covered_internal_products,
            "UNCOVERED_INTERNAL_PRODUCTS": self.uncovered_internal_products,
            "PROCESSED": self.processed,
            "WRITTEN": self.written,
            "TIER_COUNTS": self.tier_counts,
            "MEDIAN_CANDIDATE_COUNT": median(self.candidate_counts),
            "AVERAGE_CANDIDATE_COUNT": average(self.candidate_counts),
            "TOP1_SCORE_DISTRIBUTION": buckets(self.top1_scores),
            "SCORE_GAP_DISTRIBUTION": buckets(self.score_gaps),
            "MANUFACTURER_EXACT_RATE": round((self.manufacturer_exact_count / self.processed) * 100, 2) if self.processed else 0,
            "ZERO_HARD_CONFLICT_RATE": round((self.zero_hard_conflict_count / self.processed) * 100, 2) if self.processed else 0,
            "ROWS_PER_SEC": round(self.rows_per_sec, 2),
        }


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _covered_product_ids(db: Session) -> set[int]:
    return set(
        db.scalars(
            select(VidmanProductMatch.product_id).where(
                VidmanProductMatch.product_id.is_not(None),
                VidmanProductMatch.status.in_([AUTO_MATCHED, MANUALLY_APPROVED]),
            )
        )
    )


def internal_coverage_counts(db: Session) -> tuple[int, int, int]:
    total = int(db.scalar(select(func.count(Product.id))) or 0)
    covered = len(_covered_product_ids(db))
    return total, covered, total - covered


def assert_expected_uncovered_count(db: Session, *, expected: int = EXPECTED_UNCOVERED_INTERNAL_PRODUCTS) -> None:
    total, covered, uncovered = internal_coverage_counts(db)
    if uncovered != expected:
        raise ValueError(
            f"Expected {expected} uncovered internal products before Stage 3.5, got {uncovered} "
            f"(total={total}, covered={covered})."
        )


def _parsed_from_internal(row: InternalProductNormalized):
    from backend.app.services.vidman_product_matching import _parsed_from_internal as parse_row

    return parse_row(row)


def _identity_for(product: Product, normalized: InternalProductNormalized) -> ProductIdentity:
    return ProductIdentity(
        product_id=product.id,
        code=product.code or "",
        name=product.name or "",
        manufacturer=normalized.raw_manufacturer or "",
        parsed=_parsed_from_internal(normalized),
    )


def _canonical_tokens(canonical: VidmanCanonicalProduct) -> set[str]:
    return _tokens(canonical.base_name or "")


def _canonical_structural_tokens(canonical: VidmanCanonicalProduct) -> set[str]:
    tokens: set[str] = set()
    if canonical.dosage_value is not None:
        tokens.add(f"dosage:{_decimal_token(canonical.dosage_value)}{canonical.dosage_unit or ''}")
    if canonical.concentration_value is not None:
        tokens.add(f"concentration:{_decimal_token(canonical.concentration_value)}{canonical.concentration_unit or ''}")
    if canonical.volume_value is not None:
        tokens.add(f"volume:{_decimal_token(canonical.volume_value)}{canonical.volume_unit or ''}")
    if canonical.weight_value is not None:
        tokens.add(f"weight:{_decimal_token(canonical.weight_value)}{canonical.weight_unit or ''}")
    if canonical.pack_count is not None:
        tokens.add(f"pack:{canonical.pack_count}")
    if canonical.dosage_form:
        tokens.add(f"form:{canonical.dosage_form}")
    return tokens


def _product_structural_tokens(product: ProductIdentity) -> set[str]:
    parsed = product.parsed
    tokens: set[str] = set()
    if parsed.dosage_value is not None:
        tokens.add(f"dosage:{_decimal_token(parsed.dosage_value)}{parsed.dosage_unit or ''}")
    if parsed.concentration_value is not None:
        tokens.add(f"concentration:{_decimal_token(parsed.concentration_value)}{parsed.concentration_unit or ''}")
    if parsed.volume_value is not None:
        tokens.add(f"volume:{_decimal_token(parsed.volume_value)}{parsed.volume_unit or ''}")
    if parsed.weight_value is not None:
        tokens.add(f"weight:{_decimal_token(parsed.weight_value)}{parsed.weight_unit or ''}")
    if parsed.pack_count is not None:
        tokens.add(f"pack:{parsed.pack_count}")
    if parsed.dosage_form:
        tokens.add(f"form:{parsed.dosage_form}")
    return tokens


def build_canonical_indexes(db: Session) -> CanonicalIndexes:
    indexes = CanonicalIndexes(canonicals=[], by_signature={}, by_base_name={}, by_token={}, by_manufacturer_core={}, by_structural_token={})
    occupied = set(
        db.scalars(
            select(VidmanProductMatch.canonical_product_id).where(
                VidmanProductMatch.status.in_([AUTO_MATCHED, MANUALLY_APPROVED])
            )
        )
    )
    for canonical in db.scalars(select(VidmanCanonicalProduct).order_by(VidmanCanonicalProduct.id)):
        if canonical.id in occupied:
            continue
        indexes.canonicals.append(canonical)
        if canonical.canonical_signature:
            indexes.by_signature.setdefault(canonical.canonical_signature, []).append(canonical)
        if canonical.base_name:
            indexes.by_base_name.setdefault(canonical.base_name, []).append(canonical)
        for token in _canonical_tokens(canonical):
            indexes.by_token.setdefault(token, []).append(canonical)
        manufacturer_core = _manufacturer_core(canonical.canonical_manufacturer)
        if manufacturer_core:
            indexes.by_manufacturer_core.setdefault(manufacturer_core, []).append(canonical)
        for structural_token in _canonical_structural_tokens(canonical):
            indexes.by_structural_token.setdefault(structural_token, []).append(canonical)
    return indexes


def _candidate_canonicals(product: ProductIdentity, indexes: CanonicalIndexes, *, limit: int = 12) -> list[VidmanCanonicalProduct]:
    seen: dict[int, VidmanCanonicalProduct] = {}
    if product.parsed.normalized_signature:
        for canonical in indexes.by_signature.get(product.parsed.normalized_signature, []):
            seen[canonical.id] = canonical
    if product.parsed.base_name:
        for canonical in indexes.by_base_name.get(product.parsed.base_name, []):
            seen.setdefault(canonical.id, canonical)
    for token in _tokens(product.parsed.base_name):
        for canonical in indexes.by_token.get(token, [])[:250]:
            seen.setdefault(canonical.id, canonical)
    product_structural = _product_structural_tokens(product)
    product_tokens = _tokens(product.parsed.base_name)
    fallback: dict[int, VidmanCanonicalProduct] = {}
    manufacturer_core = _manufacturer_core(product.parsed.normalized_manufacturer)
    for canonical in indexes.by_manufacturer_core.get(manufacturer_core, [])[:1500] if manufacturer_core else []:
        name_ratio = SequenceMatcher(None, product.parsed.base_name, canonical.base_name or "").ratio()
        if product_structural & _canonical_structural_tokens(canonical) and (product_tokens & _tokens(canonical.base_name or "") or name_ratio >= 0.55):
            fallback.setdefault(canonical.id, canonical)
    for structural_token in product_structural:
        for canonical in indexes.by_structural_token.get(structural_token, [])[:500]:
            if manufacturer_core and _manufacturer_core(canonical.canonical_manufacturer) != manufacturer_core:
                continue
            if product_tokens & _tokens(canonical.base_name or ""):
                fallback.setdefault(canonical.id, canonical)
    for canonical in fallback.values():
        if canonical.id in seen:
            continue
        conflicts = _structural_conflicts(canonical, product)
        evidence = _evidence(canonical, product, conflicts)
        shared_count = len(evidence.get("shared_structural_fields") or [])
        if conflicts or shared_count < 2 or not evidence.get("manufacturer_compatible"):
            continue
        seen[canonical.id] = canonical
    scored = [
        (
            SequenceMatcher(None, product.parsed.base_name, canonical.base_name or "").ratio()
            + (0.1 if _manufacturer_compatible(canonical.canonical_manufacturer, product.parsed.normalized_manufacturer) else 0),
            canonical,
        )
        for canonical in seen.values()
    ]
    scored.sort(key=lambda item: (-item[0], item[1].id))
    return [canonical for _score, canonical in scored[:limit]]


def _rejected_canonical_ids(db: Session, product_id: int) -> set[int]:
    return set(
        db.scalars(
            select(VidmanInternalCoverageRejection.canonical_product_id).where(
                VidmanInternalCoverageRejection.product_id == product_id
            )
        )
    )


def rank_coverage_candidates(
    product: ProductIdentity,
    candidates: list[VidmanCanonicalProduct],
) -> tuple[RankedCoverageCandidate, ...]:
    ranked: list[RankedCoverageCandidate] = []
    for canonical in candidates:
        scored = score_review_candidate(canonical, product)
        conflicts = _structural_conflicts(canonical, product)
        evidence = _evidence(canonical, product, conflicts)
        ranked.append(
            RankedCoverageCandidate(
                canonical=canonical,
                rank=0,
                score=scored.score,
                name_score=scored.name_score,
                manufacturer_score=scored.manufacturer_score,
                structural_score=scored.structural_score,
                variant_score=scored.variant_score,
                conflict_penalty=scored.conflict_penalty,
                evidence=evidence,
                reason=scored.reason,
            )
        )
    ranked.sort(
        key=lambda item: (
            -item.score,
            bool(item.evidence.get("hard_conflicts") or item.evidence.get("conflicts")),
            -len(item.evidence.get("shared_structural_fields") or []),
            item.canonical.id,
        )
    )
    return tuple(
        RankedCoverageCandidate(
            canonical=item.canonical,
            rank=rank,
            score=item.score,
            name_score=item.name_score,
            manufacturer_score=item.manufacturer_score,
            structural_score=item.structural_score,
            variant_score=item.variant_score,
            conflict_penalty=item.conflict_penalty,
            evidence=item.evidence,
            reason=item.reason,
        )
        for rank, item in enumerate(ranked[:5], start=1)
    )


def _tier_for(ranked: tuple[RankedCoverageCandidate, ...]) -> tuple[str, str]:
    if not ranked:
        return COVERAGE_NO_CANDIDATE, "no vidman canonical candidates"
    top = ranked[0]
    second_score = ranked[1].score if len(ranked) > 1 else Decimal("0")
    gap = top.score - second_score
    conflicts = top.evidence.get("hard_conflicts") or top.evidence.get("conflicts") or []
    shared = len(top.evidence.get("shared_structural_fields") or [])
    missing = len(top.evidence.get("missing_on_internal") or []) + len(top.evidence.get("missing_on_vidman") or [])
    strong_name = bool(top.evidence.get("exact_name") or top.evidence.get("comparison_name_exact") or top.name_score >= Decimal("30"))
    if conflicts:
        return COVERAGE_D_WEAK if top.score < 75 or gap < 15 else COVERAGE_C_AMBIGUOUS, "top candidate has hard conflict"
    if top.score >= 75 and gap >= 18 and top.evidence.get("manufacturer_match") and shared >= 2 and strong_name:
        return COVERAGE_A_STRONG, "dominant coverage candidate"
    if top.score >= 72 and gap >= 12 and shared >= 1 and missing <= 3:
        return COVERAGE_B_GOOD, "good coverage candidate with review needed"
    if len(ranked) >= 2 and (gap < 12 or ranked[1].score >= 70):
        return COVERAGE_C_AMBIGUOUS, "multiple plausible vidman canonicals"
    return COVERAGE_D_WEAK, "weak coverage signal"


def coverage_item_for_product(product: ProductIdentity, candidates: list[VidmanCanonicalProduct]) -> CoverageQueueItem:
    ranked = rank_coverage_candidates(product, candidates)
    tier, reason = _tier_for(ranked)
    top = ranked[0] if ranked else None
    second = ranked[1] if len(ranked) > 1 else None
    top_score = top.score if top else None
    second_score = second.score if second else None
    gap = (top_score - second_score) if top_score is not None and second_score is not None else top_score
    conflicts = tuple(top.evidence.get("hard_conflicts") or top.evidence.get("conflicts") or []) if top else ()
    return CoverageQueueItem(
        product_id=product.product_id,
        tier=tier,
        top_candidate_canonical_id=top.canonical.id if top else None,
        top_candidate_score=top_score,
        second_candidate_score=second_score,
        score_gap=gap,
        candidate_count=len(ranked),
        shared_structural_fields=len(top.evidence.get("shared_structural_fields") or []) if top else 0,
        hard_conflicts=conflicts,
        coverage_reason=reason,
        ranked_candidates=ranked,
    )


def _uncovered_products_query(db: Session):
    covered_ids = select(VidmanProductMatch.product_id).where(
        VidmanProductMatch.product_id.is_not(None),
        VidmanProductMatch.status.in_([AUTO_MATCHED, MANUALLY_APPROVED]),
    )
    return (
        select(Product, InternalProductNormalized)
        .join(InternalProductNormalized, InternalProductNormalized.product_id == Product.id)
        .where(Product.id.not_in(covered_ids))
        .order_by(Product.id)
    )


def _apply_item(db: Session, item: CoverageQueueItem) -> None:
    row = db.scalar(select(VidmanInternalCoverageQueue).where(VidmanInternalCoverageQueue.product_id == item.product_id))
    if row is None:
        row = VidmanInternalCoverageQueue(product_id=item.product_id)
        db.add(row)
    row.tier = item.tier
    row.top_candidate_canonical_id = item.top_candidate_canonical_id
    row.top_candidate_score = item.top_candidate_score
    row.second_candidate_score = item.second_candidate_score
    row.score_gap = item.score_gap
    row.candidate_count = item.candidate_count
    row.shared_structural_fields = item.shared_structural_fields
    row.hard_conflicts_json = _json(list(item.hard_conflicts))
    row.coverage_reason = item.coverage_reason
    row.candidate_rankings_json = _json([candidate.as_json() for candidate in item.ranked_candidates])
    row.updated_at = now_kz_naive()


def process_internal_coverage(
    db: Session,
    *,
    limit: int | None = None,
    product_id: int | None = None,
    tier: str | None = None,
    apply: bool = False,
    rebuild: bool = False,
    verify_expected_count: bool = True,
    verbose: bool = False,
) -> InternalCoverageSummary:
    if db.bind is None or (db.bind.dialect.name != "sqlite" and not inspect(db.bind).has_table("vidman_product_matches")):
        raise ValueError("vidman_product_matches table is required")
    if verify_expected_count:
        assert_expected_uncovered_count(db)

    started = time.monotonic()
    summary = InternalCoverageSummary()
    summary.total_internal_products, summary.covered_internal_products, summary.uncovered_internal_products = internal_coverage_counts(db)
    indexes = build_canonical_indexes(db)
    query = _uncovered_products_query(db)
    if product_id is not None:
        query = query.where(Product.id == product_id)
    rows = list(db.execute(query.limit(limit) if limit is not None else query))

    if apply and rebuild:
        delete_query = delete(VidmanInternalCoverageQueue)
        if product_id is not None:
            delete_query = delete_query.where(VidmanInternalCoverageQueue.product_id == product_id)
        db.execute(delete_query)
        db.flush()

    for product, normalized in rows:
        identity = _identity_for(product, normalized)
        rejected = _rejected_canonical_ids(db, product.id)
        candidates = [canonical for canonical in _candidate_canonicals(identity, indexes) if canonical.id not in rejected]
        item = coverage_item_for_product(identity, candidates)
        if tier and item.tier != tier:
            continue
        summary.processed += 1
        summary.tier_counts[item.tier] = summary.tier_counts.get(item.tier, 0) + 1
        summary.candidate_counts.append(item.candidate_count)
        if item.top_candidate_score is not None:
            summary.top1_scores.append(item.top_candidate_score)
        if item.score_gap is not None:
            summary.score_gaps.append(item.score_gap)
        top = item.ranked_candidates[0] if item.ranked_candidates else None
        if top and top.evidence.get("manufacturer_match"):
            summary.manufacturer_exact_count += 1
        if top and not (top.evidence.get("hard_conflicts") or top.evidence.get("conflicts")):
            summary.zero_hard_conflict_count += 1
        if len(summary.samples) < 20:
            summary.samples.append(
                {
                    "product_id": item.product_id,
                    "tier": item.tier,
                    "top_candidate_canonical_id": item.top_candidate_canonical_id,
                    "top_candidate_score": str(item.top_candidate_score),
                    "score_gap": str(item.score_gap),
                    "coverage_reason": item.coverage_reason,
                    "candidates": [candidate.as_json() for candidate in item.ranked_candidates[:3]],
                }
            )
        if apply:
            _apply_item(db, item)
            summary.written += 1
        if verbose:
            print(
                f"product_id={item.product_id} tier={item.tier} "
                f"top={item.top_candidate_canonical_id} score={item.top_candidate_score}"
            )

    if apply:
        db.commit()
    else:
        db.rollback()
    summary.elapsed_seconds = time.monotonic() - started
    summary.rows_per_sec = summary.processed / summary.elapsed_seconds if summary.elapsed_seconds else 0
    return summary
