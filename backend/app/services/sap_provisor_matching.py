from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, func, or_, select, text, update
from sqlalchemy.orm import Session

from ..models import (
    CompetitorCodeMapping,
    Product,
    ProductExtra,
    SapMatchingSourceBatch,
    SapMatchingSourceItem,
)
from ..timezone import now_kz_naive
from .competitor_matching import parse_drug_structure
from .competitors.code_mappings import apply_mapping_to_matching_items, upsert_code_mapping
from .references.sap_balance import SAP_LGORT, SAP_PLANT_BY_CODE, SapBalancePayload
from .sku import normalize_sku


ALLOWED_PAGE_SIZES = {25, 50, 100}


class SapProvisorQueueUnavailable(ValueError):
    pass


def _raw_value(raw: dict[str, Any], *keys: str) -> str:
    folded = {str(key).casefold(): value for key, value in raw.items()}
    for key in keys:
        value = raw.get(key, folded.get(key.casefold()))
        if value not in (None, ""):
            return str(value).strip()
    return ""


def import_sap_matching_payload(
    db: Session,
    *,
    payload: SapBalancePayload,
    source_version: str,
    source_type: str,
    imported_by: str,
    payload_sha256: str,
) -> dict[str, Any]:
    if payload.plant not in SAP_PLANT_BY_CODE:
        raise ValueError(f"SAP plant is not approved: {payload.plant}")
    rows = [{"material": item.material, "plant": item.plant, "lgort": item.lgort, "raw": item.raw} for item in payload.items]
    return import_sap_matching_rows(
        db, rows=rows, source_version=source_version, source_type=source_type,
        plant=payload.plant, lgort=SAP_LGORT, effective_date=payload.effective_date,
        imported_by=imported_by, payload_sha256=payload_sha256,
    )


def import_sap_matching_rows(
    db: Session,
    *,
    rows: list[dict[str, Any]],
    source_version: str,
    source_type: str,
    plant: str,
    lgort: str,
    effective_date: Any,
    imported_by: str,
    payload_sha256: str,
) -> dict[str, Any]:
    version = str(source_version or "").strip()
    if not version or len(version) > 128:
        raise ValueError("sourceVersion is required and must not exceed 128 characters")
    if plant and plant not in SAP_PLANT_BY_CODE:
        raise ValueError(f"SAP plant is not approved: {plant}")
    if lgort != SAP_LGORT:
        raise ValueError(f"matching source warehouse must be {SAP_LGORT}")
    existing = db.scalar(select(SapMatchingSourceBatch).where(
        SapMatchingSourceBatch.source_version == version,
        SapMatchingSourceBatch.plant == plant,
        SapMatchingSourceBatch.lgort == lgort,
    ))
    if existing is not None:
        if existing.payload_sha256 != payload_sha256:
            raise ValueError("sourceVersion already exists with different content")
        return _batch_result(db, existing, idempotent=True)

    grouped: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows, start=1):
        material = row.get("material")
        if not isinstance(material, str) or not material.strip():
            raise ValueError(f"matching source row {index} has invalid material")
        row_plant = str(row.get("plant") or plant).strip()
        row_lgort = str(row.get("lgort") or lgort).strip()
        if row_plant != plant or row_lgort != lgort:
            raise ValueError(f"matching source row {index} scope mismatch")
        sku = normalize_sku(material)
        if not sku:
            raise ValueError(f"matching source row {index} has invalid material")
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        group = grouped.setdefault(sku, {"materials": set(), "names": set(), "manufacturers": set(), "raw": []})
        group["materials"].add(material.strip())
        name = _raw_value(raw, "materialText", "material_name", "materialName", "name")
        manufacturer = _raw_value(raw, "manufacturer", "manufacturerName", "producer", "producerName")
        if name:
            group["names"].add(name)
        if manufacturer:
            group["manufacturers"].add(manufacturer)
        group["raw"].append(raw)

    product_candidates: dict[str, list[tuple[int, str, str]]] = {}
    for product_id, code, name, manufacturer in db.execute(
        select(Product.id, Product.code, Product.name, ProductExtra.manufacturer).outerjoin(
            ProductExtra, ProductExtra.product_id == Product.id
        )
    ):
        sku = normalize_sku(code)
        if sku in grouped:
            product_candidates.setdefault(sku, []).append(
                (int(product_id), str(name or ""), str(manufacturer or ""))
            )

    batch = SapMatchingSourceBatch(
        source_version=version, source_type=source_type, plant=plant, lgort=lgort,
        effective_date=effective_date, payload_sha256=payload_sha256, active=True,
        record_count=len(grouped), imported_by=imported_by,
    )
    db.add(batch)
    db.flush()
    imported_at = now_kz_naive()
    items = []
    for sku, group in sorted(grouped.items()):
        candidates = product_candidates.get(sku, [])
        identity_status = (
            "ambiguous" if len(group["materials"]) > 1
            else ("resolved" if len(candidates) == 1 else ("missing" if not candidates else "ambiguous"))
        )
        candidate = candidates[0] if len(candidates) == 1 else None
        items.append({
            "batch_id": batch.id,
            "material": sorted(group["materials"])[0],
            "normalized_sku": sku,
            "product_id": candidate[0] if candidate else None,
            "product_name": sorted(group["names"])[0] if group["names"] else (candidate[1] if candidate else ""),
            "manufacturer": sorted(group["manufacturers"])[0] if group["manufacturers"] else (candidate[2] if candidate else ""),
            "identity_status": identity_status,
            "raw_json": json.dumps({"materials": sorted(group["materials"]), "rows": group["raw"]}, ensure_ascii=False, default=str),
            "imported_at": imported_at,
        })
    if items:
        db.execute(SapMatchingSourceItem.__table__.insert(), items)
    db.execute(update(SapMatchingSourceBatch).where(
        SapMatchingSourceBatch.id != batch.id,
        SapMatchingSourceBatch.plant == plant,
        SapMatchingSourceBatch.lgort == lgort,
        SapMatchingSourceBatch.active.is_(True),
    ).values(active=False))
    if not plant:
        # A verified aggregate export is a complete technical population and
        # atomically replaces any prior active matching source.
        db.execute(update(SapMatchingSourceBatch).where(
            SapMatchingSourceBatch.id != batch.id,
            SapMatchingSourceBatch.active.is_(True),
        ).values(active=False))
    else:
        version_plant_count = int(db.scalar(
            select(func.count(func.distinct(SapMatchingSourceBatch.plant))).where(
                SapMatchingSourceBatch.source_version == version,
                SapMatchingSourceBatch.active.is_(True),
                SapMatchingSourceBatch.lgort == SAP_LGORT,
                SapMatchingSourceBatch.plant.in_(tuple(SAP_PLANT_BY_CODE)),
            )
        ) or 0)
        if version_plant_count == len(SAP_PLANT_BY_CODE):
            # Keep the aggregate fallback during a partial 12-plant refresh;
            # retire it only after the complete replacement version exists.
            db.execute(update(SapMatchingSourceBatch).where(
                SapMatchingSourceBatch.plant == "",
                SapMatchingSourceBatch.active.is_(True),
            ).values(active=False))
    db.flush()
    return _batch_result(db, batch, idempotent=False)


