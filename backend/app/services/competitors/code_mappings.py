from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from ...models import (
    CompetitorCodeMapping,
    CompetitorPriceList,
    CompetitorPriceListItem,
    CalculatedPrice,
    PriceList,
    Product,
    ProductExtra,
)
from ..competitor_assignments import get_assigned_competitor_price_lists

SUPPORTED_PLATFORMS = {"provisor", "vidman"}
MANUAL_SUGGESTION_MIN_SCORE = 55.0


def _same_manual_value(left: object, right: object) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) < 0.001
    return left == right


def _manual_suggestion_score(
    *,
    product_name: str,
    product_manufacturer: str,
    candidate_name: str,
    candidate_manufacturer: str,
) -> tuple[float, dict] | None:
    """Broader scoring used only by the manual Matching Table workflow."""
    # Local import avoids a module cycle: the automatic matcher imports this
    # module to apply approved mappings.  No automatic matcher rule is changed.
    from ..competitor_matching import _base_name_similarity, parse_drug_structure

    product = parse_drug_structure(product_name)
    candidate = parse_drug_structure(candidate_name)
    product_base = product.base_name or normalize_mapping_text(product_name)
    candidate_base = candidate.base_name or normalize_mapping_text(candidate_name)
    if not product_base or not candidate_base:
        return None
    if product_base.split(" ", 1)[0] != candidate_base.split(" ", 1)[0]:
        return None

    name_score = _base_name_similarity(product_base, candidate_base)
    if name_score < 68:
        return None

    strength_fields = (
        "dosage",
        "dosage_volume",
        "concentration",
        "percent_strength",
        "iu_dosage",
        "strength_signature",
        "volume",
        "weight",
    )
    strength_matches = 0
    missing_candidate_strength = False
    for field in strength_fields:
        left = getattr(product, field, None)
        right = getattr(candidate, field, None)
        if left is not None and right is not None:
            if not _same_manual_value(left, right):
                return None
            strength_matches += 1
        elif left is not None and right is None:
            missing_candidate_strength = True

    score = name_score * 0.60
    if strength_matches:
        score += 18
    elif missing_candidate_strength:
        score -= 6

    quantity_match = None
    if product.quantity is not None and candidate.quantity is not None:
        quantity_match = product.quantity == candidate.quantity
        score += 8 if quantity_match else -8

    product_forms = set(product.forms or ((product.form,) if product.form else ()))
    candidate_forms = set(candidate.forms or ((candidate.form,) if candidate.form else ()))
    form_match = None
    if product_forms and candidate_forms:
        form_match = not product_forms.isdisjoint(candidate_forms)
        score += 6 if form_match else -6

    manufacturer_score = 0.0
    if product_manufacturer and candidate_manufacturer:
        manufacturer_score = _base_name_similarity(product_manufacturer, candidate_manufacturer)
        score += manufacturer_score * 0.08

    score = round(max(0.0, min(100.0, score)), 2)
    if score < MANUAL_SUGGESTION_MIN_SCORE:
        return None
    return score, {
        "nameScore": round(name_score, 2),
        "dosageMatch": bool(strength_matches),
        "quantityMatch": quantity_match,
        "formMatch": form_match,
        "manufacturerScore": round(manufacturer_score, 2),
    }


def normalize_mapping_text(value: object) -> str:
    text = str(value or "").strip().casefold()
    text = text.replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def platform_from_value(value: object) -> str:
    platform = str(value or "").strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError("platform must be provisor or vidman")
    return platform


def source_external_key_for_item(platform: str, item: CompetitorPriceListItem) -> str | None:
    if platform == "provisor":
        if item.provisor_goods_id is not None:
            return str(item.provisor_goods_id)
        if item.distributor_goods_id:
            return str(item.distributor_goods_id)
        return None
    return str(item.distributor_goods_id or "").strip() or None


def source_match_key(
    *,
    platform: str,
    source_external_key: object = None,
    source_name: object = None,
    source_manufacturer: object = None,
) -> str:
    platform = platform_from_value(platform)
    external = str(source_external_key or "").strip()
    if platform == "provisor" and external:
        return f"provisor:{external}"
    name_norm = normalize_mapping_text(source_name)
    manufacturer_norm = normalize_mapping_text(source_manufacturer)
    if external:
        return f"{platform}:{external}"
    return f"{platform}:name:{name_norm}|manufacturer:{manufacturer_norm}"


