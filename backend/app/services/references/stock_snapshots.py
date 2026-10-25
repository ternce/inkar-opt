from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from ...models import (
    BranchStock,
    Product,
    ReferenceImportJob,
    ReferenceUpdateStatus,
    StockSnapshot,
    StockSnapshotItem,
)
from ...timezone import now_kz_naive
from ..sku import normalize_sku
from .sap_balance import SAP_LGORT, SAP_PLANT_BY_CODE, SapBalancePayload
from .catalog_registration import (
    CatalogMaterial,
    preview_catalog_registration,
    register_catalog_preview,
)
from .types import branch_display_name


@dataclass(frozen=True)
class CanonicalStockRow:
    material: str
    normalized_sku: str
    product_id: int
    quantity: Decimal | None
    raw: dict[str, Any]


def _decimal_token(value: Decimal | None) -> str:
    if value is None:
        return "null"
    return format(value.normalize(), "f")


def stock_content_fingerprint(rows: Iterable[CanonicalStockRow]) -> str:
    digest = hashlib.sha256()
    for ordinal, row in enumerate(rows):
        # SAP rows are aggregated by material before publication.  Include the
        # retained batch evidence so a changed batch composition with the same
        # total quantity is not mistaken for an identical retry.  Manual
        # snapshot fingerprints intentionally keep their established format.
        audit_payload = None
        if isinstance(row.raw, dict) and ("sap_rows" in row.raw or row.raw.get("carry_forward")):
            audit_payload = row.raw
        digest.update(
            json.dumps(
                [
                    ordinal,
                    row.material,
                    row.normalized_sku,
                    row.product_id,
                    _decimal_token(row.quantity),
                    audit_payload,
                ]
                if audit_payload is not None
                else [ordinal, row.material, row.normalized_sku, row.product_id, _decimal_token(row.quantity)],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _lock_branch(db: Session, branch_id: str) -> None:
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"branch-stock:{branch_id}"})


def active_stock_status(db: Session, branch_id: str, *, lock: bool = False) -> ReferenceUpdateStatus | None:
    stmt = (
        select(ReferenceUpdateStatus)
        .where(ReferenceUpdateStatus.branch_id == branch_id)
        .where(ReferenceUpdateStatus.data_type == "stock")
    )
    if lock and db.bind is not None and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalars().first()


def sap_publication_allowed(
    db: Session,
    *,
    branch_id: str,
    requested_at,
    manual_precedence_hours: int,
    force: bool = False,
) -> tuple[bool, str]:
    if force:
        return True, "explicit_override"
    status = active_stock_status(db, branch_id, lock=True)
    if status is None or status.active_source_type.lower() != "manual":
        return True, "no_newer_manual_snapshot"
    published_at = status.last_success_at or status.last_updated_at
    if published_at is None:
        return False, "manual_snapshot_has_unknown_publication_time"
    if requested_at < published_at + timedelta(hours=max(0, manual_precedence_hours)):
        return False, "newer_manual_snapshot_precedence"
    return True, "manual_precedence_window_expired"


def _ensure_status(db: Session, branch_id: str) -> ReferenceUpdateStatus:
    status = active_stock_status(db, branch_id, lock=True)
    if status is None:
        status = ReferenceUpdateStatus(branch_id=branch_id, data_type="stock", status="missing")
        db.add(status)
        db.flush()
    return status


