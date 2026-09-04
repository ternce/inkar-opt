from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy import String, case, cast, desc, exists, func, literal, or_, select
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
from ..competitor_read_models import refresh_price_list_item_counters

SUPPORTED_PLATFORMS = {"provisor", "vidman"}
PRODUCT_CATALOG_CANDIDATE_LIMIT = 5
MANUAL_SUGGESTION_MIN_SCORE = 55.0
MANUAL_CANDIDATE_POOL_LIMIT = 250
MANUAL_CANDIDATE_LIMIT = 10


STRUCTURED_MANUAL_FIELDS = (
    "dosage",
    "dosage_volume",
    "strength_signature",
    "concentration",
    "percent_strength",
    "iu_dosage",
    "volume",
    "weight",
    "quantity",
    "form",
    "dimensions",
    "critical_tokens",
)


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


def _manual_characteristic_values(structure) -> dict:
    return {
        "baseName": structure.base_name,
        "dosage": structure.dosage,
        "dosageVolume": structure.dosage_volume,
        "strengthSignature": list(structure.strength_signature or ()),
        "concentration": structure.concentration,
        "percentStrength": structure.percent_strength,
        "iuDosage": structure.iu_dosage,
        "volume": structure.volume,
        "weight": structure.weight,
        "quantity": structure.quantity,
        "form": structure.form,
        "forms": list(structure.forms or ()),
        "dimensions": list(structure.dimensions or ()),
        "criticalTokens": list(structure.critical_tokens or ()),
    }


def _manual_field_value(structure, field: str):
    if field == "form":
        return set(structure.forms or ((structure.form,) if structure.form else ()))
    return getattr(structure, field, None)


def _manual_values_match(left: object, right: object) -> bool:
    if isinstance(left, set) or isinstance(right, set):
        left_set = set(left or ())
        right_set = set(right or ())
        return bool(left_set and right_set and not left_set.isdisjoint(right_set))
    if isinstance(left, tuple) or isinstance(right, tuple):
        return tuple(left or ()) == tuple(right or ())
    return _same_manual_value(left, right)


def _manual_structured_match(source_structure, product_structure) -> tuple[bool, str | None, int]:
    matched = 0
    for field in STRUCTURED_MANUAL_FIELDS:
        source_value = _manual_field_value(source_structure, field)
        product_value = _manual_field_value(product_structure, field)
        source_has = bool(source_value) if field == "form" else source_value is not None
        product_has = bool(product_value) if field == "form" else product_value is not None
        if source_has and not product_has:
            return False, f"missing_internal_{field}", matched
        if source_has and product_has:
            if not _manual_values_match(source_value, product_value):
                return False, f"{field}_conflict", matched
            matched += 1
    return True, None, matched


def _manual_candidate_level(
    *,
    source_name: str,
    source_manufacturer: str,
    product_name: str,
    product_manufacturer: str,
) -> tuple[str, bool, float, dict] | None:
    from ..competitor_matching import _base_name_similarity, _manufacturer_match, normalize_manufacturer_text, parse_drug_structure

    source_structure = parse_drug_structure(source_name)
    product_structure = parse_drug_structure(product_name)
    source_base = source_structure.base_name or normalize_mapping_text(source_name)
    product_base = product_structure.base_name or normalize_mapping_text(product_name)
    name_score = _base_name_similarity(source_base, product_base)
    if name_score < 97:
        return None

    structured_ok, reject_reason, matched_fields = _manual_structured_match(source_structure, product_structure)
    if not structured_ok:
        return None

    source_manufacturer_norm = normalize_manufacturer_text(source_manufacturer)
    product_manufacturer_norm = normalize_manufacturer_text(product_manufacturer)
    manufacturers_match = bool(
        source_manufacturer_norm
        and product_manufacturer_norm
        and _manufacturer_match(source_manufacturer_norm, product_manufacturer_norm)
    )
    manufacturer_mismatch = bool(source_manufacturer_norm and product_manufacturer_norm and not manufacturers_match)
    if manufacturers_match:
        match_level = "exact"
        confidence = 100.0
    elif manufacturer_mismatch:
        match_level = "characteristics"
        confidence = 92.0
    else:
        return None

    return match_level, manufacturer_mismatch, confidence, {
        "nameScore": round(name_score, 2),
        "matchedFields": matched_fields,
        "rejectReason": reject_reason,
        "sourceCharacteristics": _manual_characteristic_values(source_structure),
        "internalCharacteristics": _manual_characteristic_values(product_structure),
        "sourceManufacturerNormalized": source_manufacturer_norm,
        "internalManufacturerNormalized": product_manufacturer_norm,
    }