def source_match_key_for_item(platform: str, item: CompetitorPriceListItem) -> str:
    external = source_external_key_for_item(platform, item)
    name = item.raw_name or item.name or item.distributor_goods_name
    manufacturer = item.raw_manufacturer or ""
    return source_match_key(
        platform=platform,
        source_external_key=external,
        source_name=name,
        source_manufacturer=manufacturer,
    )


def mapping_source_payload(platform: str, item: CompetitorPriceListItem) -> dict:
    name = item.raw_name or item.name or item.distributor_goods_name or ""
    manufacturer = item.raw_manufacturer or ""
    external = source_external_key_for_item(platform, item)
    return {
        "source_external_key": external,
        "source_match_key": source_match_key(
            platform=platform,
            source_external_key=external,
            source_name=name,
            source_manufacturer=manufacturer,
        ),
        "source_name": name,
        "source_manufacturer": manufacturer,
        "source_dosage_form": item.parsed_form or "",
        "source_normalized_name": item.normalized_name or normalize_mapping_text(name),
    }


def _product_payload(product: Product | None, extra: ProductExtra | None = None) -> dict:
    return {
        "ourProductId": int(product.id) if product else None,
        "ourSku": product.code if product else "",
        "ourName": product.name if product else "",
        "ourManufacturer": (extra.manufacturer if extra else "") or "",
    }


def mapping_to_dict(row: CompetitorCodeMapping, product: Product | None = None, extra: ProductExtra | None = None) -> dict:
    return {
        "id": row.id,
        "platform": row.platform,
        "sourceExternalKey": row.source_external_key,
        "sourceMatchKey": row.source_match_key,
        "sourceName": row.source_name,
        "sourceManufacturer": row.source_manufacturer,
        "sourceDosageForm": row.source_dosage_form,
        "sourceNormalizedName": row.source_normalized_name,
        "status": row.status,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "createdAt": row.created_at.isoformat() if row.created_at else "",
        "updatedAt": row.updated_at.isoformat() if row.updated_at else "",
        "approvedAt": row.approved_at.isoformat() if row.approved_at else "",
        "createdBy": row.created_by,
        **_product_payload(product, extra),
    }


def _status_for_item(item: CompetitorPriceListItem, mapping: CompetitorCodeMapping | None) -> str:
    if mapping is not None and mapping.status in {"mapped", "rejected", "unmapped"}:
        return mapping.status
    if item.product_id is not None or item.matched_sku:
        return "mapped"
    return "unmapped"


def _coverage_for_platform(db: Session, platform: str, price_format_id: int | None = None) -> dict:
    assigned_ids = None
    if price_format_id is not None:
        assigned_ids = [int(item.price_list.id) for item in get_assigned_competitor_price_lists(db=db, price_format_id=price_format_id)]
    stmt = (
        select(CompetitorPriceListItem)
        .join(CompetitorPriceList, CompetitorPriceList.id == CompetitorPriceListItem.price_list_id)
        .where(CompetitorPriceList.source_type == platform)
    )
    if assigned_ids is not None:
        stmt = stmt.where(CompetitorPriceList.id.in_(assigned_ids))
    items = db.execute(stmt).scalars().all()
    keys = {source_match_key_for_item(platform, item) for item in items}
    mappings = {}
    if keys:
        mappings = {
            row.source_match_key: row
            for row in db.execute(
                select(CompetitorCodeMapping)
                .where(CompetitorCodeMapping.platform == platform)
                .where(CompetitorCodeMapping.source_match_key.in_(keys))
            ).scalars().all()
        }
    total = len(keys)
    mapped = 0
    rejected = 0
    seen: set[str] = set()
    for item in items:
        key = source_match_key_for_item(platform, item)
        if key in seen:
            continue
        seen.add(key)
        status = _status_for_item(item, mappings.get(key))
        mapped += int(status == "mapped")
        rejected += int(status == "rejected")
    unmapped = max(0, total - mapped - rejected)
    return {
        "platform": platform,
        "total": total,
        "mapped": mapped,
        "unmapped": unmapped,
        "rejected": rejected,
        "coveragePercent": round((mapped / total) * 100, 2) if total else 0,
    }


