from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.app.models import (
    InternalProductNormalized,
    Product,
    ProductExtra,
    VidmanAccount,
    VidmanCanonicalProduct,
    VidmanInternalCoverageAudit,
    VidmanInternalCoverageDecision,
    VidmanInternalCoverageQueue,
    VidmanInternalCoverageRejection,
    VidmanPriceList,
    VidmanProductMatch,
    VidmanRawCanonicalLink,
    VidmanRawItem,
)
from backend.app.services.vidman_internal_coverage import (
    MANUAL_NO_VIDMAN_MATCH,
    internal_coverage_counts,
)
from backend.app.services.vidman_product_matching import (
    AUTO_MATCHED,
    MANUALLY_APPROVED,
    REVIEW_REQUIRED,
    UNMATCHED,
)
from backend.app.timezone import now_kz_naive


REVIEW_STATUSES = {"unreviewed", "reviewed", "all"}
COVERED_STATUSES = {AUTO_MATCHED, MANUALLY_APPROVED}


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _decimal(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _unit_value(value: Any, unit: str | None) -> str:
    if value is None:
        return ""
    numeric = Decimal(str(value)).normalize()
    return f"{numeric:f} {unit or ''}".strip()


def _internal_identity(row: InternalProductNormalized | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "baseName": row.base_name or "",
        "manufacturer": row.normalized_manufacturer or "",
        "dosage": _unit_value(row.dosage_value, row.dosage_unit),
        "strengths": row.strength_components or "",
        "concentration": _unit_value(row.concentration_value, row.concentration_unit),
        "volume": _unit_value(row.volume_value, row.volume_unit),
        "packageVolume": row.package_volume or "",
        "weight": _unit_value(row.weight_value, row.weight_unit),
        "packageWeight": row.package_weight or "",
        "pack": row.pack_count,
        "form": row.dosage_form or "",
        "variant": row.variant_text or "",
        "signature": row.normalized_signature or "",
    }


def _canonical_identity(row: VidmanCanonicalProduct | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "baseName": row.base_name or "",
        "manufacturer": row.canonical_manufacturer or "",
        "dosage": _unit_value(row.dosage_value, row.dosage_unit),
        "strengths": "",
        "concentration": _unit_value(row.concentration_value, row.concentration_unit),
        "volume": _unit_value(row.volume_value, row.volume_unit),
        "packageVolume": "",
        "weight": _unit_value(row.weight_value, row.weight_unit),
        "packageWeight": "",
        "pack": row.pack_count,
        "form": row.dosage_form or "",
        "variant": "",
        "signature": row.canonical_signature or "",
    }


def _coverage_status(match: VidmanProductMatch | None, decision: VidmanInternalCoverageDecision | None) -> str:
    if match is not None and match.status in COVERED_STATUSES:
        return match.status
    if decision is not None and decision.status:
        return decision.status
    return "UNREVIEWED"


def _covered_match_for_product(db: Session, product_id: int) -> VidmanProductMatch | None:
    return db.scalar(
        select(VidmanProductMatch).where(
            VidmanProductMatch.product_id == product_id,
            VidmanProductMatch.status.in_([AUTO_MATCHED, MANUALLY_APPROVED]),
        )
    )


def _decision_for_product(db: Session, product_id: int) -> VidmanInternalCoverageDecision | None:
    return db.scalar(select(VidmanInternalCoverageDecision).where(VidmanInternalCoverageDecision.product_id == product_id))


def list_internal_coverage(
    db: Session,
    *,
    tier: str | None = None,
    search: str = "",
    manufacturer: str = "",
    review_status: str = "unreviewed",
    page: int = 1,
    limit: int = 50,
    sort: str = "priority",
) -> dict[str, Any]:
    review_status = review_status if review_status in REVIEW_STATUSES else "unreviewed"
    covered_product_ids = (
        select(VidmanProductMatch.product_id)
        .where(VidmanProductMatch.product_id.is_not(None), VidmanProductMatch.status.in_([AUTO_MATCHED, MANUALLY_APPROVED]))
    )
    no_match_product_ids = select(VidmanInternalCoverageDecision.product_id).where(
        VidmanInternalCoverageDecision.status == MANUAL_NO_VIDMAN_MATCH
    )
    query = (
        select(
            VidmanInternalCoverageQueue,
            Product,
            ProductExtra,
            VidmanCanonicalProduct,
            VidmanProductMatch,
            VidmanInternalCoverageDecision,
        )
        .join(Product, Product.id == VidmanInternalCoverageQueue.product_id)
        .outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
        .outerjoin(VidmanCanonicalProduct, VidmanCanonicalProduct.id == VidmanInternalCoverageQueue.top_candidate_canonical_id)
        .outerjoin(
            VidmanProductMatch,
            VidmanProductMatch.product_id == Product.id,
        )
        .outerjoin(VidmanInternalCoverageDecision, VidmanInternalCoverageDecision.product_id == Product.id)
    )
    if tier:
        query = query.where(VidmanInternalCoverageQueue.tier == tier)
    if search:
        like = f"%{search.strip()}%"
        query = query.where(or_(Product.name.ilike(like), Product.code.ilike(like)))
    if manufacturer:
        query = query.where(ProductExtra.manufacturer.ilike(f"%{manufacturer.strip()}%"))
    if review_status == "unreviewed":
        query = query.where(Product.id.not_in(covered_product_ids), Product.id.not_in(no_match_product_ids))
    elif review_status == "reviewed":
        query = query.where(or_(Product.id.in_(covered_product_ids), Product.id.in_(no_match_product_ids)))

    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    if sort == "score":
        query = query.order_by(VidmanInternalCoverageQueue.top_candidate_score.desc().nullslast(), VidmanInternalCoverageQueue.id)
    elif sort == "gap":
        query = query.order_by(VidmanInternalCoverageQueue.score_gap.desc().nullslast(), VidmanInternalCoverageQueue.id)
    else:
        query = query.order_by(
            VidmanInternalCoverageQueue.tier,
            VidmanInternalCoverageQueue.top_candidate_score.desc().nullslast(),
            VidmanInternalCoverageQueue.id,
        )
    rows = db.execute(query.offset(max(page - 1, 0) * limit).limit(limit)).all()
    items = []
    seen: set[int] = set()
    for queue, product, extra, canonical, match, decision in rows:
        if product.id in seen:
            continue
        seen.add(product.id)
        status = _coverage_status(match, decision)
        items.append(
            {
                "productId": product.id,
                "productCode": product.code or "",
                "productName": product.name or "",
                "manufacturer": extra.manufacturer if extra else "",
                "coverageStatus": status,
                "topCanonicalProductId": queue.top_candidate_canonical_id,
                "topCanonicalName": canonical.canonical_name if canonical else "",
                "topScore": _decimal(queue.top_candidate_score),
                "secondScore": _decimal(queue.second_candidate_score),
                "scoreGap": _decimal(queue.score_gap),
                "candidateCount": queue.candidate_count,
                "reviewReason": queue.coverage_reason,
                "tier": queue.tier,
                "alreadyCovered": status in COVERED_STATUSES,
            }
        )
    return {"items": items, "page": page, "limit": limit, "total": total}


def internal_coverage_counters(db: Session) -> dict[str, Any]:
    total, covered, uncovered = internal_coverage_counts(db)
    tier_rows = dict(db.execute(select(VidmanInternalCoverageQueue.tier, func.count()).group_by(VidmanInternalCoverageQueue.tier)).all())
    covered_product_ids = set(
        db.scalars(
            select(VidmanProductMatch.product_id).where(
                VidmanProductMatch.product_id.is_not(None),
                VidmanProductMatch.status.in_([AUTO_MATCHED, MANUALLY_APPROVED]),
            )
        )
    )
    no_match_ids = set(
        db.scalars(
            select(VidmanInternalCoverageDecision.product_id).where(
                VidmanInternalCoverageDecision.status == MANUAL_NO_VIDMAN_MATCH
            )
        )
    )
    reviewed_ids = covered_product_ids | no_match_ids
    reviewed_by_tier: dict[str, int] = {}
    for tier_value, product_id in db.execute(select(VidmanInternalCoverageQueue.tier, VidmanInternalCoverageQueue.product_id)):
        if product_id in reviewed_ids:
            reviewed_by_tier[tier_value] = reviewed_by_tier.get(tier_value, 0) + 1
    tiers: dict[str, dict[str, int]] = {}
    for tier_value, tier_total in tier_rows.items():
        reviewed = reviewed_by_tier.get(tier_value, 0)
        tiers[tier_value] = {"total": int(tier_total), "reviewed": reviewed, "remaining": int(tier_total) - reviewed}
    strong_remaining = tiers.get("COVERAGE_A_STRONG", {}).get("remaining", 0)
    good_remaining = tiers.get("COVERAGE_B_GOOD", {}).get("remaining", 0)
    manual_reverse = int(
        db.scalar(
            select(func.count(VidmanProductMatch.id)).where(
                VidmanProductMatch.status == MANUALLY_APPROVED,
                VidmanProductMatch.matched_by == "manual_reverse",
            )
        )
        or 0
    )
    manual_no_match = int(
        db.scalar(
            select(func.count(VidmanInternalCoverageDecision.id)).where(
                VidmanInternalCoverageDecision.status == MANUAL_NO_VIDMAN_MATCH
            )
        )
        or 0
    )
    return {
        "tiers": tiers,
        "totalInternalProducts": total,
        "coveredInternalProducts": covered,
        "uncoveredInternalProducts": uncovered,
        "manuallyApprovedFromReverse": manual_reverse,
        "manualNoVidmanMatch": manual_no_match,
        "potentialCoverageStrong": covered + strong_remaining,
        "potentialCoverageStrongGood": covered + strong_remaining + good_remaining,
    }


def internal_coverage_detail(db: Session, *, product_id: int) -> dict[str, Any]:
    product = db.get(Product, product_id)
    if product is None:
        raise ValueError("internal product not found")
    queue = db.scalar(select(VidmanInternalCoverageQueue).where(VidmanInternalCoverageQueue.product_id == product_id))
    if queue is None:
        raise ValueError("coverage queue row not found")
    extra = db.get(ProductExtra, product_id)
    normalized = db.scalar(select(InternalProductNormalized).where(InternalProductNormalized.product_id == product_id))
    match = _covered_match_for_product(db, product_id)
    decision = _decision_for_product(db, product_id)
    rejected_ids = set(
        db.scalars(
            select(VidmanInternalCoverageRejection.canonical_product_id).where(
                VidmanInternalCoverageRejection.product_id == product_id
            )
        )
    )
    raw_candidates = _json_loads(queue.candidate_rankings_json, [])
    candidate_ids = [
        int(item.get("canonical_product_id") or 0)
        for item in raw_candidates
        if int(item.get("canonical_product_id") or 0) not in rejected_ids
    ]
    canonicals = {row.id: row for row in db.scalars(select(VidmanCanonicalProduct).where(VidmanCanonicalProduct.id.in_(candidate_ids or [-1])))}
    matches = {
        row.canonical_product_id: row
        for row in db.scalars(select(VidmanProductMatch).where(VidmanProductMatch.canonical_product_id.in_(candidate_ids or [-1])))
    }
    mapped_product_ids = [row.product_id for row in matches.values() if row.product_id]
    mapped_products = {row.id: row for row in db.scalars(select(Product).where(Product.id.in_(mapped_product_ids or [-1])))}
    candidates = []
    for item in raw_candidates if isinstance(raw_candidates, list) else []:
        canonical_id = int(item.get("canonical_product_id") or 0)
        if canonical_id in rejected_ids:
            continue
        canonical = canonicals.get(canonical_id)
        candidate_match = matches.get(canonical_id)
        mapped_product = mapped_products.get(candidate_match.product_id) if candidate_match and candidate_match.product_id else None
        candidates.append(
            {
                "canonicalProductId": canonical_id,
                "canonicalName": canonical.canonical_name if canonical else item.get("canonical_name", ""),
                "canonicalManufacturer": canonical.canonical_manufacturer if canonical else item.get("canonical_manufacturer", ""),
                "canonicalSignature": canonical.canonical_signature if canonical else item.get("canonical_signature", ""),
                "rank": item.get("rank"),
                "score": _decimal(item.get("score")),
                "nameScore": _decimal(item.get("name_score")),
                "manufacturerScore": _decimal(item.get("manufacturer_score")),
                "structuralScore": _decimal(item.get("structural_score")),
                "variantScore": _decimal(item.get("variant_score")),
                "sharedStructuralFields": item.get("shared_structural_fields") or [],
                "missingOnInternal": (item.get("missing_fields") or {}).get("internal") or [],
                "missingOnVidman": (item.get("missing_fields") or {}).get("vidman") or [],
                "hardConflicts": item.get("conflicts") or [],
                "reason": item.get("reason") or "",
                "identity": _canonical_identity(canonical),
                "rawExamples": _raw_examples_for_canonical(db, canonical_id),
                "mappingContext": {
                    "matchStatus": candidate_match.status if candidate_match else "UNASSIGNED",
                    "mappedProductId": candidate_match.product_id if candidate_match else None,
                    "mappedProductName": mapped_product.name if mapped_product else "",
                },
            }
        )
    return {
        "productId": product.id,
        "productCode": product.code or "",
        "productName": product.name or "",
        "manufacturer": extra.manufacturer if extra else "",
        "coverageStatus": _coverage_status(match, decision),
        "coverage": {
            "tier": queue.tier,
            "reviewReason": queue.coverage_reason,
            "candidateCount": queue.candidate_count,
            "topScore": _decimal(queue.top_candidate_score),
            "scoreGap": _decimal(queue.score_gap),
        },
        "normalized": _internal_identity(normalized),
        "candidates": candidates,
        "rejectedCandidates": [
            {"canonicalProductId": row.canonical_product_id, "reason": row.reason, "createdAt": row.created_at.isoformat()}
            for row in db.scalars(select(VidmanInternalCoverageRejection).where(VidmanInternalCoverageRejection.product_id == product_id))
        ],
        "audit": _audit_rows(db, product_id),
    }


def approve_internal_coverage(
    db: Session,
    *,
    product_id: int,
    canonical_product_id: int,
    actor: str = "",
) -> VidmanProductMatch:
    product = db.get(Product, product_id)
    canonical = db.get(VidmanCanonicalProduct, canonical_product_id)
    if product is None:
        raise ValueError("internal product not found")
    if canonical is None:
        raise ValueError("canonical product not found")
    existing_product_match = _covered_match_for_product(db, product_id)
    existing_canonical_match = db.scalar(
        select(VidmanProductMatch).where(VidmanProductMatch.canonical_product_id == canonical_product_id)
    )
    decision = _decision_for_product(db, product_id)
    if decision is not None and decision.status == MANUAL_NO_VIDMAN_MATCH:
        raise ValueError("internal product has a manual no Vidman match decision")
    if existing_product_match is not None and existing_product_match.canonical_product_id != canonical_product_id:
        raise ValueError("internal product is already covered by another Vidman canonical")
    if existing_canonical_match is not None and existing_canonical_match.product_id == product_id and existing_canonical_match.status in COVERED_STATUSES:
        return existing_canonical_match
    if existing_canonical_match is not None and existing_canonical_match.product_id not in {None, product_id}:
        if existing_canonical_match.status == AUTO_MATCHED:
            raise ValueError("canonical is AUTO_MATCHED to a different product")
        if existing_canonical_match.status == MANUALLY_APPROVED:
            raise ValueError("canonical is MANUALLY_APPROVED to a different product")
        raise ValueError("canonical is mapped to a different product")
    if existing_canonical_match is None:
        existing_canonical_match = VidmanProductMatch(canonical_product_id=canonical_product_id)
        db.add(existing_canonical_match)
        db.flush()
    if existing_canonical_match.status not in {"", REVIEW_REQUIRED, UNMATCHED} and existing_canonical_match.product_id not in {None, product_id}:
        raise ValueError(f"canonical status {existing_canonical_match.status} cannot be approved from reverse workflow")
    previous = existing_canonical_match.status or "UNASSIGNED"
    previous_product_id = existing_canonical_match.product_id
    existing_canonical_match.product_id = product_id
    existing_canonical_match.status = MANUALLY_APPROVED
    existing_canonical_match.match_type = "manual_reverse"
    existing_canonical_match.confidence = Decimal("100")
    existing_canonical_match.matched_by = "manual_reverse"
    existing_canonical_match.approved_at = now_kz_naive()
    existing_canonical_match.updated_at = now_kz_naive()
    _upsert_decision(db, product_id, canonical_product_id, "MANUAL_REVERSE_APPROVED", "", actor)
    _audit(db, product_id, canonical_product_id, previous, MANUALLY_APPROVED, "approve", "", actor)
    if previous_product_id is not None and previous_product_id != product_id:
        raise ValueError("refusing to reassign canonical from another product")
    db.commit()
    return existing_canonical_match


def reject_internal_coverage_candidate(
    db: Session,
    *,
    product_id: int,
    canonical_product_id: int,
    reason: str = "",
    actor: str = "",
) -> VidmanInternalCoverageRejection:
    if db.get(Product, product_id) is None:
        raise ValueError("internal product not found")
    if db.get(VidmanCanonicalProduct, canonical_product_id) is None:
        raise ValueError("canonical product not found")
    row = db.scalar(
        select(VidmanInternalCoverageRejection).where(
            VidmanInternalCoverageRejection.product_id == product_id,
            VidmanInternalCoverageRejection.canonical_product_id == canonical_product_id,
        )
    )
    if row is None:
        row = VidmanInternalCoverageRejection(product_id=product_id, canonical_product_id=canonical_product_id)
        db.add(row)
    row.reason = reason
    row.actor = actor
    _audit(db, product_id, canonical_product_id, "", "REJECTED_CANDIDATE", "reject_candidate", reason, actor)
    db.commit()
    return row


def mark_internal_coverage_no_match(db: Session, *, product_id: int, reason: str = "", actor: str = "") -> VidmanInternalCoverageDecision:
    if db.get(Product, product_id) is None:
        raise ValueError("internal product not found")
    if _covered_match_for_product(db, product_id) is not None:
        raise ValueError("internal product is already covered")
    previous = _decision_for_product(db, product_id)
    if previous is not None and previous.status == MANUAL_NO_VIDMAN_MATCH:
        return previous
    decision = _upsert_decision(db, product_id, None, MANUAL_NO_VIDMAN_MATCH, reason, actor)
    _audit(db, product_id, None, previous.status if previous else "", MANUAL_NO_VIDMAN_MATCH, "mark_no_match", reason, actor)
    db.commit()
    return decision


def _raw_examples_for_canonical(db: Session, canonical_product_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        select(
            VidmanRawItem.raw_name,
            VidmanRawItem.raw_manufacturer,
            VidmanRawItem.main_id,
            VidmanRawItem.price,
            VidmanAccount.login,
            VidmanPriceList.name,
        )
        .join(VidmanRawCanonicalLink, VidmanRawCanonicalLink.raw_item_id == VidmanRawItem.id)
        .outerjoin(VidmanAccount, VidmanAccount.id == VidmanRawItem.account_id)
        .outerjoin(VidmanPriceList, VidmanPriceList.id == VidmanRawItem.price_list_id)
        .where(VidmanRawCanonicalLink.canonical_product_id == canonical_product_id)
        .limit(5)
    ).all()
    return [
        {
            "rawName": name or "",
            "rawManufacturer": manufacturer or "",
            "mainId": main_id,
            "price": _decimal(price),
            "account": account or "",
            "priceList": price_list or "",
        }
        for name, manufacturer, main_id, price, account, price_list in rows
    ]


def _audit_rows(db: Session, product_id: int) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(VidmanInternalCoverageAudit)
        .where(VidmanInternalCoverageAudit.product_id == product_id)
        .order_by(VidmanInternalCoverageAudit.created_at.desc(), VidmanInternalCoverageAudit.id.desc())
        .limit(20)
    )
    return [
        {
            "action": row.action,
            "canonicalProductId": row.canonical_product_id,
            "previousState": row.previous_state,
            "newState": row.new_state,
            "reason": row.reason,
            "actor": row.actor,
            "createdAt": row.created_at.isoformat(),
        }
        for row in rows
    ]


def _upsert_decision(
    db: Session,
    product_id: int,
    canonical_product_id: int | None,
    status: str,
    reason: str,
    actor: str,
) -> VidmanInternalCoverageDecision:
    row = _decision_for_product(db, product_id)
    if row is None:
        row = VidmanInternalCoverageDecision(product_id=product_id)
        db.add(row)
    row.status = status
    row.canonical_product_id = canonical_product_id
    row.reason = reason
    row.actor = actor
    row.updated_at = now_kz_naive()
    return row


def _audit(
    db: Session,
    product_id: int,
    canonical_product_id: int | None,
    previous_state: str,
    new_state: str,
    action: str,
    reason: str,
    actor: str,
) -> None:
    db.add(
        VidmanInternalCoverageAudit(
            product_id=product_id,
            canonical_product_id=canonical_product_id,
            previous_state=previous_state,
            new_state=new_state,
            action=action,
            reason=reason,
            actor=actor,
        )
    )