def publish_stock_snapshot(
    db: Session,
    *,
    branch_id: str,
    source_type: str,
    effective_date: date | None,
    rows: list[CanonicalStockRow],
    import_job: ReferenceImportJob,
    sap_plant: str = "",
    lgort: str = "",
    metadata: dict[str, Any] | None = None,
) -> StockSnapshot:
    if not branch_id:
        raise ValueError("branch_id is required")
    product_ids = [row.product_id for row in rows]
    if len(product_ids) != len(set(product_ids)):
        raise ValueError("stock snapshot contains ambiguous duplicate product mappings")
    if any(not row.normalized_sku or row.product_id <= 0 for row in rows):
        raise ValueError("stock snapshot contains missing product mappings")

    _lock_branch(db, branch_id)
    status = _ensure_status(db, branch_id)
    now = now_kz_naive()
    fingerprint = stock_content_fingerprint(rows)
    active_snapshot = db.get(StockSnapshot, status.active_stock_snapshot_id) if status.active_stock_snapshot_id else None
    snapshot_metadata = dict(metadata or {})
    if sap_plant:
        snapshot_metadata["sap_plant"] = sap_plant
    if lgort:
        snapshot_metadata["lgort"] = lgort
    try:
        active_metadata = json.loads(active_snapshot.metadata_json or "{}") if active_snapshot else {}
    except (TypeError, json.JSONDecodeError):
        active_metadata = {}
    if (
        active_snapshot is not None
        and active_snapshot.status == "active"
        and active_snapshot.source_type == source_type
        and active_snapshot.effective_date == effective_date
        and str(active_metadata.get("sap_plant") or "") == sap_plant
        and str(active_metadata.get("lgort") or "") == lgort
        and active_snapshot.content_fingerprint == fingerprint
        and active_snapshot.item_count == len(rows)
    ):
        status.last_updated_at = now
        status.last_success_at = now
        status.last_successful_import_job_id = import_job.id
        status.current_import_status = "success"
        status.current_import_finished_at = now
        status.error = ""
        return active_snapshot
    snapshot = StockSnapshot(
        branch_id=branch_id,
        source_type=source_type,
        effective_date=effective_date,
        status="staged",
        item_count=len(rows),
        mapped_product_count=len(rows),
        content_fingerprint=fingerprint,
        import_job_id=import_job.id,
        metadata_json=json.dumps(snapshot_metadata, ensure_ascii=False, default=str),
    )
    db.add(snapshot)
    db.flush()
    if rows:
        db.execute(
            StockSnapshotItem.__table__.insert(),
            [
                {
                    "snapshot_id": snapshot.id,
                    "ordinal": ordinal,
                    "material": row.material,
                    "normalized_sku": row.normalized_sku,
                    "product_id": row.product_id,
                    "quantity": row.quantity,
                    "raw_json": json.dumps(row.raw, ensure_ascii=False, default=str),
                }
                for ordinal, row in enumerate(rows)
            ],
        )

    db.execute(delete(BranchStock).where(BranchStock.branch_id == branch_id))
    if rows:
        db.execute(
            BranchStock.__table__.insert(),
            [
                {
                    "branch_id": branch_id,
                    "product_id": row.product_id,
                    "sku": row.normalized_sku,
                    "stock": row.quantity,
                    "source_type": source_type,
                    "snapshot_id": snapshot.id,
                    "updated_at": now,
                }
                for row in rows
            ],
        )
    db.execute(
        update(StockSnapshot)
        .where(StockSnapshot.branch_id == branch_id)
        .where(StockSnapshot.status == "active")
        .where(StockSnapshot.id != snapshot.id)
        .values(status="superseded")
    )
    snapshot.status = "active"
    snapshot.published_at = now
    status.branch_name = branch_display_name(branch_id)
    status.last_updated_at = now
    status.rows_count = len(rows)
    status.status = "success"
    status.error = ""
    status.current_import_status = "success"
    status.current_import_finished_at = now
    status.last_successful_import_job_id = import_job.id
    status.active_snapshot_product_count = len(rows)
    status.active_stock_snapshot_id = snapshot.id
    status.active_source_type = source_type
    status.stock_effective_date = effective_date
    status.last_success_at = now
    return snapshot


def _sap_catalog_materials(payload: SapBalancePayload) -> list[CatalogMaterial]:
    rows: list[CatalogMaterial] = []
    for item in payload.items:
        raw = item.raw if isinstance(item.raw, dict) else {}
        name = str(
            raw.get("materialText")
            or raw.get("material_text")
            or raw.get("materialName")
            or ""
        ).strip()
        rows.append(CatalogMaterial(code=item.material, name=name))
    return rows


def preview_sap_catalog_registration(db: Session, payload: SapBalancePayload):
    return preview_catalog_registration(
        db,
        _sap_catalog_materials(payload),
        require_exact_18_digit_codes=True,
    )