def _batch_result(db: Session, batch: SapMatchingSourceBatch, *, idempotent: bool) -> dict[str, Any]:
    counts = dict(db.execute(
        select(SapMatchingSourceItem.identity_status, func.count()).where(
            SapMatchingSourceItem.batch_id == batch.id
        ).group_by(SapMatchingSourceItem.identity_status)
    ).all())
    return {
        "batchId": int(batch.id), "sourceVersion": batch.source_version,
        "plant": batch.plant, "warehouse": batch.lgort, "recordCount": batch.record_count,
        "resolvedCount": int(counts.get("resolved", 0)), "missingCount": int(counts.get("missing", 0)),
        "ambiguousCount": int(counts.get("ambiguous", 0)), "idempotent": idempotent,
        "stockPublicationPerformed": False,
    }


def sap_matching_source_health(db: Session) -> dict[str, Any]:
    allowed_plants = tuple(["", *SAP_PLANT_BY_CODE])
    batches = list(db.scalars(select(SapMatchingSourceBatch).where(
        SapMatchingSourceBatch.active.is_(True),
        SapMatchingSourceBatch.lgort == SAP_LGORT,
        SapMatchingSourceBatch.plant.in_(allowed_plants),
    ).order_by(SapMatchingSourceBatch.plant, SapMatchingSourceBatch.imported_at.desc())))
    counts = dict(db.execute(
        select(SapMatchingSourceItem.identity_status, func.count(func.distinct(SapMatchingSourceItem.normalized_sku)))
        .join(SapMatchingSourceBatch, SapMatchingSourceBatch.id == SapMatchingSourceItem.batch_id)
        .where(SapMatchingSourceBatch.active.is_(True), SapMatchingSourceBatch.lgort == SAP_LGORT,
               SapMatchingSourceBatch.plant.in_(allowed_plants))
        .group_by(SapMatchingSourceItem.identity_status)
    ).all())
    unique_skus = int(db.scalar(select(func.count(func.distinct(SapMatchingSourceItem.normalized_sku)))
        .join(SapMatchingSourceBatch, SapMatchingSourceBatch.id == SapMatchingSourceItem.batch_id)
        .where(SapMatchingSourceBatch.active.is_(True), SapMatchingSourceBatch.lgort == SAP_LGORT,
               SapMatchingSourceBatch.plant.in_(allowed_plants))) or 0)
    available = bool(batches)
    available_plants = sorted({row.plant for row in batches if row.plant})
    aggregate_present = any(not row.plant for row in batches)
    missing_plants = [] if aggregate_present else [plant for plant in SAP_PLANT_BY_CODE if plant not in available_plants]
    return {
        "available": available,
        "complete": available and not missing_plants and not counts.get("missing", 0) and not counts.get("ambiguous", 0),
        "availablePlants": available_plants,
        "missingPlants": missing_plants, "stalePlants": [], "conflictingPlants": [],
        "uniqueSkuCount": unique_skus, "resolvedCount": int(counts.get("resolved", 0)),
        "missingIdentityCount": int(counts.get("missing", 0)),
        "ambiguousIdentityCount": int(counts.get("ambiguous", 0)),
        "batches": [{"batchId": int(row.id), "sourceVersion": row.source_version, "sourceType": row.source_type,
                     "plant": row.plant, "warehouse": row.lgort, "recordCount": row.record_count,
                     "importedAt": row.imported_at.isoformat() if row.imported_at else None} for row in batches],
    }