def _manual_candidates_for_source(db: Session, source: dict, limit: int = MANUAL_CANDIDATE_LIMIT) -> list[dict]:
    from ..competitor_matching import parse_drug_structure

    source_name = str(source.get("sourceName") or "")
    source_structure = parse_drug_structure(source_name)
    source_base = source_structure.base_name or normalize_mapping_text(source_name)
    source_tokens = [token for token in source_base.split() if len(token) >= 3 and not token.isdigit()]
    source_tokens.extend(token for token in str(source_name).split() if len(token) >= 3 and not token.isdigit())
    source_tokens.extend(token for token in normalize_mapping_text(source_name).split() if len(token) >= 3 and not token.isdigit())
    source_tokens = list(dict.fromkeys(source_tokens))
    if not source_tokens:
        return []

    name_filter = None
    for token in source_tokens[:3]:
        condition = Product.name.ilike(f"%{token}%")
        name_filter = condition if name_filter is None else name_filter | condition
    product_rows = (
        db.execute(
            select(Product, ProductExtra)
            .join(ProductExtra, ProductExtra.product_id == Product.id, isouter=True)
            .where(name_filter)
            .order_by(Product.code.asc())
            .limit(MANUAL_CANDIDATE_POOL_LIMIT)
        )
        .all()
    )

    candidate_by_product: dict[int, dict] = {}
    for product, extra in product_rows:
        product_manufacturer = (extra.manufacturer if extra else "") or ""
        level = _manual_candidate_level(
            source_name=source_name,
            source_manufacturer=str(source.get("sourceManufacturer") or ""),
            product_name=product.name,
            product_manufacturer=product_manufacturer,
        )
        if level is None:
            continue
        match_level, manufacturer_mismatch, confidence, details = level
        product_id = int(product.id)
        candidate_by_product[product_id] = {
            **source,
            "ourProductId": product_id,
            "productId": product_id,
            "ourSku": product.code,
            "ourName": product.name,
            "ourManufacturer": product_manufacturer,
            "confidence": confidence,
            "matchType": f"manual_{match_level}_suggestion",
            "matchLevel": match_level,
            "manufacturerMismatch": manufacturer_mismatch,
            "sourceManufacturer": str(source.get("sourceManufacturer") or ""),
            "internalManufacturer": product_manufacturer,
            "manualSuggestion": details,
        }

    level_rank = {"exact": 0, "characteristics": 1}
    return sorted(
        candidate_by_product.values(),
        key=lambda item: (
            level_rank.get(str(item.get("matchLevel") or ""), 9),
            -float(item.get("confidence") or 0),
            str(item.get("ourSku") or ""),
            int(item.get("ourProductId") or 0),
        ),
    )[: max(1, min(int(limit or MANUAL_CANDIDATE_LIMIT), MANUAL_CANDIDATE_LIMIT))]


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


def _provisor_source_key_expr(goods_id_col):
    return literal("provisor:") + cast(goods_id_col, String)


def _provisor_catalog_status_exists(base) -> tuple:
    mapped_mapping_exists = exists(
        select(1)
        .select_from(CompetitorCodeMapping)
        .where(CompetitorCodeMapping.platform == "provisor")
        .where(CompetitorCodeMapping.status == "mapped")
        .where(CompetitorCodeMapping.source_match_key == base.c.source_match_key)
        .correlate(base)
    )
    rejected_mapping_exists = exists(
        select(1)
        .select_from(CompetitorCodeMapping)
        .where(CompetitorCodeMapping.platform == "provisor")
        .where(CompetitorCodeMapping.status == "rejected")
        .where(CompetitorCodeMapping.source_match_key == base.c.source_match_key)
        .correlate(base)
    )
    return mapped_mapping_exists, rejected_mapping_exists


def _provisor_catalog_base(
    *,
    assigned_ids: list[int] | None,
    source_q: str = "",
    goods_ids: list[int] | None = None,
) -> object:
    source_key = _provisor_source_key_expr(CompetitorPriceListItem.provisor_goods_id).label("source_match_key")
    stmt = (
        select(
            CompetitorPriceListItem.id.label("item_id"),
            CompetitorPriceListItem.price_list_id.label("price_list_id"),
            CompetitorPriceListItem.product_id.label("product_id"),
            CompetitorPriceListItem.provisor_goods_id.label("provisor_goods_id"),
            CompetitorPriceListItem.distributor_goods_id.label("distributor_goods_id"),
            CompetitorPriceListItem.name.label("name"),
            CompetitorPriceListItem.raw_name.label("raw_name"),
            CompetitorPriceListItem.raw_manufacturer.label("raw_manufacturer"),
            CompetitorPriceListItem.normalized_name.label("normalized_name"),
            CompetitorPriceListItem.parsed_form.label("parsed_form"),
            CompetitorPriceListItem.match_type.label("match_type"),
            CompetitorPriceListItem.matched_sku.label("matched_sku"),
            CompetitorPriceListItem.distributor_price.label("source_price"),
            CompetitorPriceListItem.match_score.label("confidence"),
            CompetitorPriceList.display_name.label("price_list_name"),
            CompetitorPriceList.supplier.label("supplier"),
            CompetitorPriceList.source_key.label("price_list_source_key"),
            CompetitorPriceList.price_date.label("price_date"),
            source_key,
            func.row_number()
            .over(
                partition_by=CompetitorPriceListItem.provisor_goods_id,
                order_by=(
                    desc(CompetitorPriceList.price_date),
                    desc(CompetitorPriceList.updated_at),
                    desc(CompetitorPriceListItem.id),
                ),
            )
            .label("rn"),
        )
        .select_from(CompetitorPriceListItem)
        .join(CompetitorPriceList, CompetitorPriceList.id == CompetitorPriceListItem.price_list_id)
        .where(CompetitorPriceList.source_type == "provisor")
        .where(CompetitorPriceListItem.provisor_goods_id.is_not(None))
    )
    if assigned_ids is not None:
        stmt = stmt.where(CompetitorPriceList.id.in_(assigned_ids))
    if goods_ids is not None:
        stmt = stmt.where(CompetitorPriceListItem.provisor_goods_id.in_(goods_ids))
    search = source_q.strip()
    if search:
        if search.isdigit():
            stmt = stmt.where(CompetitorPriceListItem.provisor_goods_id == int(search))
        else:
            like = f"%{search}%"
            stmt = stmt.where(
                or_(
                    CompetitorPriceListItem.name.ilike(like),
                    CompetitorPriceListItem.raw_name.ilike(like),
                    CompetitorPriceListItem.distributor_goods_name.ilike(like),
                    CompetitorPriceListItem.raw_manufacturer.ilike(like),
                    CompetitorPriceListItem.distributor_goods_id.ilike(like),
                )
            )
    return stmt.subquery()