def canonical_rows_from_sap(db: Session, payload: SapBalancePayload) -> list[CanonicalStockRow]:
    if payload.plant not in SAP_PLANT_BY_CODE:
        raise ValueError(f"SAP plant is not approved: {payload.plant}")
    if payload.raw_row_count != len(payload.items):
        raise ValueError(
            f"SAP GetBalance is incomplete: rowCount={payload.raw_row_count}, items={len(payload.items)}"
        )

    grouped: dict[str, dict[str, Any]] = {}
    material_by_sku: dict[str, str] = {}
    for source_row_number, item in enumerate(payload.items, start=1):
        if item.plant != payload.plant:
            raise ValueError(
                f"SAP item {source_row_number} plant mismatch: expected {payload.plant}, "
                f"got {item.plant or 'missing'}"
            )
        if item.lgort != SAP_LGORT:
            raise ValueError(
                f"SAP item {source_row_number} LGORT mismatch: expected {SAP_LGORT}, "
                f"got {item.lgort or 'missing'}"
            )
        if not isinstance(item.material, str) or not item.material.strip():
            raise ValueError(f"SAP item {source_row_number} has an invalid material identifier")
        if not isinstance(item.quantity, Decimal) or not item.quantity.is_finite():
            raise ValueError(f"SAP item {source_row_number} has an invalid endingStockQuantity")

        material = item.material.strip()
        sku = normalize_sku(material)
        if not sku:
            raise ValueError(f"SAP item {source_row_number} has an invalid material identifier")
        previous_material = material_by_sku.setdefault(sku, material)
        if previous_material != material:
            raise ValueError(
                "SAP stock contains ambiguous material identifiers after normalization: "
                f"{previous_material}, {material}"
            )
        group = grouped.setdefault(
            sku,
            {
                "material": material,
                "quantity": Decimal("0"),
                "sap_rows": [],
            },
        )
        group["quantity"] += item.quantity
        group["sap_rows"].append(
            {
                "source_row_number": source_row_number,
                "material": material,
                "plant": item.plant,
                "lgort": item.lgort,
                "charge": item.charge,
                "quantity": str(item.quantity),
                "raw": dict(item.raw),
            }
        )

    wanted = set(grouped)
    product_ids_by_sku: dict[str, set[int]] = {}
    # Product.code is the authoritative SAP material mapping.  Normalize it
    # with the same established SKU rule and reject collisions explicitly.
    for product_id, code in db.execute(select(Product.id, Product.code)):
        sku = normalize_sku(code)
        if sku in wanted:
            product_ids_by_sku.setdefault(sku, set()).add(int(product_id))

    ambiguous = sorted(sku for sku, ids in product_ids_by_sku.items() if len(ids) > 1)
    if ambiguous:
        sample = ", ".join(ambiguous[:20])
        raise ValueError(
            f"SAP stock contains {len(ambiguous)} ambiguous internal product mappings; sample: {sample}"
        )
    missing = sorted(str(grouped[sku]["material"]) for sku in wanted if sku not in product_ids_by_sku)
    if missing:
        sample = ", ".join(missing[:20])
        raise ValueError(f"SAP stock contains {len(missing)} unmapped materials; sample: {sample}")

    rows: list[CanonicalStockRow] = []
    for sku in sorted(grouped):
        group = grouped[sku]
        rows.append(
            CanonicalStockRow(
                material=str(group["material"]),
                normalized_sku=sku,
                product_id=next(iter(product_ids_by_sku[sku])),
                quantity=group["quantity"],
                raw={
                    "aggregation": "sap_material_batches_v1",
                    "plant": payload.plant,
                    "lgort": SAP_LGORT,
                    "batch_row_count": len(group["sap_rows"]),
                    "sap_rows": group["sap_rows"],
                },
            )
        )
    return rows