def _generated_pricing_coverage(db: Session, price_format_id: int | None = None) -> dict:
    if price_format_id is None:
        return {"priceListId": None, "priceListNumber": "", "total": 0, "withCompetitors": 0, "withoutCompetitors": 0, "coveragePercent": 0}
    row = (
        db.execute(
            select(PriceList)
            .where(PriceList.price_format_id == price_format_id)
            .order_by(desc(PriceList.created_at), desc(PriceList.id))
            .limit(1)
        )
        .scalars()
        .first()
    )
    if row is None:
        return {"priceListId": None, "priceListNumber": "", "total": 0, "withCompetitors": 0, "withoutCompetitors": 0, "coveragePercent": 0}
    total = int(db.execute(select(func.count(CalculatedPrice.id)).where(CalculatedPrice.price_list_id == row.id)).scalar() or 0)
    with_competitors = int(
        db.execute(
            select(func.count(CalculatedPrice.id))
            .where(CalculatedPrice.price_list_id == row.id)
            .where(CalculatedPrice.competitor_price.is_not(None))
        ).scalar()
        or 0
    )
    without_competitors = max(0, total - with_competitors)
    return {
        "priceListId": int(row.id),
        "priceListNumber": row.number,
        "total": total,
        "withCompetitors": with_competitors,
        "withoutCompetitors": without_competitors,
        "coveragePercent": round((with_competitors / total) * 100, 2) if total else 0,
    }


def _source_item_to_payload(platform: str, item: CompetitorPriceListItem, price_list: CompetitorPriceList) -> dict:
    source_payload = mapping_source_payload(platform, item)
    return {
        "itemId": item.id,
        "priceListId": price_list.id,
        "priceListName": price_list.display_name or price_list.supplier or price_list.source_key,
        "platform": platform,
        "matchType": item.match_type or "",
        "matchedSku": item.matched_sku or "",
        "sourcePrice": float(item.distributor_price) if item.distributor_price is not None else None,
        "priceDate": price_list.price_date.isoformat() if price_list.price_date else "",
        "confidence": float(item.match_score) if item.match_score is not None else None,
        "sourceExternalKey": source_payload["source_external_key"],
        "sourceMatchKey": source_payload["source_match_key"],
        "sourceName": source_payload["source_name"],
        "sourceManufacturer": source_payload["source_manufacturer"],
        "sourceDosageForm": source_payload["source_dosage_form"],
        "sourceNormalizedName": source_payload["source_normalized_name"],
    }