def _active_matching_products():
    allowed_plants = tuple(["", *SAP_PLANT_BY_CODE])
    return (
        select(
            SapMatchingSourceItem.normalized_sku.label("normalized_sku"),
            func.min(SapMatchingSourceItem.material).label("sap_material"),
            func.max(SapMatchingSourceItem.product_name).label("sap_name"),
            func.max(SapMatchingSourceItem.manufacturer).label("sap_manufacturer"),
            func.min(SapMatchingSourceItem.product_id).label("product_id"),
            func.count(func.distinct(SapMatchingSourceItem.product_id)).label("product_variant_count"),
        )
        .join(SapMatchingSourceBatch, SapMatchingSourceBatch.id == SapMatchingSourceItem.batch_id)
        .where(SapMatchingSourceBatch.active.is_(True), SapMatchingSourceBatch.lgort == SAP_LGORT,
               SapMatchingSourceBatch.plant.in_(allowed_plants), SapMatchingSourceItem.identity_status == "resolved")
        .group_by(SapMatchingSourceItem.normalized_sku)
        .subquery("active_sap_matching_products")
    )


def _approved_provisor_mapping_exists(product_id_column):
    return exists(
        select(1).where(
            CompetitorCodeMapping.platform == "provisor",
            CompetitorCodeMapping.status == "mapped",
            CompetitorCodeMapping.our_product_id == product_id_column,
        )
    )


def _structure_payload(name: str) -> dict[str, Any]:
    structure = parse_drug_structure(name)
    forms = list(structure.forms or ((structure.form,) if structure.form else ()))
    dosage_parts = [
        str(value)
        for value in (
            structure.dosage,
            structure.dosage_volume,
            structure.concentration,
            structure.percent_strength,
            structure.iu_dosage,
        )
        if value not in (None, "")
    ]
    return {
        "dosage": " / ".join(dosage_parts),
        "form": " / ".join(forms),
        "packageCount": structure.quantity,
    }


def list_unmatched_sap_products(
    db: Session,
    *,
    q: str = "",
    page: int = 1,
    page_size: int = 25,
) -> dict[str, Any]:
    if page_size not in ALLOWED_PAGE_SIZES:
        raise ValueError("Размер страницы должен быть 25, 50 или 100")
    page = max(1, int(page or 1))
    health = sap_matching_source_health(db)
    if not health["available"]:
        raise SapProvisorQueueUnavailable(
            "Источник SAP для технического сопоставления ещё не импортирован."
        )
    active_products = _active_matching_products()
    ambiguous_material = db.execute(
        select(active_products.c.normalized_sku).where(active_products.c.product_variant_count > 1).limit(1)
    ).first()
    if ambiguous_material is not None:
        raise SapProvisorQueueUnavailable(
            "Источник SAP содержит неоднозначную внутреннюю идентификацию товара."
        )
    base = (
        select(
            Product, ProductExtra, active_products.c.sap_material,
            active_products.c.sap_name, active_products.c.sap_manufacturer,
        )
        .join(active_products, active_products.c.product_id == Product.id)
        .outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
        .where(
            Product.provisor_goods_id.is_(None),
            ~_approved_provisor_mapping_exists(Product.id),
        )
    )
    search = str(q or "").strip()
    if search:
        like = f"%{search}%"
        base = base.where(
            or_(
                active_products.c.sap_material.ilike(like),
                active_products.c.sap_name.ilike(like),
                Product.code.ilike(like),
                Product.name.ilike(like),
            )
        )

    count_stmt = select(func.count()).select_from(base.order_by(None).subquery())
    total = int(db.scalar(count_stmt) or 0)
    page_count = (total + page_size - 1) // page_size if total else 0
    if page_count and page > page_count:
        page = page_count
    rows = db.execute(
        base.order_by(Product.code.asc()).limit(page_size).offset((page - 1) * page_size)
    ).all()
    items = []
    for product, extra, sap_material, sap_name, sap_manufacturer in rows:
        display_name = str(sap_name or product.name or "")
        items.append(
            {
                "productId": int(product.id),
                "sapSku": str(sap_material),
                "sapName": display_name,
                "sapManufacturer": str(sap_manufacturer or (extra.manufacturer if extra else "") or ""),
                **_structure_payload(display_name),
            }
        )
    return {
        "items": items,
        "filter": "unmatched",
        "pagination": {
            "page": page,
            "pageSize": page_size,
            "total": total,
            "pageCount": page_count,
        },
        "sourceHealth": health,
    }