def _carry_forward_absent_sap_products(
    db: Session,
    *,
    branch_id: str,
    incoming_rows: list[CanonicalStockRow],
) -> tuple[list[CanonicalStockRow], int]:
    """Retain active stock that SAP did not explicitly return.

    GetBalance has no approved contract declaring that an absent material means
    zero. Until that rule exists, absence must not erase a valid manual or SAP
    stock value. Carry-forward evidence is stored in the new snapshot and is
    reused unchanged on identical retries so fingerprint idempotency is stable.
    """
    incoming_product_ids = {row.product_id for row in incoming_rows}
    status = active_stock_status(db, branch_id, lock=True)
    prior_items: dict[int, tuple[str, str, Decimal | None, dict[str, Any]]] = {}
    if status is not None and status.active_stock_snapshot_id:
        for material, sku, product_id, quantity, raw_json in db.execute(
            select(
                StockSnapshotItem.material,
                StockSnapshotItem.normalized_sku,
                StockSnapshotItem.product_id,
                StockSnapshotItem.quantity,
                StockSnapshotItem.raw_json,
            ).where(StockSnapshotItem.snapshot_id == status.active_stock_snapshot_id)
        ):
            if product_id is None:
                continue
            try:
                raw = json.loads(raw_json or "{}")
            except (TypeError, json.JSONDecodeError):
                raw = {}
            prior_items[int(product_id)] = (
                str(material),
                str(sku),
                Decimal(str(quantity)) if quantity is not None else None,
                raw if isinstance(raw, dict) else {},
            )

    carried: list[CanonicalStockRow] = []
    for product_id, sku, quantity, snapshot_id in db.execute(
        select(BranchStock.product_id, BranchStock.sku, BranchStock.stock, BranchStock.snapshot_id)
        .where(BranchStock.branch_id == branch_id)
        .order_by(BranchStock.product_id)
    ):
        product_key = int(product_id)
        if product_key in incoming_product_ids:
            continue
        prior = prior_items.get(product_key)
        prior_raw = prior[3] if prior is not None else {}
        if prior_raw.get("carry_forward"):
            raw = prior_raw
        else:
            raw = {
                "carry_forward": True,
                "reason": "sap_material_absent_without_approved_removal_rule",
                "source_snapshot_id": int(snapshot_id) if snapshot_id is not None else None,
                "source_raw": prior_raw,
            }
        carried.append(
            CanonicalStockRow(
                material=prior[0] if prior is not None else str(sku),
                normalized_sku=prior[1] if prior is not None else str(sku),
                product_id=product_key,
                quantity=prior[2] if prior is not None else (Decimal(str(quantity)) if quantity is not None else None),
                raw=raw,
            )
        )
    return sorted([*incoming_rows, *carried], key=lambda row: (row.normalized_sku, row.product_id)), len(carried)


