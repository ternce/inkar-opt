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
    VidmanPriceList,
    VidmanProductMatch,
    VidmanProductMatchAudit,
    VidmanProductReviewQueue,
    VidmanRawCanonicalLink,
    VidmanRawItem,
    VidmanRejectedCandidate,
)
from backend.app.services.vidman_product_matching import (
    MANUALLY_APPROVED,
    MANUAL_UNMATCHED,
    REVIEW_REQUIRED,
)
from backend.app.timezone import now_kz_naive


REVIEW_STATUSES = {"unreviewed", "reviewed", "all"}


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


def _signature_payload(row: VidmanCanonicalProduct | InternalProductNormalized | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "baseName": getattr(row, "base_name", "") or "",
        "manufacturer": getattr(row, "canonical_manufacturer", None) or getattr(row, "normalized_manufacturer", "") or "",
        "dosage": _unit_value(getattr(row, "dosage_value", None), getattr(row, "dosage_unit", "")),
        "strengths": getattr(row, "strength_components", "") if isinstance(row, InternalProductNormalized) else "",
        "concentration": _unit_value(getattr(row, "concentration_value", None), getattr(row, "concentration_unit", "")),
        "volume": _unit_value(getattr(row, "volume_value", None), getattr(row, "volume_unit", "")),
        "packageVolume": getattr(row, "package_volume", "") if isinstance(row, InternalProductNormalized) else "",
        "weight": _unit_value(getattr(row, "weight_value", None), getattr(row, "weight_unit", "")),
        "packageWeight": getattr(row, "package_weight", "") if isinstance(row, InternalProductNormalized) else "",
        "pack": getattr(row, "pack_count", None),
        "form": getattr(row, "dosage_form", "") or "",
        "variant": getattr(row, "variant_text", "") if isinstance(row, InternalProductNormalized) else "",
        "signature": getattr(row, "canonical_signature", None) or getattr(row, "normalized_signature", "") or "",
    }


def _unit_value(value: Any, unit: str | None) -> str:
    if value is None:
        return ""
    numeric = Decimal(str(value)).normalize()
    return f"{numeric:f} {unit or ''}".strip()


def _match_payload(match: VidmanProductMatch | None) -> dict[str, Any]:
    if match is None:
        return {"status": ""}
    return {
        "status": match.status,
        "productId": match.product_id,
        "matchType": match.match_type,
        "confidence": _decimal(match.confidence),
        "matchedBy": match.matched_by,
        "approvedAt": match.approved_at.isoformat() if match.approved_at else None,
        "updatedAt": match.updated_at.isoformat() if match.updated_at else None,
    }


def _candidate_payload(product: Product | None, extra: ProductExtra | None, normalized: InternalProductNormalized | None) -> dict[str, Any]:
    return {
        "productId": product.id if product else None,
        "code": product.code if product else "",
        "name": product.name if product else "",
        "manufacturer": extra.manufacturer if extra else "",
        "normalized": _signature_payload(normalized),
    }