def _provisor_goods_base(
    *,
    assigned_ids: list[int] | None,
    source_q: str = "",
) -> object:
    source_key = _provisor_source_key_expr(CompetitorPriceListItem.provisor_goods_id).label("source_match_key")
    stmt = (
        select(
            CompetitorPriceListItem.provisor_goods_id.label("provisor_goods_id"),
            source_key,
        )
        .select_from(CompetitorPriceListItem)
        .join(CompetitorPriceList, CompetitorPriceList.id == CompetitorPriceListItem.price_list_id)
        .where(CompetitorPriceList.source_type == "provisor")
        .where(CompetitorPriceListItem.provisor_goods_id.is_not(None))
    )
    if assigned_ids is not None:
        stmt = stmt.where(CompetitorPriceList.id.in_(assigned_ids))
    search = source_q.strip()
    if search:
        if search.isdigit():
            stmt = stmt.where(CompetitorPriceListItem.provisor_goods_id == int(search))
        else:
            like = f"%{search}%"
            stmt = stmt.where(
                or_(
                    CompetitorPriceListItem.name.ilike(like),
                    CompetitorPriceListItem.raw_name.ilike(like),
                    CompetitorPriceListItem.distributor_goods_name.ilike(like),
                    CompetitorPriceListItem.raw_manufacturer.ilike(like),
                    CompetitorPriceListItem.distributor_goods_id.ilike(like),
                )
            )
    return stmt.group_by(CompetitorPriceListItem.provisor_goods_id, source_key).subquery()


def _apply_provisor_catalog_filters(stmt, base, status: str, product_q: str):
    mapped_mapping_exists, rejected_mapping_exists = _provisor_catalog_status_exists(base)
    if status == "unmapped":
        stmt = stmt.where(~mapped_mapping_exists).where(~rejected_mapping_exists)
    elif status == "mapped":
        stmt = stmt.where(~rejected_mapping_exists).where(mapped_mapping_exists)
    elif status == "rejected":
        stmt = stmt.where(rejected_mapping_exists)
    product_search = product_q.strip()
    if product_search:
        like = f"%{product_search}%"
        stmt = stmt.where(
            or_(
                Product.code.ilike(like),
                Product.name.ilike(like),
                ProductExtra.manufacturer.ilike(like),
            )
        )
    return stmt


def _provisor_metrics_counts(db: Session, assigned_ids: list[int] | None) -> tuple[int, int, int]:
    base = _provisor_goods_base(assigned_ids=assigned_ids)
    mapping_join = (
        (CompetitorCodeMapping.platform == "provisor")
        & (CompetitorCodeMapping.source_match_key == base.c.source_match_key)
        & (CompetitorCodeMapping.status.in_(["mapped", "rejected"]))
    )
    row = db.execute(
        select(
            func.count(func.distinct(base.c.provisor_goods_id)).label("total"),
            func.count(func.distinct(case((CompetitorCodeMapping.status == "mapped", base.c.provisor_goods_id)))).label("mapped"),
            func.count(func.distinct(case((CompetitorCodeMapping.status == "rejected", base.c.provisor_goods_id)))).label("rejected"),
        )
        .select_from(base)
        .outerjoin(CompetitorCodeMapping, mapping_join)
    ).one()
    return int(row.total or 0), int(row.mapped or 0), int(row.rejected or 0)