def list_catalog_code_mappings(
    *,
    db: Session,
    platform: str,
    price_format_id: int | None = None,
    status: str = "all",
    source_q: str = "",
    product_q: str = "",
    page: int = 1,
    limit: int = 300,
    include_candidates: bool = True,
) -> dict:
    platform = platform_from_value(platform)
    status = status if status in {"all", "mapped", "unmapped", "rejected", "no_candidates"} else "all"
    limit = max(1, min(int(limit or 300), 1000))
    page = max(1, int(page or 1))

    product_stmt = (
        select(Product, ProductExtra)
        .join(ProductExtra, ProductExtra.product_id == Product.id, isouter=True)
        .order_by(Product.code.asc())
    )
    product_rows = db.execute(product_stmt).all()
    products_by_id = {int(product.id): (product, extra) for product, extra in product_rows}
    products_by_sku = {product.code: int(product.id) for product, _ in product_rows}

    item_stmt = (
        select(CompetitorPriceListItem, CompetitorPriceList)
        .join(CompetitorPriceList, CompetitorPriceList.id == CompetitorPriceListItem.price_list_id)
        .where(CompetitorPriceList.source_type == platform)
        .order_by(desc(CompetitorPriceList.price_date), desc(CompetitorPriceListItem.match_score), CompetitorPriceListItem.id.desc())
    )
    if price_format_id is not None:
        assigned_ids = [int(item.price_list.id) for item in get_assigned_competitor_price_lists(db=db, price_format_id=price_format_id)]
        item_stmt = item_stmt.where(CompetitorPriceList.id.in_(assigned_ids))
    source_like = f"%{source_q.strip()}%" if source_q.strip() else ""
    if source_like:
        item_stmt = item_stmt.where(
            (CompetitorPriceListItem.name.ilike(source_like))
            | (CompetitorPriceListItem.raw_name.ilike(source_like))
            | (CompetitorPriceListItem.distributor_goods_name.ilike(source_like))
            | (CompetitorPriceListItem.raw_manufacturer.ilike(source_like))
            | (CompetitorPriceListItem.distributor_goods_id.ilike(source_like))
        )
    item_rows = db.execute(item_stmt).all()

    item_payloads: list[dict] = []
    items_by_product: dict[int, list[dict]] = {}
    payload_by_match_key: dict[str, dict] = {}
    payloads_by_external_key: dict[str, list[dict]] = {}
    keys: set[str] = set()
    for item, price_list in item_rows:
        payload = _source_item_to_payload(platform, item, price_list)
        item_payloads.append(payload)
        match_key = str(payload["sourceMatchKey"] or "")
        keys.add(match_key)
        if match_key and match_key not in payload_by_match_key:
            payload_by_match_key[match_key] = payload
        external_key = str(payload.get("sourceExternalKey") or "")
        if external_key:
            payloads_by_external_key.setdefault(external_key, []).append(payload)
        product_id = int(item.product_id) if item.product_id else None
        if product_id is None and item.matched_sku:
            product_id = products_by_sku.get(item.matched_sku)
        if product_id:
            items_by_product.setdefault(product_id, []).append(payload)

    source_search_requested = bool(source_q.strip())
    classify_candidates_during_scan = status in {"no_candidates", "rejected"} or source_search_requested
    needs_candidate_index = include_candidates or classify_candidates_during_scan
    manual_candidates_by_first_token: dict[str, list[dict]] = {}
    seen_manual_source_keys: set[str] = set()
    if needs_candidate_index:
        from ..competitor_matching import parse_drug_structure

        for payload in item_payloads:
            source_key = str(payload.get("sourceMatchKey") or payload.get("itemId") or "")
            if source_key in seen_manual_source_keys:
                continue
            seen_manual_source_keys.add(source_key)
            structure = parse_drug_structure(payload.get("sourceName") or "")
            base_name = structure.base_name or normalize_mapping_text(payload.get("sourceName"))
            first_token = base_name.split(" ", 1)[0] if base_name else ""
            if first_token:
                manual_candidates_by_first_token.setdefault(first_token, []).append(payload)

    def candidate_pool_for_product(product: Product) -> list[dict]:
        candidate_pool = []
        if manual_candidates_by_first_token:
            from ..competitor_matching import parse_drug_structure

            product_structure = parse_drug_structure(product.name)
            product_base = product_structure.base_name or normalize_mapping_text(product.name)
            first_token = product_base.split(" ", 1)[0] if product_base else ""
            candidate_pool = list(manual_candidates_by_first_token.get(first_token, []))
        candidate_pool.extend(items_by_product.get(int(product.id), []))
        return candidate_pool

    def score_candidates_for_product(product: Product, extra: ProductExtra | None, candidate_pool: list[dict]) -> list[dict]:
        candidate_by_key: dict[str, dict] = {}
        for candidate in candidate_pool:
            scored = _manual_suggestion_score(
                product_name=product.name,
                product_manufacturer=(extra.manufacturer if extra else "") or "",
                candidate_name=str(candidate.get("sourceName") or ""),
                candidate_manufacturer=str(candidate.get("sourceManufacturer") or ""),
            )
            if scored is None:
                continue
            confidence, score_details = scored
            candidate_key = str(candidate.get("sourceMatchKey") or candidate.get("itemId") or "")
            candidate_payload = {
                **candidate,
                "confidence": confidence,
                "matchType": "manual_suggestion",
                "manualSuggestion": score_details,
            }
            previous = candidate_by_key.get(candidate_key)
            if previous is None or float(previous.get("confidence") or 0) < confidence:
                candidate_by_key[candidate_key] = candidate_payload
        return sorted(
            candidate_by_key.values(),
            key=lambda item: (float(item.get("confidence") or 0), str(item.get("priceDate") or "")),
            reverse=True,
        )[:10]

    mappings_by_product: dict[int, CompetitorCodeMapping] = {}
    rejected_keys: set[str] = set()
    if keys:
        mapping_rows = db.execute(
            select(CompetitorCodeMapping)
            .where(CompetitorCodeMapping.platform == platform)
            .where(CompetitorCodeMapping.source_match_key.in_(list(keys)))
        ).scalars().all()
        for row in mapping_rows:
            if row.status == "mapped" and row.our_product_id:
                mappings_by_product.setdefault(int(row.our_product_id), row)
            elif row.status == "rejected":
                rejected_keys.add(row.source_match_key)

    generated_coverage = _generated_pricing_coverage(db, price_format_id)
    metrics = {
        "platform": platform,
        "total": 0,
        "mapped": 0,
        "unmapped": 0,
        "rejected": 0,
        "noCandidates": 0,
        "coveragePercent": 0,
        "mappingCoveragePercent": 0,
        "generatedPricingCoverage": generated_coverage,
    }
    rows: list[dict] = []
    product_search = product_q.strip().casefold()
    for product, extra in product_rows:
        product_id = int(product.id)
        manual = mappings_by_product.get(product_id)
        product_items = list(items_by_product.get(product_id, []))
        existing_matched_item = next((item for item in product_items if item.get("matchedSku") == product.code), None)
        has_primary_goods_mapping = platform == "provisor" and product.provisor_goods_id is not None
        candidate_pool = [] if (manual or has_primary_goods_mapping or existing_matched_item) else candidate_pool_for_product(product)
        candidates = (
            score_candidates_for_product(product, extra, candidate_pool)
            if candidate_pool and classify_candidates_during_scan
            else []
        )

        mapped_item = None
        mapping_id = None
        row_status = "unmapped"
        if manual is not None:
            mapped_item = payload_by_match_key.get(manual.source_match_key)
            mapped_item = mapped_item or {
                "sourceExternalKey": manual.source_external_key,
                "sourceMatchKey": manual.source_match_key,
                "sourceName": manual.source_name,
                "sourceManufacturer": manual.source_manufacturer,
                "sourceDosageForm": manual.source_dosage_form,
                "sourceNormalizedName": manual.source_normalized_name,
                "confidence": float(manual.confidence) if manual.confidence is not None else None,
                "platform": platform,
            }
            mapping_id = manual.id
            row_status = "mapped"
        elif has_primary_goods_mapping:
            # Provisor goodsId is the canonical durable product mapping.  The
            # latest price list is only a candidate/price source and must not
            # make an already mapped catalog product appear as unmatched.
            mapped_item = next(iter(payloads_by_external_key.get(str(product.provisor_goods_id), [])), None)
            mapped_item = mapped_item or {
                "sourceExternalKey": str(product.provisor_goods_id),
                "sourceMatchKey": source_match_key(platform=platform, source_external_key=product.provisor_goods_id),
                "matchType": "provisor_goods_id",
                "matchedSku": product.code,
                "platform": platform,
            }
            row_status = "mapped"
        elif existing_matched_item is not None:
            mapped_item = existing_matched_item
            row_status = "mapped"
        elif classify_candidates_during_scan and candidates:
            matched_candidate = next((item for item in candidates if item.get("matchedSku") == product.code), None)
            mapped_item = matched_candidate or candidates[0]
            if mapped_item.get("sourceMatchKey") in rejected_keys:
                row_status = "rejected"
            else:
                row_status = "mapped" if matched_candidate is not None else "unmapped"
        elif candidate_pool:
            row_status = "unmapped"
        else:
            row_status = "no_candidates"

        metrics["total"] += 1
        if row_status == "mapped":
            metrics["mapped"] += 1
        elif row_status == "rejected":
            metrics["rejected"] += 1
        elif row_status == "no_candidates":
            metrics["noCandidates"] += 1
        else:
            metrics["unmapped"] += 1

        if status == "unmapped" and row_status not in {"unmapped", "no_candidates"}:
            continue
        if status not in {"all", "unmapped"} and row_status != status:
            continue
        if product_search and not any(
            product_search in str(value or "").casefold()
            for value in (product.code, product.name, extra.manufacturer if extra else "")
        ):
            continue
        if source_search_requested and not candidates and manual is None:
            continue

        source = mapped_item or {}
        rows.append(
                {
                    "productId": product_id,
                    "ourProductId": product_id,
                    "ourSku": product.code,
                    "ourName": product.name,
                    "ourManufacturer": (extra.manufacturer if extra else "") or "",
                    "platform": platform,
                    "status": "unmapped" if row_status == "no_candidates" else row_status,
                    "mappingStatus": row_status,
                    "mappingId": mapping_id,
                    "candidatesCount": len(candidates) if classify_candidates_during_scan else len(candidate_pool),
                    "candidates": candidates,
                    "bestCandidate": candidates[0] if candidates else None,
                    "confidence": source.get("confidence"),
                    "itemId": source.get("itemId"),
                    "priceListId": source.get("priceListId"),
                    "priceListName": source.get("priceListName") or "",
                    "matchType": source.get("matchType") or "",
                    "matchedSku": source.get("matchedSku") or "",
                    "sourcePrice": source.get("sourcePrice"),
                    "priceDate": source.get("priceDate") or "",
                    "sourceExternalKey": source.get("sourceExternalKey"),
                    "sourceMatchKey": source.get("sourceMatchKey") or "",
                    "sourceName": source.get("sourceName") or "",
                    "sourceManufacturer": source.get("sourceManufacturer") or "",
                    "sourceDosageForm": source.get("sourceDosageForm") or "",
                    "sourceNormalizedName": source.get("sourceNormalizedName") or "",
                }
            )

    metrics["coveragePercent"] = round((metrics["mapped"] / metrics["total"]) * 100, 2) if metrics["total"] else 0
    metrics["mappingCoveragePercent"] = metrics["coveragePercent"]
    filtered_total = len(rows)
    page_count = (filtered_total + limit - 1) // limit if filtered_total else 0
    if page_count and page > page_count:
        page = page_count
    start = (page - 1) * limit
    page_rows = rows[start : start + limit]
    if include_candidates:
        for row in page_rows:
            if row.get("mappingStatus") not in {"unmapped", "no_candidates"}:
                continue
            product, extra = products_by_id.get(int(row["productId"]), (None, None))
            if product is None:
                continue
            candidates = score_candidates_for_product(product, extra, candidate_pool_for_product(product))
            row["candidates"] = candidates
            row["candidatesCount"] = len(candidates)
            row["bestCandidate"] = candidates[0] if candidates else None
            if candidates:
                source = candidates[0]
                row["mappingStatus"] = "unmapped"
                row["status"] = "unmapped"
                row["confidence"] = source.get("confidence")
                row["itemId"] = source.get("itemId")
                row["priceListId"] = source.get("priceListId")
                row["priceListName"] = source.get("priceListName") or ""
                row["matchType"] = source.get("matchType") or ""
                row["matchedSku"] = source.get("matchedSku") or ""
                row["sourcePrice"] = source.get("sourcePrice")
                row["priceDate"] = source.get("priceDate") or ""
                row["sourceExternalKey"] = source.get("sourceExternalKey")
                row["sourceMatchKey"] = source.get("sourceMatchKey") or ""
                row["sourceName"] = source.get("sourceName") or ""
                row["sourceManufacturer"] = source.get("sourceManufacturer") or ""
                row["sourceDosageForm"] = source.get("sourceDosageForm") or ""
                row["sourceNormalizedName"] = source.get("sourceNormalizedName") or ""
            else:
                row["mappingStatus"] = "no_candidates"
                row["status"] = "unmapped"
    return {
        "items": page_rows,
        "metrics": [metrics],
        "pagination": {
            "page": page,
            "pageSize": limit,
            "total": filtered_total,
            "pageCount": page_count,
        },
    }