def import_sap_balance_payload(
    db: Session,
    *,
    branch_id: str,
    payload: SapBalancePayload,
    user_name: str,
    manual_precedence_hours: int,
    force: bool = False,
    approve_catalog_registration: bool = False,
    payload_sha256: str = "",
) -> ReferenceImportJob:
    now = now_kz_naive()
    job = ReferenceImportJob(
        data_type="stock",
        branch_ids_json=json.dumps([branch_id], ensure_ascii=False),
        filename=f"SAP_GetBalance_{payload.plant}_{payload.effective_date.isoformat()}.json",
        source_type="sap",
        status="running",
        rows_total=payload.raw_row_count,
        started_at=now,
        user_name=user_name,
    )
    db.add(job)
    db.flush()
    try:
        _lock_branch(db, branch_id)
        allowed, reason = sap_publication_allowed(
            db,
            branch_id=branch_id,
            requested_at=now,
            manual_precedence_hours=manual_precedence_hours,
            force=force,
        )
        if not allowed:
            raise ValueError(f"SAP publication blocked: {reason}")
        catalog_preview = preview_sap_catalog_registration(db, payload)
        created_products = register_catalog_preview(
            db,
            catalog_preview,
            approved=approve_catalog_registration,
        )
        sap_rows = canonical_rows_from_sap(db, payload)
        rows, carried_product_count = _carry_forward_absent_sap_products(
            db,
            branch_id=branch_id,
            incoming_rows=sap_rows,
        )
        snapshot = publish_stock_snapshot(
            db,
            branch_id=branch_id,
            source_type="sap",
            effective_date=payload.effective_date,
            rows=rows,
            import_job=job,
            sap_plant=payload.plant,
            lgort=SAP_LGORT,
            metadata={
                "publication_precedence": reason,
                "raw_row_count": payload.raw_row_count,
                "aggregated_material_count": len(sap_rows),
                "carried_forward_product_count": carried_product_count,
                "absent_material_policy": "carry_forward_pending_business_decision",
                "catalog_registration_approved": bool(approve_catalog_registration),
                "catalog_existing_product_count": len(catalog_preview.existing),
                "catalog_created_product_count": len(created_products),
                "catalog_created_product_ids": [int(row.id) for row in created_products],
                "payload_sha256": payload_sha256,
            },
        )
        job.status = "success"
        job.rows_success = payload.raw_row_count
        job.rows_failed = 0
        job.finished_at = now_kz_naive()
        job.log_json = json.dumps(
            [{
                "snapshot_id": snapshot.id,
                "catalog_registration_approved": bool(approve_catalog_registration),
                "catalog_created_product_count": len(created_products),
                "catalog_created_product_ids": [int(row.id) for row in created_products],
                "payload_sha256": payload_sha256,
            }],
            ensure_ascii=False,
        )
        db.commit()
        db.refresh(job)
        return job
    except Exception as exc:
        db.rollback()
        failed = ReferenceImportJob(
            data_type="stock",
            branch_ids_json=json.dumps([branch_id], ensure_ascii=False),
            filename=f"SAP_GetBalance_{payload.plant}_{payload.effective_date.isoformat()}.json",
            source_type="sap",
            status="error",
            rows_total=payload.raw_row_count,
            rows_success=0,
            rows_failed=payload.raw_row_count,
            error=str(exc),
            log_json=json.dumps([{"error": str(exc)}], ensure_ascii=False),
            started_at=now,
            finished_at=now_kz_naive(),
            user_name=user_name,
        )
        db.add(failed)
        status = _ensure_status(db, branch_id)
        status.current_import_status = "error"
        status.current_import_started_at = now
        status.current_import_finished_at = failed.finished_at
        status.last_failure_at = failed.finished_at
        status.last_failure_reason = str(exc)
        status.error = str(exc)
        db.commit()
        db.refresh(failed)
        return failed


def record_sap_import_failure(
    db: Session,
    *,
    branch_id: str,
    plant: str,
    effective_date: date,
    user_name: str,
    error: str,
) -> ReferenceImportJob:
    now = now_kz_naive()
    _lock_branch(db, branch_id)
    job = ReferenceImportJob(
        data_type="stock",
        branch_ids_json=json.dumps([branch_id], ensure_ascii=False),
        filename=f"SAP_GetBalance_{plant}_{effective_date.isoformat()}.json",
        source_type="sap",
        status="error",
        error=error,
        log_json=json.dumps([{"error": error}], ensure_ascii=False),
        started_at=now,
        finished_at=now,
        user_name=user_name,
    )
    db.add(job)
    status = _ensure_status(db, branch_id)
    status.current_import_status = "error"
    status.current_import_started_at = now
    status.current_import_finished_at = now
    status.last_failure_at = now
    status.last_failure_reason = error
    status.error = error
    db.commit()
    db.refresh(job)
    return job


def snapshot_quantities(db: Session, snapshot_id: int) -> dict[int, Decimal | None]:
    snapshot = db.get(StockSnapshot, snapshot_id)
    if snapshot is None or snapshot.status not in {"active", "superseded"}:
        raise ValueError("stock snapshot provenance is missing or invalid")
    result: dict[int, Decimal | None] = {}
    for product_id, quantity in db.execute(
        select(StockSnapshotItem.product_id, StockSnapshotItem.quantity)
        .where(StockSnapshotItem.snapshot_id == snapshot_id)
        .where(StockSnapshotItem.product_id.is_not(None))
    ):
        key = int(product_id)
        if key in result:
            raise ValueError("stock snapshot has ambiguous product mappings")
        result[key] = Decimal(str(quantity)) if quantity is not None else None
    return result
