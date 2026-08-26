from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher

from sqlalchemy import delete, func, inspect, select
from sqlalchemy.orm import Session

from ..models import (
    Product,
    VidmanCanonicalProduct,
    VidmanProductMatch,
    VidmanRejectedCandidate,
    VidmanProductReviewQueue,
)
from .vidman_product_matching import (
    AUTO_MATCHED,
    MANUAL_UNMATCHED,
    MANUALLY_APPROVED,
    REJECTED,
    REVIEW_REQUIRED,
    UNMATCHED,
    ProductIdentity,
    _candidate_products,
    _evidence,
    _manufacturer_compatible,
    _manufacturer_exact,
    _structural_conflicts,
    _tokens,
    build_product_indexes,
    decide_match,
)
from ..timezone import now_kz_naive


TIER_A_STRONG = "TIER_A_STRONG"
TIER_B_GOOD = "TIER_B_GOOD"
TIER_C_AMBIGUOUS = "TIER_C_AMBIGUOUS"
TIER_D_WEAK = "TIER_D_WEAK"

REVIEW_TIERS = {TIER_A_STRONG, TIER_B_GOOD, TIER_C_AMBIGUOUS, TIER_D_WEAK}


@dataclass(frozen=True)
class RankedReviewCandidate:
    product: ProductIdentity
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
            "product_id": self.product.product_id,
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
class ReviewQueueItem:
    canonical_product_id: int
    tier: str
    top_candidate_product_id: int | None
    top_candidate_score: Decimal | None
    second_candidate_score: Decimal | None
    score_gap: Decimal | None
    candidate_count: int
    shared_structural_fields: int
    hard_conflicts: tuple[str, ...]
    review_reason: str
    ranked_candidates: tuple[RankedReviewCandidate, ...]


@dataclass
class ReviewTriageSummary:
    total_review_required: int = 0
    processed: int = 0
    written: int = 0
    tier_counts: dict[str, int] = field(default_factory=lambda: {tier: 0 for tier in sorted(REVIEW_TIERS)})
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

        def bucket_scores(values: list[Decimal]) -> dict[str, int]:
            buckets = {"90+": 0, "80-89": 0, "70-79": 0, "60-69": 0, "<60": 0}
            for value in values:
                numeric = float(value)
                if numeric >= 90:
                    buckets["90+"] += 1
                elif numeric >= 80:
                    buckets["80-89"] += 1
                elif numeric >= 70:
                    buckets["70-79"] += 1
                elif numeric >= 60:
                    buckets["60-69"] += 1
                else:
                    buckets["<60"] += 1
            return buckets

        return {
            "TOTAL_REVIEW_REQUIRED": self.total_review_required,
            "PROCESSED": self.processed,
            "WRITTEN": self.written,
            "TIER_COUNTS": self.tier_counts,
            "MEDIAN_CANDIDATE_COUNT": median(self.candidate_counts),
            "AVERAGE_CANDIDATE_COUNT": average(self.candidate_counts),
            "TOP1_SCORE_DISTRIBUTION": bucket_scores(self.top1_scores),
            "SCORE_GAP_DISTRIBUTION": bucket_scores(self.score_gaps),
            "MANUFACTURER_EXACT_RATE": round((self.manufacturer_exact_count / self.processed) * 100, 2) if self.processed else 0,
            "ZERO_HARD_CONFLICT_RATE": round((self.zero_hard_conflict_count / self.processed) * 100, 2) if self.processed else 0,
            "ROWS_PER_SEC": round(self.rows_per_sec, 2),
        }