def _lock_goods_identity(db: Session, goods_id: int) -> None:
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(goods_id)})


def confirm_sap_provisor_mapping(
    db: Session,
    *,
    product_id: int,
    candidate: dict[str, Any],
    username: str,
) -> dict[str, Any]:
    try:
        goods_id = int(candidate.get("goodsId") or candidate.get("sourceExternalKey"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректный Provisor Goods ID") from exc
    if goods_id <= 0 or str(candidate.get("sourceMatchKey") or "") != f"provisor:{goods_id}":
        raise ValueError("Ответ официального каталога Provisor не подтвердил Goods ID")

    _lock_goods_identity(db, goods_id)
    product = db.scalar(select(Product).where(Product.id == product_id).with_for_update())
    if product is None:
        raise ValueError("Товар SAP больше не существует")
    active_products = _active_matching_products()
    if db.scalar(select(active_products.c.product_id).where(active_products.c.product_id == product_id)) is None:
        raise ValueError("Товар отсутствует в активном источнике SAP для сопоставления")

    existing_for_product = db.scalar(
        select(CompetitorCodeMapping)
        .where(
            CompetitorCodeMapping.platform == "provisor",
            CompetitorCodeMapping.status == "mapped",
            CompetitorCodeMapping.our_product_id == product_id,
        )
        .with_for_update()
    )
    if product.provisor_goods_id is not None and int(product.provisor_goods_id) != goods_id:
        raise ValueError(f"Товар уже сопоставлен с Provisor Goods ID {product.provisor_goods_id}")
    if existing_for_product is not None and existing_for_product.source_match_key != f"provisor:{goods_id}":
        raise ValueError("Товар уже имеет другое подтверждённое сопоставление Provisor")

    direct_owner = db.scalar(
        select(Product)
        .where(Product.provisor_goods_id == goods_id, Product.id != product_id)
        .with_for_update()
    )
    mapping_owner = db.scalar(
        select(CompetitorCodeMapping)
        .where(
            CompetitorCodeMapping.platform == "provisor",
            CompetitorCodeMapping.source_match_key == f"provisor:{goods_id}",
            CompetitorCodeMapping.status == "mapped",
        )
        .with_for_update()
    )
    if direct_owner is not None:
        raise ValueError(f"Goods ID уже принадлежит товару SAP {direct_owner.code}")
    if mapping_owner is not None and mapping_owner.our_product_id not in (None, product_id):
        raise ValueError("Goods ID уже сопоставлен с другим товаром SAP")

    source_payload = {
        "source_external_key": str(goods_id),
        "source_match_key": f"provisor:{goods_id}",
        "source_name": str(candidate.get("sourceName") or candidate.get("fullName") or ""),
        "source_manufacturer": str(candidate.get("sourceManufacturer") or candidate.get("producer") or ""),
        "source_dosage_form": str(candidate.get("sourceDosageForm") or candidate.get("dosageForm") or ""),
        "source_normalized_name": str(candidate.get("sourceNormalizedName") or ""),
    }
    mapping = upsert_code_mapping(
        db=db,
        platform="provisor",
        product=product,
        source_payload=source_payload,
        status="mapped",
        confidence=100,
        created_by=username,
    )
    product.provisor_goods_id = goods_id
    db.flush()
    touched = apply_mapping_to_matching_items(db=db, mapping=mapping, product=product)
    db.commit()
    return {
        "productId": int(product.id),
        "sapSku": str(product.code),
        "goodsId": goods_id,
        "mappingId": int(mapping.id),
        "touchedItems": int(touched),
        "createdBy": username,
        "approvedAt": mapping.approved_at.isoformat() if mapping.approved_at else None,
    }