def _list_provisor_catalog_code_mappings_sql_page(
    *,
    db: Session,
    price_format_id: int | None,
    assigned_ids: list[int] | None,
    status: str,
    source_q: str,
    product_q: str,
    page: int,
    limit: int,
    include_candidates: bool,
) -> dict:
    if assigned_ids == []:
        return {
            "items": [],
            "metrics": [
                {
                    "platform": "provisor",
                    "total": 0,
                    "mapped": 0,
                    "unmapped": 0,
                    "rejected": 0,
                    "noCandidates": 0,
                    "coveragePercent": 0,
                    "mappingCoveragePercent": 0,
                    "generatedPricingCoverage": _generated_pricing_coverage(db, price_format_id),
                }
            ],
            "pagination": {"page": 1, "pageSize": limit, "total": 0, "pageCount": 0},
        }

    total, mapped, rejected = _provisor_metrics_counts(db, assigned_ids)
    unmapped = max(0, total - mapped - rejected)
    metrics = {
        "platform": "provisor",
        "total": total,
        "mapped": mapped,
        "unmapped": unmapped,
        "rejected": rejected,
        "noCandidates": 0,
        "coveragePercent": round((mapped / total) * 100, 2) if total else 0,
        "mappingCoveragePercent": round((mapped / total) * 100, 2) if total else 0,
        "generatedPricingCoverage": _generated_pricing_coverage(db, price_format_id),
    }

    goods_base = _provisor_goods_base(assigned_ids=assigned_ids, source_q=source_q)
    mapping_join = (
        (CompetitorCodeMapping.platform == "provisor")
        & (CompetitorCodeMapping.source_match_key == goods_base.c.source_match_key)
        & (CompetitorCodeMapping.status.in_(["mapped", "rejected"]))
    )
    goods_stmt = (
        select(goods_base.c.provisor_goods_id)
        .select_from(goods_base)
        .outerjoin(CompetitorCodeMapping, mapping_join)
        .outerjoin(Product, Product.id == CompetitorCodeMapping.our_product_id)
        .outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
    )
    goods_stmt = _apply_provisor_catalog_filters(goods_stmt, goods_base, status, product_q)

    if not source_q.strip() and not product_q.strip():
        filtered_total = {
            "all": total,
            "mapped": mapped,
            "rejected": rejected,
            "unmapped": unmapped,
            "no_candidates": unmapped,
        }.get(status, total)
    else:
        count_stmt = select(func.count()).select_from(goods_stmt.order_by(None).subquery())
        filtered_total = int(db.scalar(count_stmt) or 0)

    page_count = (filtered_total + limit - 1) // limit if filtered_total else 0
    if page_count and page > page_count:
        page = page_count

    page_goods_ids = [
        int(goods_id)
        for goods_id in db.execute(
            goods_stmt.distinct().order_by(goods_base.c.provisor_goods_id.asc()).limit(limit).offset((page - 1) * limit)
        ).scalars().all()
    ]
    if not page_goods_ids:
        return {
            "items": [],
            "metrics": [metrics],
            "pagination": {
                "page": 1 if not filtered_total else page,
                "pageSize": limit,
                "total": filtered_total,
                "pageCount": page_count,
            },
        }

    base = _provisor_catalog_base(assigned_ids=assigned_ids, goods_ids=page_goods_ids)
    row_mapping_join = (
        (CompetitorCodeMapping.platform == "provisor")
        & (CompetitorCodeMapping.source_match_key == base.c.source_match_key)
        & (CompetitorCodeMapping.status.in_(["mapped", "rejected"]))
    )
    row_stmt = (
        select(
            base,
            CompetitorCodeMapping.id.label("mapping_id"),
            CompetitorCodeMapping.status.label("manual_status"),
            CompetitorCodeMapping.confidence.label("manual_confidence"),
            Product.id.label("our_product_id"),
            Product.code.label("our_sku"),
            Product.name.label("our_name"),
            ProductExtra.manufacturer.label("our_manufacturer"),
        )
        .outerjoin(CompetitorCodeMapping, row_mapping_join)
        .outerjoin(Product, Product.id == CompetitorCodeMapping.our_product_id)
        .outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
        .where(base.c.rn == 1)
    )

    rows = db.execute(
        row_stmt.order_by(base.c.provisor_goods_id.asc(), base.c.item_id.desc())
    ).all()

    items = []
    for row in rows:
        data = row._mapping
        source_name = data["raw_name"] or data["name"] or ""
        source_manufacturer = data["raw_manufacturer"] or ""
        source_external_key = str(data["provisor_goods_id"])
        mapping_status = "unmapped"
        if data["manual_status"] == "rejected":
            mapping_status = "rejected"
        elif data["manual_status"] == "mapped":
            mapping_status = "mapped"
        item_payload = {
                "itemId": int(data["item_id"]),
                "priceListId": int(data["price_list_id"]),
                "priceListName": data["price_list_name"] or data["supplier"] or data["price_list_source_key"] or "",
                "platform": "provisor",
                "status": mapping_status,
                "mappingStatus": mapping_status,
                "mappingId": data["mapping_id"],
                "matchType": (data["manual_status"] if data["manual_status"] else data["match_type"]) or "",
                "matchedSku": data["matched_sku"] or "",
                "sourcePrice": float(data["source_price"]) if data["source_price"] is not None else None,
                "priceDate": data["price_date"].isoformat() if data["price_date"] else "",
                "confidence": (
                    float(data["manual_confidence"])
                    if data["manual_confidence"] is not None
                    else float(data["confidence"])
                    if data["confidence"] is not None
                    else None
                ),
                "sourceExternalKey": source_external_key,
                "sourceMatchKey": data["source_match_key"],
                "goodsId": source_external_key,
                "sourceGoodsId": source_external_key,
                "sourceName": source_name,
                "sourceManufacturer": source_manufacturer,
                "sourceDosageForm": data["parsed_form"] or "",
                "sourceNormalizedName": data["normalized_name"] or normalize_mapping_text(source_name),
                "productId": int(data["our_product_id"]) if data["our_product_id"] is not None else None,
                "ourProductId": int(data["our_product_id"]) if data["our_product_id"] is not None else None,
                "ourSku": data["our_sku"] or "",
                "ourName": data["our_name"] or "",
                "ourManufacturer": data["our_manufacturer"] or "",
                "candidatesCount": 0,
                "candidates": [],
                "bestCandidate": None,
            }
        if include_candidates and mapping_status == "unmapped":
            candidates = _manual_candidates_for_source(db, item_payload)
            item_payload["candidates"] = candidates
            item_payload["candidatesCount"] = len(candidates)
            item_payload["bestCandidate"] = candidates[0] if candidates else None
        items.append(item_payload)

    return {
        "items": items,
        "metrics": [metrics],
        "pagination": {
            "page": page,
            "pageSize": limit,
            "total": filtered_total,
            "pageCount": page_count,
        },
    }