def _candidate_rankings(
    queue: VidmanProductReviewQueue,
    products: dict[int, Product],
    extras: dict[int, ProductExtra],
    normalized: dict[int, InternalProductNormalized],
    rejected_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    rejected_ids = rejected_ids or set()
    raw = _json_loads(queue.candidate_rankings_json, [])
    enriched = []
    for item in raw if isinstance(raw, list) else []:
        product_id = int(item.get("product_id") or 0)
        if product_id in rejected_ids:
            continue
        product = products.get(product_id)
        enriched.append(
            {
                "rank": item.get("rank"),
                "score": _decimal(item.get("score")),
                "nameScore": _decimal(item.get("name_score")),
                "manufacturerScore": _decimal(item.get("manufacturer_score")),
                "structuralScore": _decimal(item.get("structural_score")),
                "variantScore": _decimal(item.get("variant_score")),
                "sharedFields": item.get("shared_structural_fields") or [],
                "missingFields": item.get("missing_fields") or {},
                "conflicts": item.get("conflicts") or [],
                "reason": item.get("reason") or "",
                **_candidate_payload(product, extras.get(product_id), normalized.get(product_id)),
            }
        )
    return enriched


def list_review_queue(
    db: Session,
    *,
    tier: str | None = None,
    status: str = "unreviewed",
    search: str = "",
    manufacturer: str = "",
    page: int = 1,
    limit: int = 50,
    sort: str = "priority",
) -> dict[str, Any]:
    status = status if status in REVIEW_STATUSES else "unreviewed"
    query = (
        select(VidmanProductReviewQueue, VidmanCanonicalProduct, VidmanProductMatch, Product)
        .join(VidmanCanonicalProduct, VidmanCanonicalProduct.id == VidmanProductReviewQueue.canonical_product_id)
        .outerjoin(VidmanProductMatch, VidmanProductMatch.canonical_product_id == VidmanProductReviewQueue.canonical_product_id)
        .outerjoin(Product, Product.id == VidmanProductReviewQueue.top_candidate_product_id)
    )
    if tier:
        query = query.where(VidmanProductReviewQueue.tier == tier)
    if search:
        like = f"%{search.strip()}%"
        query = query.where(or_(VidmanCanonicalProduct.canonical_name.ilike(like), VidmanCanonicalProduct.canonical_signature.ilike(like)))
    if manufacturer:
        query = query.where(VidmanCanonicalProduct.canonical_manufacturer.ilike(f"%{manufacturer.strip()}%"))
    if status == "unreviewed":
        query = query.where(func.coalesce(VidmanProductMatch.status, "") == REVIEW_REQUIRED)
    elif status == "reviewed":
        query = query.where(func.coalesce(VidmanProductMatch.status, "") != REVIEW_REQUIRED)

    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    if sort == "score":
        query = query.order_by(VidmanProductReviewQueue.top_candidate_score.desc().nullslast(), VidmanProductReviewQueue.id)
    elif sort == "gap":
        query = query.order_by(VidmanProductReviewQueue.score_gap.desc().nullslast(), VidmanProductReviewQueue.id)
    else:
        query = query.order_by(VidmanProductReviewQueue.tier, VidmanProductReviewQueue.top_candidate_score.desc().nullslast(), VidmanProductReviewQueue.id)

    offset = max(page - 1, 0) * limit
    rows = db.execute(query.offset(offset).limit(limit)).all()
    items = [
        {
            "canonicalProductId": queue.canonical_product_id,
            "canonicalName": canonical.canonical_name,
            "canonicalManufacturer": canonical.canonical_manufacturer,
            "canonicalSignature": canonical.canonical_signature,
            "tier": queue.tier,
            "reviewReason": queue.review_reason,
            "topCandidateProductId": queue.top_candidate_product_id,
            "topCandidateName": product.name if product else "",
            "topCandidateScore": _decimal(queue.top_candidate_score),
            "scoreGap": _decimal(queue.score_gap),
            "candidateCount": queue.candidate_count,
            "matchStatus": match.status if match else "",
        }
        for queue, canonical, match, product in rows
    ]
    return {"items": items, "page": page, "limit": limit, "total": total}


def review_counters(db: Session) -> dict[str, Any]:
    tier_rows = dict(db.execute(select(VidmanProductReviewQueue.tier, func.count()).group_by(VidmanProductReviewQueue.tier)).all())
    reviewed_by_tier = dict(
        db.execute(
            select(VidmanProductReviewQueue.tier, func.count())
            .join(VidmanProductMatch, VidmanProductMatch.canonical_product_id == VidmanProductReviewQueue.canonical_product_id)
            .where(VidmanProductMatch.status != REVIEW_REQUIRED)
            .group_by(VidmanProductReviewQueue.tier)
        ).all()
    )
    statuses = dict(db.execute(select(VidmanProductMatch.status, func.count()).group_by(VidmanProductMatch.status)).all())
    tiers = {}
    for tier, total in tier_rows.items():
        reviewed = int(reviewed_by_tier.get(tier, 0))
        tiers[tier] = {"total": int(total), "reviewed": reviewed, "remaining": int(total) - reviewed}
    return {
        "tiers": tiers,
        "statuses": {key: int(value) for key, value in statuses.items()},
        "manuallyApproved": int(statuses.get(MANUALLY_APPROVED, 0)),
        "manuallyUnmatched": int(statuses.get(MANUAL_UNMATCHED, 0)),
    }


def review_detail(db: Session, *, canonical_product_id: int) -> dict[str, Any]:
    canonical = db.get(VidmanCanonicalProduct, canonical_product_id)
    if canonical is None:
        raise ValueError("canonical product not found")
    queue = db.scalar(select(VidmanProductReviewQueue).where(VidmanProductReviewQueue.canonical_product_id == canonical_product_id))
    if queue is None:
        raise ValueError("review queue row not found")
    match = db.scalar(select(VidmanProductMatch).where(VidmanProductMatch.canonical_product_id == canonical_product_id))
    rejected_rows = list(
        db.scalars(select(VidmanRejectedCandidate).where(VidmanRejectedCandidate.canonical_product_id == canonical_product_id))
    )
    rejected_ids = {row.product_id for row in rejected_rows}
    candidate_ids = [
        int(item.get("product_id") or 0)
        for item in _json_loads(queue.candidate_rankings_json, [])
        if int(item.get("product_id") or 0) not in rejected_ids
    ]
    products = {row.id: row for row in db.scalars(select(Product).where(Product.id.in_(candidate_ids or [-1])))}
    extras = {row.product_id: row for row in db.scalars(select(ProductExtra).where(ProductExtra.product_id.in_(candidate_ids or [-1])))}
    normalized = {
        row.product_id: row
        for row in db.scalars(select(InternalProductNormalized).where(InternalProductNormalized.product_id.in_(candidate_ids or [-1])))
    }
    candidates = _candidate_rankings(queue, products, extras, normalized, rejected_ids)
    raw_examples = _raw_examples(db, canonical_product_id)
    return {
        "canonicalProductId": canonical.id,
        "tier": queue.tier,
        "reviewReason": queue.review_reason,
        "topCandidateScore": _decimal(queue.top_candidate_score),
        "scoreGap": _decimal(queue.score_gap),
        "candidateCount": queue.candidate_count,
        "match": _match_payload(match),
        "vidman": {
            "canonicalName": canonical.canonical_name,
            "canonicalManufacturer": canonical.canonical_manufacturer,
            "canonicalSignature": canonical.canonical_signature,
            "identity": _signature_payload(canonical),
        },
        "sourceContext": {
            "rawExamples": raw_examples,
            "accounts": sorted({item["account"] for item in raw_examples if item.get("account")}),
            "plks": sorted({item["priceList"] for item in raw_examples if item.get("priceList")}),
        },
        "candidates": candidates,
        "rejectedCandidates": [
            {"productId": row.product_id, "reason": row.reason, "createdAt": row.created_at.isoformat()} for row in rejected_rows
        ],
        "audit": _audit_rows(db, canonical_product_id),
    }


def _raw_examples(db: Session, canonical_product_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        select(VidmanRawItem.raw_name, VidmanRawItem.raw_manufacturer, VidmanAccount.login, VidmanPriceList.name)
        .join(VidmanRawCanonicalLink, VidmanRawCanonicalLink.raw_item_id == VidmanRawItem.id)
        .outerjoin(VidmanAccount, VidmanAccount.id == VidmanRawItem.account_id)
        .outerjoin(VidmanPriceList, VidmanPriceList.id == VidmanRawItem.price_list_id)
        .where(VidmanRawCanonicalLink.canonical_product_id == canonical_product_id)
        .limit(5)
    ).all()
    return [
        {"rawName": name or "", "rawManufacturer": manufacturer or "", "account": account or "", "priceList": price_list or ""}
        for name, manufacturer, account, price_list in rows
    ]


def _audit_rows(db: Session, canonical_product_id: int) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(VidmanProductMatchAudit)
        .where(VidmanProductMatchAudit.canonical_product_id == canonical_product_id)
        .order_by(VidmanProductMatchAudit.created_at.desc(), VidmanProductMatchAudit.id.desc())
        .limit(20)
    )
    return [
        {
            "action": row.action,
            "previousStatus": row.previous_status,
            "newStatus": row.new_status,
            "previousProductId": row.previous_product_id,
            "newProductId": row.new_product_id,
            "reason": row.reason,
            "actor": row.actor,
            "createdAt": row.created_at.isoformat(),
        }
        for row in rows
    ]