def list_code_mappings(
    *,
    db: Session,
    platform: str,
    price_format_id: int | None = None,
    status: str = "all",
    source_q: str = "",
    product_q: str = "",
    limit: int = 200,
) -> dict:
    platform = platform_from_value(platform)
    status = status if status in {"all", "mapped", "unmapped", "rejected"} else "all"
    limit = max(1, min(int(limit or 200), 1000))
    stmt = (
        select(CompetitorPriceListItem, CompetitorPriceList)
        .join(CompetitorPriceList, CompetitorPriceList.id == CompetitorPriceListItem.price_list_id)
        .where(CompetitorPriceList.source_type == platform)
        .order_by(CompetitorPriceListItem.id.desc())
        .limit(limit * 5)
    )
    if price_format_id is not None:
        assigned_ids = [int(item.price_list.id) for item in get_assigned_competitor_price_lists(db=db, price_format_id=price_format_id)]
        stmt = stmt.where(CompetitorPriceList.id.in_(assigned_ids))
    source_like = f"%{source_q.strip()}%" if source_q.strip() else ""
    if source_like:
        stmt = stmt.where(
            (CompetitorPriceListItem.name.ilike(source_like))
            | (CompetitorPriceListItem.raw_name.ilike(source_like))
            | (CompetitorPriceListItem.distributor_goods_name.ilike(source_like))
            | (CompetitorPriceListItem.raw_manufacturer.ilike(source_like))
            | (CompetitorPriceListItem.distributor_goods_id.ilike(source_like))
        )
    item_rows = db.execute(stmt).all()
    keys = [source_match_key_for_item(platform, item) for item, _ in item_rows]
    mappings_by_key = {}
    if keys:
        mappings_by_key = {
            row.source_match_key: row
            for row in db.execute(
                select(CompetitorCodeMapping)
                .where(CompetitorCodeMapping.platform == platform)
                .where(CompetitorCodeMapping.source_match_key.in_(keys))
            ).scalars().all()
        }
    product_ids = [row.our_product_id for row in mappings_by_key.values() if row.our_product_id]
    product_ids.extend([item.product_id for item, _ in item_rows if item.product_id])
    products = {}
    extras = {}
    if product_ids:
        for product, extra in db.execute(
            select(Product, ProductExtra)
            .join(ProductExtra, ProductExtra.product_id == Product.id, isouter=True)
            .where(Product.id.in_(list({int(x) for x in product_ids if x})))
        ).all():
            products[int(product.id)] = product
            extras[int(product.id)] = extra
    out: list[dict] = []
    seen_keys: set[str] = set()
    product_search = product_q.strip().casefold()
    for item, price_list in item_rows:
        key = source_match_key_for_item(platform, item)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        mapping = mappings_by_key.get(key)
        product_id = mapping.our_product_id if mapping and mapping.our_product_id else item.product_id
        product = products.get(int(product_id)) if product_id else None
        extra = extras.get(int(product_id)) if product_id else None
        item_status = _status_for_item(item, mapping)
        if status != "all" and item_status != status:
            continue
        if product_search and not any(
            product_search in str(value or "").casefold()
            for value in [product.code if product else "", product.name if product else "", extra.manufacturer if extra else ""]
        ):
            continue
        source_payload = mapping_source_payload(platform, item)
        out.append(
            {
                "itemId": item.id,
                "priceListId": price_list.id,
                "priceListName": price_list.display_name or price_list.supplier or price_list.source_key,
                "platform": platform,
                "status": item_status,
                "mappingId": mapping.id if mapping else None,
                "matchType": item.match_type or "",
                "matchedSku": item.matched_sku or "",
                "sourcePrice": float(item.distributor_price) if item.distributor_price is not None else None,
                **{
                    "sourceExternalKey": source_payload["source_external_key"],
                    "sourceMatchKey": source_payload["source_match_key"],
                    "sourceName": source_payload["source_name"],
                    "sourceManufacturer": source_payload["source_manufacturer"],
                    "sourceDosageForm": source_payload["source_dosage_form"],
                    "sourceNormalizedName": source_payload["source_normalized_name"],
                },
                **_product_payload(product, extra),
            }
        )
        if len(out) >= limit:
            break
    metrics = [_coverage_for_platform(db, item, price_format_id) for item in ("provisor", "vidman")]
    return {"items": out, "metrics": metrics}