def _catalog_mapped_condition(platform: str, assigned_ids: list[int] | None):
    manual_exists = exists(
        select(1)
        .select_from(CompetitorCodeMapping)
        .where(CompetitorCodeMapping.platform == platform)
        .where(CompetitorCodeMapping.status == "mapped")
        .where(CompetitorCodeMapping.our_product_id == Product.id)
    )
    item_exists_stmt = (
        select(1)
        .select_from(CompetitorPriceListItem)
        .join(CompetitorPriceList, CompetitorPriceList.id == CompetitorPriceListItem.price_list_id)
        .where(CompetitorPriceList.source_type == platform)
        .where((CompetitorPriceListItem.product_id == Product.id) | (CompetitorPriceListItem.matched_sku == Product.code))
    )
    if assigned_ids is not None:
        item_exists_stmt = item_exists_stmt.where(CompetitorPriceList.id.in_(assigned_ids))
    item_exists = exists(item_exists_stmt)
    condition = manual_exists | item_exists
    if platform == "provisor":
        condition = condition | Product.provisor_goods_id.is_not(None)
    return condition


def _list_catalog_code_mappings_sql_page(
    *,
    db: Session,
    platform: str,
    price_format_id: int | None,
    assigned_ids: list[int] | None,
    status: str,
    product_q: str,
    page: int,
    limit: int,
) -> dict:
    mapped_condition = _catalog_mapped_condition(platform, assigned_ids)
    product_filters = []
    product_search = product_q.strip()
    if product_search:
        like = f"%{product_search}%"
        product_filters.append((Product.code.ilike(like)) | (Product.name.ilike(like)) | (ProductExtra.manufacturer.ilike(like)))
    if status == "mapped":
        product_filters.append(mapped_condition)
    elif status == "unmapped":
        product_filters.append(~mapped_condition)

    count_stmt = (
        select(func.count(func.distinct(Product.id)))
        .select_from(Product)
        .outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
    )
    product_stmt = (
        select(Product, ProductExtra)
        .outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
        .order_by(Product.code.asc())
    )
    for condition in product_filters:
        count_stmt = count_stmt.where(condition)
        product_stmt = product_stmt.where(condition)

    filtered_total = int(db.scalar(count_stmt) or 0)
    page_count = (filtered_total + limit - 1) // limit if filtered_total else 0
    if page_count and page > page_count:
        page = page_count
    product_rows = db.execute(product_stmt.limit(limit).offset((page - 1) * limit)).all()
    products_by_id = {int(product.id): (product, extra) for product, extra in product_rows}
    products_by_sku = {product.code: int(product.id) for product, _extra in product_rows}
    product_ids = list(products_by_id)
    product_skus = list(products_by_sku)

    item_stmt = (
        select(CompetitorPriceListItem, CompetitorPriceList)
        .join(CompetitorPriceList, CompetitorPriceList.id == CompetitorPriceListItem.price_list_id)
        .where(CompetitorPriceList.source_type == platform)
        .where(
            (CompetitorPriceListItem.product_id.in_(product_ids) if product_ids else False)
            | (CompetitorPriceListItem.matched_sku.in_(product_skus) if product_skus else False)
        )
        .order_by(desc(CompetitorPriceList.price_date), desc(CompetitorPriceListItem.match_score), CompetitorPriceListItem.id.desc())
    )
    if assigned_ids is not None:
        item_stmt = item_stmt.where(CompetitorPriceList.id.in_(assigned_ids))
    item_rows = db.execute(item_stmt).all() if product_rows else []

    items_by_product: dict[int, list[dict]] = {}
    payload_by_match_key: dict[str, dict] = {}
    payloads_by_external_key: dict[str, list[dict]] = {}
    keys: set[str] = set()
    for item, price_list in item_rows:
        payload = _source_item_to_payload(platform, item, price_list)
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

    mappings_by_product: dict[int, CompetitorCodeMapping] = {}
    if product_ids:
        for row in db.execute(
            select(CompetitorCodeMapping)
            .where(CompetitorCodeMapping.platform == platform)
            .where(CompetitorCodeMapping.status == "mapped")
            .where(CompetitorCodeMapping.our_product_id.in_(product_ids))
        ).scalars():
            mappings_by_product.setdefault(int(row.our_product_id), row)

    rows: list[dict] = []
    for product, extra in product_rows:
        product_id = int(product.id)
        manual = mappings_by_product.get(product_id)
        product_items = list(items_by_product.get(product_id, []))
        existing_matched_item = next((item for item in product_items if item.get("matchedSku") == product.code), None)
        has_primary_goods_mapping = platform == "provisor" and product.provisor_goods_id is not None
        mapped_item = None
        mapping_id = None
        row_status = "unmapped"
        if manual is not None:
            mapped_item = payload_by_match_key.get(manual.source_match_key) or {
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
            mapped_item = next(iter(payloads_by_external_key.get(str(product.provisor_goods_id), [])), None) or {
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
                "candidatesCount": 0,
                "candidates": [],
                "bestCandidate": None,
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

    total_products = int(db.scalar(select(func.count(Product.id))) or 0)
    mapped_total = int(db.scalar(select(func.count(Product.id)).where(_catalog_mapped_condition(platform, assigned_ids))) or 0)
    metrics = {
        "platform": platform,
        "total": total_products,
        "mapped": mapped_total,
        "unmapped": max(0, total_products - mapped_total),
        "rejected": 0,
        "noCandidates": 0,
        "coveragePercent": round((mapped_total / total_products) * 100, 2) if total_products else 0,
        "mappingCoveragePercent": round((mapped_total / total_products) * 100, 2) if total_products else 0,
        "generatedPricingCoverage": _generated_pricing_coverage(db, price_format_id),
    }
    return {
        "items": rows,
        "metrics": [metrics],
        "pagination": {
            "page": page,
            "pageSize": limit,
            "total": filtered_total,
            "pageCount": page_count,
        },
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
    assigned_ids = (
        [int(item.price_list.id) for item in get_assigned_competitor_price_lists(db=db, price_format_id=price_format_id)]
        if price_format_id is not None
        else None
    )
    if platform == "provisor":
        return _list_provisor_catalog_code_mappings_sql_page(
            db=db,
            price_format_id=price_format_id,
            assigned_ids=assigned_ids,
            status=status,
            source_q=source_q,
            product_q=product_q,
            page=page,
            limit=limit,
            include_candidates=include_candidates,
        )
    if (
        not include_candidates
        and not source_q.strip()
        and status in {"all", "mapped", "unmapped"}
    ):
        return _list_catalog_code_mappings_sql_page(
            db=db,
            platform=platform,
            price_format_id=price_format_id,
            assigned_ids=assigned_ids,
            status=status,
            product_q=product_q,
            page=page,
            limit=limit,
        )

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


def _product_catalog_platforms(platform: str) -> list[str]:
    value = str(platform or "all").strip().lower()
    if value in {"", "all", "__all__"}:
        return sorted(SUPPORTED_PLATFORMS)
    return [platform_from_value(value)]


def _product_catalog_mapping_payload(row: CompetitorCodeMapping) -> dict:
    external = str(row.source_external_key or "")
    return {
        "id": int(row.id),
        "mappingId": int(row.id),
        "platform": row.platform,
        "sourceKey": row.source_match_key,
        "sourceMatchKey": row.source_match_key,
        "externalId": external,
        "sourceExternalKey": external,
        "externalName": row.source_name,
        "sourceName": row.source_name,
        "externalManufacturer": row.source_manufacturer,
        "sourceManufacturer": row.source_manufacturer,
        "status": row.status,
        "confidence": float(row.confidence) if row.confidence is not None else None,
    }


def _product_catalog_candidate_explanation(candidate: dict) -> list[dict]:
    details = candidate.get("manualSuggestion") if isinstance(candidate.get("manualSuggestion"), dict) else {}
    items = [
        {"label": "Название", "status": "match", "message": f"Совпадение {float(candidate.get('confidence') or 0):.0f}%"},
    ]
    if details.get("dosageMatch"):
        items.append({"label": "Дозировка", "status": "match", "message": "Критические значения совместимы"})
    if details.get("formMatch") is True:
        items.append({"label": "Форма", "status": "match", "message": "Форма совместима"})
    if details.get("quantityMatch") is True:
        items.append({"label": "Количество", "status": "match", "message": "Количество совпадает"})
    if candidate.get("manufacturerMismatch"):
        items.append({"label": "Производитель", "status": "warning", "message": "Производитель отличается"})
    elif candidate.get("sourceManufacturer") or candidate.get("ourManufacturer") or candidate.get("internalManufacturer"):
        items.append({"label": "Производитель", "status": "match", "message": "Производитель совместим или не критичен"})
    return items


def _product_catalog_candidate_payload(product: Product, extra: ProductExtra | None, source: dict, level: tuple[str, bool, float, dict]) -> dict:
    match_level, manufacturer_mismatch, confidence, score_details = level
    payload = {
        **source,
        "productId": int(product.id),
        "ourProductId": int(product.id),
        "ourSku": product.code,
        "ourName": product.name,
        "ourManufacturer": (extra.manufacturer if extra else "") or "",
        "internalManufacturer": (extra.manufacturer if extra else "") or "",
        "confidence": confidence,
        "matchType": "auto_match_candidate" if match_level == "exact" and confidence >= 99 else "manual_review_candidate",
        "matchLevel": match_level,
        "classification": "auto_match" if match_level == "exact" and confidence >= 99 else "manual_review",
        "manufacturerMismatch": manufacturer_mismatch,
        "manualSuggestion": score_details,
    }
    payload["explanation"] = _product_catalog_candidate_explanation(payload)
    return payload


def _product_catalog_source_candidates_for_products(
    db: Session,
    *,
    products: list[tuple[Product, ProductExtra | None]],
    platforms: list[str],
    limit_per_product: int = PRODUCT_CATALOG_CANDIDATE_LIMIT,
) -> dict[int, list[dict]]:
    from ..competitor_matching import parse_drug_structure

    if not products:
        return {}

    token_by_product: dict[int, str] = {}
    tokens: list[str] = []
    for product, _extra in products:
        normalized_name = normalize_mapping_text(product.name)
        structure = parse_drug_structure(product.name)
        base_name = structure.base_name or normalize_mapping_text(product.name)
        token = base_name.split(" ", 1)[0] if base_name else ""
        raw_normalized_token = normalized_name.split(" ", 1)[0] if normalized_name else ""
        raw_token = str(product.name or "").split(" ", 1)[0].strip()
        if token and raw_normalized_token and token not in normalized_name:
            token = raw_normalized_token
        if len(token) < 3 or token.isdigit():
            continue
        token_by_product[int(product.id)] = token
        if token not in tokens:
            tokens.append(token)
        if raw_normalized_token and raw_normalized_token not in tokens:
            tokens.append(raw_normalized_token)
        if raw_token and raw_token not in tokens:
            tokens.append(raw_token)
    if not tokens:
        return {int(product.id): [] for product, _extra in products}

    token_filter = None
    for token in tokens[:50]:
        condition = CompetitorPriceListItem.name.ilike(f"%{token}%") | CompetitorPriceListItem.raw_name.ilike(f"%{token}%")
        token_filter = condition if token_filter is None else token_filter | condition

    rows = (
        db.execute(
            select(CompetitorPriceListItem, CompetitorPriceList)
            .join(CompetitorPriceList, CompetitorPriceList.id == CompetitorPriceListItem.price_list_id)
            .where(CompetitorPriceList.source_type.in_(platforms))
            .where(token_filter)
            .order_by(desc(CompetitorPriceList.price_date), desc(CompetitorPriceListItem.id))
            .limit(750)
        )
        .all()
    )
    if not rows:
        return {int(product.id): [] for product, _extra in products}

    source_by_key: dict[str, dict] = {}
    for item, price_list in rows:
        source = _source_item_to_payload(price_list.source_type, item, price_list)
        key = str(source.get("sourceMatchKey") or "")
        if key and key not in source_by_key:
            source_by_key[key] = source

    existing_keys = set(source_by_key)
    if existing_keys:
        for row in db.execute(
            select(CompetitorCodeMapping)
            .where(CompetitorCodeMapping.platform.in_(platforms))
            .where(CompetitorCodeMapping.source_match_key.in_(existing_keys))
            .where(CompetitorCodeMapping.status.in_(["mapped", "rejected"]))
        ).scalars():
            source_by_key.pop(row.source_match_key, None)

    out: dict[int, list[dict]] = {}
    source_rows = list(source_by_key.values())
    for product, extra in products:
        product_id = int(product.id)
        token = token_by_product.get(product_id, "")
        candidates: dict[str, dict] = {}
        for source in source_rows:
            source_name = str(source.get("sourceName") or "")
            if token and token not in normalize_mapping_text(source_name):
                continue
            level = _manual_candidate_level(
                source_name=source_name,
                source_manufacturer=str(source.get("sourceManufacturer") or ""),
                product_name=product.name,
                product_manufacturer=(extra.manufacturer if extra else "") or "",
            )
            if level is None:
                continue
            candidate = _product_catalog_candidate_payload(product, extra, source, level)
            key = str(candidate.get("sourceMatchKey") or "")
            previous = candidates.get(key)
            if previous is None or float(previous.get("confidence") or 0) < float(candidate.get("confidence") or 0):
                candidates[key] = candidate
        out[product_id] = sorted(candidates.values(), key=lambda item: float(item.get("confidence") or 0), reverse=True)[:limit_per_product]
    return out


def _product_catalog_search_external_product_ids(db: Session, *, platforms: list[str], q: str) -> set[int]:
    query = q.strip()
    if not query:
        return set()
    like = f"%{query}%"
    rows = db.execute(
        select(CompetitorCodeMapping.our_product_id)
        .where(CompetitorCodeMapping.platform.in_(platforms))
        .where(CompetitorCodeMapping.status == "mapped")
        .where(CompetitorCodeMapping.our_product_id.is_not(None))
        .where(
            or_(
                CompetitorCodeMapping.source_external_key.ilike(like),
                CompetitorCodeMapping.source_name.ilike(like),
                CompetitorCodeMapping.source_manufacturer.ilike(like),
                CompetitorCodeMapping.source_match_key.ilike(like),
            )
        )
    ).scalars()
    return {int(item) for item in rows if item is not None}


def list_product_catalog_code_mappings(
    *,
    db: Session,
    platform: str = "all",
    q: str = "",
    status: str = "all",
    page: int = 1,
    limit: int = 50,
    include_candidates: bool = True,
) -> dict:
    platforms = _product_catalog_platforms(platform)
    status = status if status in {"all", "mapped", "review", "unmapped"} else "all"
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 50), 200))

    mapped_exists = exists(
        select(1)
        .select_from(CompetitorCodeMapping)
        .where(CompetitorCodeMapping.status == "mapped")
        .where(CompetitorCodeMapping.platform.in_(platforms))
        .where(CompetitorCodeMapping.our_product_id == Product.id)
    )

    external_product_ids = _product_catalog_search_external_product_ids(db, platforms=platforms, q=q)
    search = q.strip()
    product_filter = None
    if search:
        like = f"%{search}%"
        product_filter = or_(
            Product.code.ilike(like),
            Product.name.ilike(like),
            ProductExtra.manufacturer.ilike(like),
            Product.id.in_(external_product_ids) if external_product_ids else literal(False),
        )

    base_count_stmt = select(func.count(Product.id)).select_from(Product).outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
    mapped_count_stmt = base_count_stmt.where(mapped_exists)
    total_products = int(db.scalar(base_count_stmt) or 0)
    mapped_products = int(db.scalar(mapped_count_stmt) or 0)

    product_stmt = (
        select(Product, ProductExtra)
        .outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
        .order_by(Product.code.asc())
    )
    count_stmt = base_count_stmt
    if product_filter is not None:
        product_stmt = product_stmt.where(product_filter)
        count_stmt = count_stmt.where(product_filter)
    if status == "mapped":
        product_stmt = product_stmt.where(mapped_exists)
        count_stmt = count_stmt.where(mapped_exists)
    elif status in {"review", "unmapped"}:
        product_stmt = product_stmt.where(~mapped_exists)
        count_stmt = count_stmt.where(~mapped_exists)

    filtered_total = int(db.scalar(count_stmt) or 0)
    page_count = (filtered_total + limit - 1) // limit if filtered_total else 0
    if page_count and page > page_count:
        page = page_count
    product_rows = db.execute(product_stmt.limit(limit).offset((page - 1) * limit)).all()
    product_ids = [int(product.id) for product, _extra in product_rows]

    mappings_by_product: dict[int, list[dict]] = {product_id: [] for product_id in product_ids}
    if product_ids:
        for row in db.execute(
            select(CompetitorCodeMapping)
            .where(CompetitorCodeMapping.platform.in_(platforms))
            .where(CompetitorCodeMapping.status == "mapped")
            .where(CompetitorCodeMapping.our_product_id.in_(product_ids))
            .order_by(CompetitorCodeMapping.platform.asc(), CompetitorCodeMapping.source_external_key.asc())
        ).scalars():
            mappings_by_product.setdefault(int(row.our_product_id), []).append(_product_catalog_mapping_payload(row))

    candidates_by_product = (
        _product_catalog_source_candidates_for_products(db, products=product_rows, platforms=platforms)
        if include_candidates or status == "review"
        else {int(product.id): [] for product, _extra in product_rows}
    )

    rows: list[dict] = []
    for product, extra in product_rows:
        product_id = int(product.id)
        mappings = mappings_by_product.get(product_id, [])
        candidates = [] if mappings else candidates_by_product.get(product_id, [])
        row_status = "mapped" if mappings else "review" if candidates else "unmapped"
        if status == "review" and row_status != "review":
            continue
        if status == "unmapped" and row_status != "unmapped":
            continue
        rows.append(
            {
                "productId": product_id,
                "sku": product.code,
                "name": product.name,
                "manufacturer": (extra.manufacturer if extra else "") or "",
                "platform": "all" if len(platforms) > 1 else platforms[0],
                "mappings": mappings,
                "mappingCount": len(mappings),
                "status": row_status,
                "reviewCandidates": candidates,
                "candidates": candidates,
                "bestCandidate": candidates[0] if candidates else None,
            }
        )

    if status in {"review", "unmapped"} and (include_candidates or status == "review"):
        filtered_total = len(rows) if not search else len(rows)
        page_count = 1 if rows else 0
        page = 1 if rows else page

    review_on_page = sum(1 for row in rows if row["status"] == "review")
    unmapped_on_page = sum(1 for row in rows if row["status"] == "unmapped")
    metrics = {
        "platform": "all" if len(platforms) > 1 else platforms[0],
        "total": total_products,
        "mapped": mapped_products,
        "review": review_on_page,
        "unmapped": max(0, total_products - mapped_products - review_on_page),
        "rejected": 0,
        "noCandidates": unmapped_on_page,
        "coveragePercent": round((mapped_products / total_products) * 100, 2) if total_products else 0,
        "mappingCoveragePercent": round((mapped_products / total_products) * 100, 2) if total_products else 0,
    }
    return {
        "items": rows,
        "metrics": [metrics],
        "pagination": {"page": page, "pageSize": limit, "total": filtered_total, "pageCount": page_count},
        "platforms": platforms,
    }