def search_internal_products(db: Session, *, q: str, limit: int = 20) -> list[dict[str, Any]]:
    q = q.strip()
    if len(q) < 2:
        return []
    like = f"%{q}%"
    rows = db.execute(
        select(Product, ProductExtra, InternalProductNormalized)
        .outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
        .outerjoin(InternalProductNormalized, InternalProductNormalized.product_id == Product.id)
        .where(or_(Product.name.ilike(like), Product.code.ilike(like), ProductExtra.manufacturer.ilike(like)))
        .order_by(Product.name)
        .limit(limit)
    ).all()
    return [_candidate_payload(product, extra, normalized) for product, extra, normalized in rows]


def approve_review_match(db: Session, *, canonical_product_id: int, product_id: int, actor: str = "") -> VidmanProductMatch:
    canonical = db.get(VidmanCanonicalProduct, canonical_product_id)
    product = db.get(Product, product_id)
    if canonical is None:
        raise ValueError("canonical product not found")
    if product is None:
        raise ValueError("internal product not found")
    match = _current_or_new_match(db, canonical_product_id)
    if match.status == MANUALLY_APPROVED:
        return match
    previous_status, previous_product_id = match.status or "", match.product_id
    match.product_id = product_id
    match.status = MANUALLY_APPROVED
    match.match_type = "manual_review"
    match.confidence = Decimal("100")
    match.matched_by = "manual"
    match.approved_at = now_kz_naive()
    match.updated_at = now_kz_naive()
    _audit(db, canonical_product_id, previous_status, MANUALLY_APPROVED, previous_product_id, product_id, "approve", "", actor)
    db.commit()
    return match


