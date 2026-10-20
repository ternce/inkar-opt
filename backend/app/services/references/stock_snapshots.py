from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from ...models import (
    BranchStock,
    ReferenceImportJob,
    ReferenceUpdateStatus,
    StockSnapshot,
    StockSnapshotItem,
)
from ...timezone import now_kz_naive
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