def _decimal(value: float | int | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _token_overlap(left: str, right: str) -> Decimal:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return Decimal("0")
    return _decimal((len(left_tokens & right_tokens) / len(left_tokens | right_tokens)) * 10)


def _name_score(canonical: VidmanCanonicalProduct, product: ProductIdentity, evidence: dict[str, object]) -> Decimal:
    if evidence.get("exact_signature"):
        return Decimal("35")
    if evidence.get("exact_name"):
        return Decimal("32")
    if evidence.get("comparison_name_exact"):
        return Decimal("32")
    left = str(evidence.get("comparison_canonical_name") or canonical.base_name or "")
    right = str(evidence.get("comparison_internal_name") or product.parsed.base_name or "")
    ratio = SequenceMatcher(None, left, right).ratio()
    return min(Decimal("30"), _decimal(ratio * 25) + _token_overlap(left, right))


def _manufacturer_score(canonical: VidmanCanonicalProduct, product: ProductIdentity, evidence: dict[str, object]) -> Decimal:
    if evidence.get("manufacturer_match"):
        return Decimal("20")
    if evidence.get("manufacturer_alias_exact"):
        return Decimal("16")
    if evidence.get("manufacturer_compatible") and (canonical.canonical_manufacturer or product.parsed.normalized_manufacturer):
        return Decimal("12")
    if _manufacturer_compatible(canonical.canonical_manufacturer, product.parsed.normalized_manufacturer):
        return Decimal("4")
    return Decimal("-20")


def _structural_score(evidence: dict[str, object]) -> Decimal:
    shared = len(evidence.get("shared_structural_fields") or [])
    missing = len(evidence.get("missing_on_internal") or []) + len(evidence.get("missing_on_vidman") or [])
    return max(Decimal("0"), Decimal(shared * 9) - Decimal(missing * 2))


def _variant_score(evidence: dict[str, object]) -> Decimal:
    return Decimal("-15") if evidence.get("unshared_variant_identity") else Decimal("5")


def _conflict_penalty(evidence: dict[str, object]) -> Decimal:
    conflicts = set(evidence.get("hard_conflicts") or evidence.get("conflicts") or [])
    if not conflicts:
        return Decimal("0")
    structural_hard = {"dosage", "pack_count", "volume", "concentration", "strengths", "ratio"}
    penalty = Decimal("0")
    if "manufacturer" in conflicts:
        penalty -= Decimal("20")
    penalty -= Decimal(35 * len(conflicts & structural_hard))
    penalty -= Decimal(15 * len(conflicts - structural_hard - {"manufacturer"}))
    return penalty


def score_review_candidate(canonical: VidmanCanonicalProduct, product: ProductIdentity) -> RankedReviewCandidate:
    conflicts = _structural_conflicts(canonical, product)
    evidence = _evidence(canonical, product, conflicts)
    name_score = _name_score(canonical, product, evidence)
    manufacturer_score = _manufacturer_score(canonical, product, evidence)
    structural_score = _structural_score(evidence)
    variant_score = _variant_score(evidence)
    conflict_penalty = _conflict_penalty(evidence)
    raw_score = name_score + manufacturer_score + structural_score + variant_score + conflict_penalty
    score = max(Decimal("0"), min(Decimal("100"), raw_score))
    reason_parts = []
    if evidence.get("exact_name"):
        reason_parts.append("exact_name")
    if evidence.get("manufacturer_match"):
        reason_parts.append("manufacturer_exact")
    if evidence.get("hard_conflicts"):
        reason_parts.append("hard_conflict")
    if evidence.get("missing_on_internal") or evidence.get("missing_on_vidman"):
        reason_parts.append("missing_structural_evidence")
    if not reason_parts:
        reason_parts.append("ranked_by_similarity")
    return RankedReviewCandidate(
        product=product,
        rank=0,
        score=score,
        name_score=name_score,
        manufacturer_score=manufacturer_score,
        structural_score=structural_score,
        variant_score=variant_score,
        conflict_penalty=conflict_penalty,
        evidence=evidence,
        reason="+".join(reason_parts),
    )


def _rank_candidates(canonical: VidmanCanonicalProduct, candidates: list[ProductIdentity]) -> tuple[RankedReviewCandidate, ...]:
    scored = [score_review_candidate(canonical, product) for product in candidates]
    scored.sort(
        key=lambda item: (
            -item.score,
            bool(item.evidence.get("hard_conflicts") or item.evidence.get("conflicts")),
            -len(item.evidence.get("shared_structural_fields") or []),
            item.product.product_id,
        )
    )
    return tuple(
        RankedReviewCandidate(
            product=item.product,
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
        for rank, item in enumerate(scored[:5], start=1)
    )


def _tier_for(ranked: tuple[RankedReviewCandidate, ...]) -> tuple[str, str]:
    if not ranked:
        return TIER_D_WEAK, "no review candidates"
    top = ranked[0]
    second_score = ranked[1].score if len(ranked) > 1 else Decimal("0")
    gap = top.score - second_score
    conflicts = top.evidence.get("hard_conflicts") or top.evidence.get("conflicts") or []
    shared_count = len(top.evidence.get("shared_structural_fields") or [])
    missing_count = len(top.evidence.get("missing_on_internal") or []) + len(top.evidence.get("missing_on_vidman") or [])
    duplicate_exact_identity = len(ranked) > 1 and top.evidence.get("exact_signature") and ranked[1].evidence.get("exact_signature")
    strong_name = bool(top.evidence.get("exact_name") or top.evidence.get("comparison_name_exact") or top.name_score >= Decimal("30"))

    if duplicate_exact_identity:
        return TIER_C_AMBIGUOUS, "duplicate internal normalized identity"
    if conflicts:
        return (TIER_C_AMBIGUOUS if top.score >= 70 and gap < 12 else TIER_D_WEAK), "top candidate has hard conflict"
    if not top.evidence.get("manufacturer_match"):
        if top.score >= 82 and gap >= 18 and shared_count >= 3:
            return TIER_B_GOOD, "strong structure with missing/non-exact manufacturer"
        return TIER_C_AMBIGUOUS if gap < 15 and len(ranked) >= 2 else TIER_D_WEAK, "manufacturer not exact"
    if top.score >= 75 and gap >= 18 and shared_count >= 2 and missing_count <= 2 and strong_name:
        return TIER_A_STRONG, "dominant no-conflict candidate"
    if top.score >= 65 and gap >= 10 and shared_count >= 1:
        return TIER_B_GOOD, "good dominant candidate with missing evidence"
    if len(ranked) >= 2 and (gap < 10 or ranked[1].score >= 70):
        return TIER_C_AMBIGUOUS, "multiple plausible candidates"
    return TIER_D_WEAK, "weak or noisy candidates"


def triage_review_item(canonical: VidmanCanonicalProduct, candidates: list[ProductIdentity]) -> ReviewQueueItem:
    ranked = _rank_candidates(canonical, candidates)
    tier, reason = _tier_for(ranked)
    top = ranked[0] if ranked else None
    second = ranked[1] if len(ranked) > 1 else None
    top_score = top.score if top else None
    second_score = second.score if second else None
    gap = (top_score - second_score) if top_score is not None and second_score is not None else top_score
    conflicts = tuple(top.evidence.get("hard_conflicts") or top.evidence.get("conflicts") or []) if top else ()
    return ReviewQueueItem(
        canonical_product_id=canonical.id,
        tier=tier,
        top_candidate_product_id=top.product.product_id if top else None,
        top_candidate_score=top_score,
        second_candidate_score=second_score,
        score_gap=gap,
        candidate_count=len(ranked),
        shared_structural_fields=len(top.evidence.get("shared_structural_fields") or []) if top else 0,
        hard_conflicts=conflicts,
        review_reason=reason,
        ranked_candidates=ranked,
    )


def _review_canonicals_query(db: Session):
    has_match_table = db.bind is not None and (db.bind.dialect.name == "sqlite" or inspect(db.bind).has_table("vidman_product_matches"))
    query = select(VidmanCanonicalProduct).order_by(VidmanCanonicalProduct.id)
    if has_match_table:
        query = query.join(VidmanProductMatch, VidmanProductMatch.canonical_product_id == VidmanCanonicalProduct.id).where(
            VidmanProductMatch.status == REVIEW_REQUIRED
        )
    return query


def _candidate_identities_for_review(db: Session, canonical: VidmanCanonicalProduct, decision, indexes) -> list[ProductIdentity]:
    if decision.candidates:
        candidates = [product for product, _score, _match_type, _evidence in decision.candidates]
    else:
        candidates = _candidate_products(canonical, indexes, limit=12)
    rejected_ids = set(
        db.scalars(
            select(VidmanRejectedCandidate.product_id).where(
                VidmanRejectedCandidate.canonical_product_id == canonical.id
            )
        )
    )
    return [product for product in candidates if product.product_id not in rejected_ids]


def _apply_queue_item(db: Session, item: ReviewQueueItem) -> None:
    row = db.scalar(
        select(VidmanProductReviewQueue).where(VidmanProductReviewQueue.canonical_product_id == item.canonical_product_id)
    )
    if row is None:
        row = VidmanProductReviewQueue(canonical_product_id=item.canonical_product_id)
        db.add(row)
    row.tier = item.tier
    row.top_candidate_product_id = item.top_candidate_product_id
    row.top_candidate_score = item.top_candidate_score
    row.second_candidate_score = item.second_candidate_score
    row.score_gap = item.score_gap
    row.candidate_count = item.candidate_count
    row.shared_structural_fields = item.shared_structural_fields
    row.hard_conflicts_json = json.dumps(list(item.hard_conflicts), ensure_ascii=False)
    row.review_reason = item.review_reason
    row.candidate_rankings_json = json.dumps([candidate.as_json() for candidate in item.ranked_candidates], ensure_ascii=False, sort_keys=True)
    row.updated_at = now_kz_naive()


def process_review_triage(
    db: Session,
    *,
    limit: int | None = None,
    canonical_id: int | None = None,
    tier: str | None = None,
    apply_triage: bool = False,
    rebuild: bool = False,
    verbose: bool = False,
) -> ReviewTriageSummary:
    started = time.monotonic()
    summary = ReviewTriageSummary()
    indexes = build_product_indexes(db)
    query = _review_canonicals_query(db)
    if canonical_id is not None:
        query = query.where(VidmanCanonicalProduct.id == canonical_id)
    summary.total_review_required = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    canonicals = list(db.scalars(query.limit(limit) if limit is not None else query))

    if apply_triage and rebuild:
        delete_query = delete(VidmanProductReviewQueue)
        if canonical_id is not None:
            delete_query = delete_query.where(VidmanProductReviewQueue.canonical_product_id == canonical_id)
        db.execute(delete_query)
        db.flush()

    for canonical in canonicals:
        decision = decide_match(canonical, indexes)
        candidates = _candidate_identities_for_review(db, canonical, decision, indexes)
        item = triage_review_item(canonical, candidates)
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
                    "canonical_product_id": item.canonical_product_id,
                    "tier": item.tier,
                    "top_candidate_product_id": item.top_candidate_product_id,
                    "top_candidate_score": str(item.top_candidate_score),
                    "score_gap": str(item.score_gap),
                    "review_reason": item.review_reason,
                    "candidates": [candidate.as_json() for candidate in item.ranked_candidates[:3]],
                }
            )
        if apply_triage:
            _apply_queue_item(db, item)
            summary.written += 1
        if verbose:
            print(f"canonical_id={canonical.id} tier={item.tier} top={item.top_candidate_product_id} score={item.top_candidate_score}")

    if apply_triage:
        db.commit()
    else:
        db.rollback()
    summary.elapsed_seconds = time.monotonic() - started
    summary.rows_per_sec = summary.processed / summary.elapsed_seconds if summary.elapsed_seconds else 0
    return summary