def upsert_code_mapping(
    *,
    db: Session,
    platform: str,
    product: Product | None,
    source_payload: dict,
    status: str = "mapped",
    confidence: float | None = 100,
    created_by: str = "",
) -> CompetitorCodeMapping:
    platform = platform_from_value(platform)
    status = status if status in {"mapped", "unmapped", "rejected"} else "mapped"
    key = source_payload.get("source_match_key") or source_match_key(
        platform=platform,
        source_external_key=source_payload.get("source_external_key"),
        source_name=source_payload.get("source_name"),
        source_manufacturer=source_payload.get("source_manufacturer"),
    )
    row = (
        db.execute(
            select(CompetitorCodeMapping)
            .where(CompetitorCodeMapping.platform == platform)
            .where(CompetitorCodeMapping.source_match_key == str(key))
        )
        .scalars()
        .first()
    )
    if row is None:
        row = CompetitorCodeMapping(platform=platform, source_match_key=str(key), created_at=datetime.utcnow())
        db.add(row)
    row.source_external_key = source_payload.get("source_external_key")
    row.source_match_key = str(key)
    row.source_name = str(source_payload.get("source_name") or "")
    row.source_manufacturer = str(source_payload.get("source_manufacturer") or "")
    row.source_dosage_form = str(source_payload.get("source_dosage_form") or "")
    row.source_normalized_name = str(source_payload.get("source_normalized_name") or normalize_mapping_text(row.source_name))
    row.our_product_id = product.id if product and status == "mapped" else None
    row.our_sku = product.code if product and status == "mapped" else ""
    row.status = status
    row.confidence = Decimal(str(confidence)) if confidence is not None and status == "mapped" else None
    row.approved_at = datetime.utcnow() if status == "mapped" else None
    row.updated_at = datetime.utcnow()
    row.created_by = created_by
    return row


