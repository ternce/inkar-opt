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
from .sap_balance import SapBalancePayload
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
        digest.update(
            json.dumps(
                [ordinal, row.material, row.normalized_sku, row.product_id, _decimal_token(row.quantity)],
                ensure_ascii=False,
                separators=(",", ":"),
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
    if (
        active_snapshot is not None
        and active_snapshot.status == "active"
        and active_snapshot.source_type == source_type
        and active_snapshot.effective_date == effective_date
        and active_snapshot.sap_plant == sap_plant
        and active_snapshot.lgort == lgort
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
        sap_plant=sap_plant,
        lgort=lgort,
        effective_date=effective_date,
        status="staged",
        item_count=len(rows),
        mapped_product_count=len(rows),
        content_fingerprint=fingerprint,
        import_job_id=import_job.id,
        metadata_json=json.dumps(metadata or {}, ensure_ascii=False, default=str),
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


def canonical_rows_from_sap(db: Session, payload: SapBalancePayload) -> list[CanonicalStockRow]:
    normalized = [normalize_sku(item.material) for item in payload.items]
    wanted = {sku for sku in normalized if sku}
    products = db.execute(select(Product.id, Product.code).where(Product.code.in_(wanted))).all() if wanted else []
    product_by_sku = {str(code): int(product_id) for product_id, code in products}
    missing: list[str] = []
    rows: list[CanonicalStockRow] = []
    for item, sku in zip(payload.items, normalized):
        if not sku or sku not in product_by_sku:
            missing.append(item.material)
            continue
        rows.append(
            CanonicalStockRow(
                material=item.material,
                normalized_sku=sku,
                product_id=product_by_sku[sku],
                quantity=item.quantity,
                raw=item.raw,
            )
        )
    if missing:
        sample = ", ".join(missing[:20])
        raise ValueError(f"SAP stock contains {len(missing)} unmapped materials; sample: {sample}")
    return rows


def import_sap_balance_payload(
    db: Session,
    *,
    branch_id: str,
    payload: SapBalancePayload,
    user_name: str,
    manual_precedence_hours: int,
    force: bool = False,
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
        rows = canonical_rows_from_sap(db, payload)
        snapshot = publish_stock_snapshot(
            db,
            branch_id=branch_id,
            source_type="sap",
            effective_date=payload.effective_date,
            rows=rows,
            import_job=job,
            sap_plant=payload.plant,
            lgort="1000",
            metadata={"publication_precedence": reason, "raw_row_count": payload.raw_row_count},
        )
        job.status = "success"
        job.rows_success = len(rows)
        job.rows_failed = 0
        job.finished_at = now_kz_naive()
        job.log_json = json.dumps([{"snapshot_id": snapshot.id}], ensure_ascii=False)
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