def approve_match(db: Session, *, canonical_product_id: int, product_id: int) -> VidmanProductMatch:
    product = db.get(Product, product_id)
    if product is None:
        raise ValueError(f"Unknown product_id={product_id}")
    match = db.scalar(select(VidmanProductMatch).where(VidmanProductMatch.canonical_product_id == canonical_product_id))
    if match is None:
        match = VidmanProductMatch(canonical_product_id=canonical_product_id)
        db.add(match)
    match.product_id = product_id
    match.status = MANUALLY_APPROVED
    match.match_type = "manual_review"
    match.confidence = Decimal("100")
    match.matched_by = "manual"
    match.approved_at = now_kz_naive()
    match.updated_at = now_kz_naive()
    db.commit()
    return match


def reject_candidate(db: Session, *, canonical_product_id: int, product_id: int, reason: str = "") -> VidmanProductMatch:
    product = db.get(Product, product_id)
    if product is None:
        raise ValueError(f"Unknown product_id={product_id}")
    rejected = db.scalar(
        select(VidmanRejectedCandidate).where(
            VidmanRejectedCandidate.canonical_product_id == canonical_product_id,
            VidmanRejectedCandidate.product_id == product_id,
        )
    )
    if rejected is None:
        rejected = VidmanRejectedCandidate(canonical_product_id=canonical_product_id, product_id=product_id)
        db.add(rejected)
    rejected.reason = reason
    match = db.scalar(select(VidmanProductMatch).where(VidmanProductMatch.canonical_product_id == canonical_product_id))
    if match is None:
        match = VidmanProductMatch(canonical_product_id=canonical_product_id)
        db.add(match)
    match.updated_at = now_kz_naive()
    db.commit()
    return match


def mark_unmatched(db: Session, *, canonical_product_id: int, reason: str = "") -> VidmanProductMatch:
    match = db.scalar(select(VidmanProductMatch).where(VidmanProductMatch.canonical_product_id == canonical_product_id))
    if match is None:
        match = VidmanProductMatch(canonical_product_id=canonical_product_id)
        db.add(match)
    match.product_id = None
    match.status = MANUAL_UNMATCHED
    match.match_type = "manual_unmatched"
    match.confidence = Decimal("0")
    match.evidence_json = json.dumps({"reason": reason}, ensure_ascii=False)
    match.matched_by = "manual"
    match.updated_at = now_kz_naive()
    db.commit()
    return match
