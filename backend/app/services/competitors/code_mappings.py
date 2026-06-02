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
    limit: int = 300,
) -> dict:
    platform = platform_from_value(platform)
    status = status if status in {"all", "mapped", "unmapped", "rejected", "no_candidates"} else "all"
    limit = max(1, min(int(limit or 300), 1000))

    product_like = f"%{product_q.strip()}%" if product_q.strip() else ""
    product_stmt = (
        select(Product, ProductExtra)
        .join(ProductExtra, ProductExtra.product_id == Product.id, isouter=True)
        .order_by(Product.code.asc())
    )
    if product_like:
        product_stmt = product_stmt.where(
            (Product.code.ilike(product_like))
            | (Product.name.ilike(product_like))
            | (ProductExtra.manufacturer.ilike(product_like))
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
    items_by_norm_name: dict[str, list[dict]] = {}
    keys: set[str] = set()
    for item, price_list in item_rows:
        payload = _source_item_to_payload(platform, item, price_list)
        item_payloads.append(payload)
        keys.add(str(payload["sourceMatchKey"] or ""))
        product_id = int(item.product_id) if item.product_id else None
        if product_id is None and item.matched_sku:
            product_id = products_by_sku.get(item.matched_sku)
        if product_id:
            items_by_product.setdefault(product_id, []).append(payload)
        norm_name = normalize_mapping_text(payload.get("sourceName"))
        if norm_name:
            items_by_norm_name.setdefault(norm_name, []).append(payload)

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
    for product, extra in product_rows:
        product_id = int(product.id)
        manual = mappings_by_product.get(product_id)
        candidates = list(items_by_product.get(product_id, []))
        norm_product_name = normalize_mapping_text(product.name)
        for candidate in items_by_norm_name.get(norm_product_name, []):
            if candidate not in candidates:
                candidates.append(candidate)
        candidates = candidates[:10]

        mapped_item = None
        mapping_id = None
        row_status = "unmapped"
        if manual is not None:
            mapped_item = next((item for item in item_payloads if item.get("sourceMatchKey") == manual.source_match_key), None)
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
        elif candidates:
            mapped_item = candidates[0]
            if mapped_item.get("sourceMatchKey") in rejected_keys:
                row_status = "rejected"
            else:
                row_status = "mapped" if mapped_item.get("matchedSku") == product.code else "unmapped"
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

        if status != "all" and row_status != status:
            continue
        if source_q.strip() and not candidates and manual is None:
            continue

        source = mapped_item or {}
        if len(rows) < limit:
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
                    "candidatesCount": len(candidates),
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
    return {"items": rows, "metrics": [metrics]}


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