def reject_review_candidate(db: Session, *, canonical_product_id: int, product_id: int, reason: str = "", actor: str = "") -> VidmanRejectedCandidate:
    if db.get(VidmanCanonicalProduct, canonical_product_id) is None:
        raise ValueError("canonical product not found")
    if db.get(Product, product_id) is None:
        raise ValueError("internal product not found")
    row = db.scalar(
        select(VidmanRejectedCandidate).where(
            VidmanRejectedCandidate.canonical_product_id == canonical_product_id,
            VidmanRejectedCandidate.product_id == product_id,
        )
    )
    if row is None:
        row = VidmanRejectedCandidate(canonical_product_id=canonical_product_id, product_id=product_id)
        db.add(row)
    row.reason = reason
    row.actor = actor
    _audit(db, canonical_product_id, "", "", None, product_id, "reject_candidate", reason, actor)
    db.commit()
    return row


def mark_review_unmatched(db: Session, *, canonical_product_id: int, reason: str = "", actor: str = "") -> VidmanProductMatch:
    if db.get(VidmanCanonicalProduct, canonical_product_id) is None:
        raise ValueError("canonical product not found")
    match = _current_or_new_match(db, canonical_product_id)
    if match.status == MANUAL_UNMATCHED:
        return match
    previous_status, previous_product_id = match.status or "", match.product_id
    match.product_id = None
    match.status = MANUAL_UNMATCHED
    match.match_type = "manual_unmatched"
    match.confidence = Decimal("0")
    match.evidence_json = json.dumps({"reason": reason}, ensure_ascii=False)
    match.matched_by = "manual"
    match.updated_at = now_kz_naive()
    _audit(db, canonical_product_id, previous_status, MANUAL_UNMATCHED, previous_product_id, None, "mark_unmatched", reason, actor)
    db.commit()
    return match


def _current_or_new_match(db: Session, canonical_product_id: int) -> VidmanProductMatch:
    match = db.scalar(select(VidmanProductMatch).where(VidmanProductMatch.canonical_product_id == canonical_product_id))
    if match is None:
        match = VidmanProductMatch(canonical_product_id=canonical_product_id)
        db.add(match)
        db.flush()
    return match


def _audit(
    db: Session,
    canonical_product_id: int,
    previous_status: str,
    new_status: str,
    previous_product_id: int | None,
    new_product_id: int | None,
    action: str,
    reason: str,
    actor: str,
) -> None:
    db.add(
        VidmanProductMatchAudit(
            canonical_product_id=canonical_product_id,
            previous_status=previous_status,
            new_status=new_status,
            previous_product_id=previous_product_id,
            new_product_id=new_product_id,
            action=action,
            reason=reason,
            actor=actor,
        )
    )