def apply_mapping_to_matching_items(
    *,
    db: Session,
    mapping: CompetitorCodeMapping,
    product: Product | None,
    clear: bool = False,
) -> int:
    stmt = (
        select(CompetitorPriceListItem)
        .join(CompetitorPriceList, CompetitorPriceList.id == CompetitorPriceListItem.price_list_id)
        .where(CompetitorPriceList.source_type == mapping.platform)
    )
    touched = 0
    for item in db.execute(stmt).scalars().all():
        if source_match_key_for_item(mapping.platform, item) != mapping.source_match_key:
            continue
        if clear or product is None or mapping.status != "mapped":
            item.product_id = None
            item.matched_sku = ""
            item.match_type = "manual_rejected" if mapping.status == "rejected" else "unmatched"
            item.match_score = None
        else:
            item.product_id = product.id
            item.matched_sku = product.code
            item.match_type = "manual_code_mapping"
            item.match_score = mapping.confidence or 100
        touched += 1
    return touched


def apply_manual_mappings_to_items(*, db: Session, price_list: CompetitorPriceList, items: list[CompetitorPriceListItem]) -> dict:
    if price_list.source_type not in SUPPORTED_PLATFORMS:
        return {"applied": 0, "rejected": 0, "productIds": set(), "itemIds": set()}
    keys = [source_match_key_for_item(price_list.source_type, item) for item in items]
    if not keys:
        return {"applied": 0, "rejected": 0, "productIds": set(), "itemIds": set()}
    mappings = {
        row.source_match_key: row
        for row in db.execute(
            select(CompetitorCodeMapping)
            .where(CompetitorCodeMapping.platform == price_list.source_type)
            .where(CompetitorCodeMapping.source_match_key.in_(list(set(keys))))
        ).scalars().all()
    }
    products = {}
    product_ids = [row.our_product_id for row in mappings.values() if row.status == "mapped" and row.our_product_id]
    if product_ids:
        products = {
            int(row.id): row
            for row in db.execute(select(Product).where(Product.id.in_(list(set(product_ids))))).scalars().all()
        }
    applied = 0
    rejected = 0
    product_ids_applied: set[int] = set()
    item_ids_applied: set[int] = set()
    for item in items:
        mapping = mappings.get(source_match_key_for_item(price_list.source_type, item))
        if mapping is None:
            continue
        if mapping.status == "rejected":
            item.product_id = None
            item.matched_sku = ""
            item.match_type = "manual_rejected"
            item.match_score = None
            rejected += 1
            item_ids_applied.add(int(item.id))
            continue
        if mapping.status != "mapped" or not mapping.our_product_id:
            continue
        product = products.get(int(mapping.our_product_id))
        if product is None:
            continue
        item.product_id = product.id
        item.matched_sku = product.code
        item.match_type = "manual_code_mapping"
        item.match_score = mapping.confidence or 100
        applied += 1
        product_ids_applied.add(int(product.id))
        item_ids_applied.add(int(item.id))
    return {"applied": applied, "rejected": rejected, "productIds": product_ids_applied, "itemIds": item_ids_applied}


def find_products_for_mapping(*, db: Session, q: str, limit: int = 30) -> list[dict]:
    query = q.strip()
    if not query:
        return []
    like = f"%{query}%"
    rows = (
        db.execute(
            select(Product, ProductExtra)
            .join(ProductExtra, ProductExtra.product_id == Product.id, isouter=True)
            .where((Product.code.ilike(like)) | (Product.name.ilike(like)) | (ProductExtra.manufacturer.ilike(like)))
            .order_by(Product.code.asc())
            .limit(max(1, min(limit, 100)))
        )
        .all()
    )
    return [
        {
            "productId": product.id,
            "sku": product.code,
            "name": product.name,
            "manufacturer": (extra.manufacturer if extra else "") or "",
        }
        for product, extra in rows
    ]