def auto_match_product_catalog_code_mappings(
    *,
    db: Session,
    platform: str = "all",
    limit: int = 100,
    created_by: str = "",
) -> dict:
    platforms = _product_catalog_platforms(platform)
    limit = max(1, min(int(limit or 100), 500))
    product_rows = (
        db.execute(
            select(Product, ProductExtra)
            .outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
            .where(
                ~exists(
                    select(1)
                    .select_from(CompetitorCodeMapping)
                    .where(CompetitorCodeMapping.status == "mapped")
                    .where(CompetitorCodeMapping.platform.in_(platforms))
                    .where(CompetitorCodeMapping.our_product_id == Product.id)
                )
            )
            .order_by(Product.code.asc())
            .limit(limit)
        )
        .all()
    )
    candidates_by_product = _product_catalog_source_candidates_for_products(db, products=product_rows, platforms=platforms, limit_per_product=1)
    created = 0
    reviewed = 0
    for product, _extra in product_rows:
        reviewed += 1
        candidate = (candidates_by_product.get(int(product.id)) or [None])[0]
        if not candidate or candidate.get("classification") != "auto_match":
            continue
        row = upsert_code_mapping(
            db=db,
            platform=str(candidate.get("platform") or platforms[0]),
            product=product,
            source_payload={
                "source_external_key": candidate.get("sourceExternalKey"),
                "source_match_key": candidate.get("sourceMatchKey"),
                "source_name": candidate.get("sourceName"),
                "source_manufacturer": candidate.get("sourceManufacturer"),
                "source_dosage_form": candidate.get("sourceDosageForm"),
                "source_normalized_name": candidate.get("sourceNormalizedName"),
            },
            status="mapped",
            confidence=float(candidate.get("confidence") or 100),
            created_by=created_by,
        )
        apply_mapping_to_matching_items(db=db, mapping=row, product=product, clear=False)
        created += 1
    db.commit()
    return {"status": "ok", "reviewedProducts": reviewed, "createdMappings": created, "platforms": platforms}


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
    touched_price_list_ids: set[int] = set()
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
        touched_price_list_ids.add(int(item.price_list_id))
    if touched_price_list_ids:
        db.flush()
        refresh_price_list_item_counters(db=db, price_list_ids=touched_price_list_ids)
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
