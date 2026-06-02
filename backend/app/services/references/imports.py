from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import (
    BranchCost,
    BranchStock,
    Counterparty,
    DeliveryPoint,
    Holding,
    Product,
    ProductExtra,
    ProductRating,
    ReferenceImportJob,
    ReferenceUpdateStatus,
)
from ..sku import normalize_sku
from .parsers import as_decimal, as_int, parse_excel_rows
from .types import BRANCH_BY_ID, REFERENCE_TYPE_BY_CODE
from .validators import required_columns_for


def _branch_name(branch_id: str) -> str:
    return BRANCH_BY_ID.get(str(branch_id), {}).get("name", str(branch_id))


def _get_or_create_product(db: Session, sku: str, name: str | None = None) -> Product:
    row = db.execute(select(Product).where(Product.code == sku)).scalars().first()
    if row is None:
        row = Product(code=sku, name=name or sku, cost=0)
        db.add(row)
        db.flush()
    elif name and not (row.name or "").strip():
        row.name = name
    return row


def _get_extra(db: Session, product_id: int) -> ProductExtra:
    row = db.get(ProductExtra, product_id)
    if row is None:
        row = ProductExtra(product_id=product_id)
        db.add(row)
        db.flush()
    return row


def _upsert_status(
    *,
    db: Session,
    branch_id: str,
    data_type: str,
    rows_count: int,
    status: str,
    error: str = "",
) -> None:
    row = (
        db.execute(
            select(ReferenceUpdateStatus)
            .where(ReferenceUpdateStatus.branch_id == branch_id)
            .where(ReferenceUpdateStatus.data_type == data_type)
        )
        .scalars()
        .first()
    )
    if row is None:
        row = ReferenceUpdateStatus(branch_id=branch_id, data_type=data_type)
        db.add(row)
    row.branch_name = _branch_name(branch_id)
    row.last_updated_at = datetime.utcnow() if status != "running" else row.last_updated_at
    row.rows_count = rows_count
    row.status = status
    row.error = error


def _branch_ids_from_request(branch_ids: list[str], row: dict) -> list[str]:
    raw_branch = row.get("branch_id") or row.get("branch_name")
    if raw_branch not in (None, ""):
        text = str(raw_branch).strip()
        matched = next((branch["id"] for branch in BRANCH_BY_ID.values() if branch["name"].casefold() == text.casefold()), None)
        return [matched or text]
    return branch_ids


def _upsert_branch_stock(db: Session, *, branch_id: str, product: Product, sku: str, stock: Decimal | None) -> None:
    row = (
        db.execute(select(BranchStock).where(BranchStock.branch_id == branch_id).where(BranchStock.product_id == product.id))
        .scalars()
        .first()
    )
    if row is None:
        row = BranchStock(branch_id=branch_id, product_id=product.id, sku=sku)
        db.add(row)
    row.stock = float(stock) if stock is not None else None
    row.source_type = "excel"
    row.updated_at = datetime.utcnow()


def _upsert_branch_cost(db: Session, *, branch_id: str, product: Product, sku: str, cost: Decimal | None) -> None:
    row = (
        db.execute(select(BranchCost).where(BranchCost.branch_id == branch_id).where(BranchCost.product_id == product.id))
        .scalars()
        .first()
    )
    if row is None:
        row = BranchCost(branch_id=branch_id, product_id=product.id, sku=sku)
        db.add(row)
    row.cost = float(cost) if cost is not None else None
    row.source_type = "excel"
    row.updated_at = datetime.utcnow()


def _upsert_rating(db: Session, *, branch_id: str, product: Product, sku: str, rating_type: str, rating: int | None) -> None:
    row = (
        db.execute(
            select(ProductRating)
            .where(ProductRating.branch_id == branch_id)
            .where(ProductRating.product_id == product.id)
            .where(ProductRating.rating_type == rating_type)
        )
        .scalars()
        .first()
    )
    if row is None:
        row = ProductRating(branch_id=branch_id, product_id=product.id, sku=sku, rating_type=rating_type)
        db.add(row)
    row.rating = rating
    row.source_type = "excel"
    row.updated_at = datetime.utcnow()


def import_reference_excel(
    *,
    db: Session,
    data_type: str,
    branch_ids: list[str],
    content: bytes,
    filename: str,
    user_name: str = "",
) -> ReferenceImportJob:
    if data_type not in REFERENCE_TYPE_BY_CODE:
        raise ValueError("unknown data_type")
    if not branch_ids:
        raise ValueError("branch_ids is required")

    branch_ids = [str(x).strip() for x in branch_ids if str(x).strip()]
    job = ReferenceImportJob(
        data_type=data_type,
        branch_ids_json=json.dumps(branch_ids, ensure_ascii=False),
        filename=filename,
        source_type="excel",
        status="running",
        started_at=datetime.utcnow(),
        user_name=user_name,
    )
    db.add(job)
    db.flush()
    for branch_id in branch_ids:
        _upsert_status(db=db, branch_id=branch_id, data_type=data_type, rows_count=0, status="running")
    db.commit()

    logs: list[dict] = []
    success_by_branch = {branch_id: 0 for branch_id in branch_ids}
    try:
        rows, headers = parse_excel_rows(content)
        required = required_columns_for(data_type)
        missing = [key for key in required if key not in headers]
        if missing:
            raise ValueError(f"Не найдены обязательные колонки: {', '.join(missing)}")

        total = len(rows)
        success = 0
        failed = 0
        for raw in rows:
            row_no = raw.get("_row")
            try:
                sku = normalize_sku(raw.get("sku")) if raw.get("sku") not in (None, "") else None
                row_branch_ids = _branch_ids_from_request(branch_ids, raw)

                if data_type in {"stock", "cost", "rating_global", "rating_local", "products"}:
                    if not sku:
                        raise ValueError("empty SKU")
                    product = _get_or_create_product(db, sku, str(raw.get("name") or "").strip() or None)
                    extra = _get_extra(db, product.id)

                    if data_type == "products":
                        product.name = str(raw.get("name") or product.name or sku).strip()
                        manufacturer = str(raw.get("manufacturer") or "").strip()
                        if manufacturer:
                            extra.manufacturer = manufacturer
                        extra.updated_at = datetime.utcnow()
                        for branch_id in row_branch_ids:
                            success_by_branch.setdefault(branch_id, 0)
                            success_by_branch[branch_id] += 1

                    elif data_type == "stock":
                        stock = as_decimal(raw.get("stock"))
                        for branch_id in row_branch_ids:
                            _upsert_branch_stock(db, branch_id=branch_id, product=product, sku=sku, stock=stock)
                            if len(row_branch_ids) == 1:
                                extra.stock = float(stock) if stock is not None else None
                                extra.updated_at = datetime.utcnow()
                            success_by_branch.setdefault(branch_id, 0)
                            success_by_branch[branch_id] += 1

                    elif data_type == "cost":
                        cost = as_decimal(raw.get("cost"))
                        if cost is not None:
                            product.cost = float(cost)
                        for branch_id in row_branch_ids:
                            _upsert_branch_cost(db, branch_id=branch_id, product=product, sku=sku, cost=cost)
                            success_by_branch.setdefault(branch_id, 0)
                            success_by_branch[branch_id] += 1

                    elif data_type == "rating_global":
                        rating = as_int(raw.get("rating_global"))
                        for branch_id in row_branch_ids:
                            _upsert_rating(db, branch_id=branch_id, product=product, sku=sku, rating_type="global", rating=rating)
                            success_by_branch.setdefault(branch_id, 0)
                            success_by_branch[branch_id] += 1
                        if rating is not None:
                            product.top_rank = rating

                    elif data_type == "rating_local":
                        rating = as_int(raw.get("rating_local"))
                        for branch_id in row_branch_ids:
                            _upsert_rating(db, branch_id=branch_id, product=product, sku=sku, rating_type="local", rating=rating)
                            success_by_branch.setdefault(branch_id, 0)
                            success_by_branch[branch_id] += 1

                elif data_type == "holdings":
                    name = str(raw.get("name") or "").strip()
                    if not name:
                        raise ValueError("empty name")
                    for branch_id in row_branch_ids:
                        db.add(Holding(external_id=str(raw.get("external_id") or ""), name=name, branch_id=branch_id, source_type="excel"))
                        success_by_branch.setdefault(branch_id, 0)
                        success_by_branch[branch_id] += 1

                elif data_type == "counterparties":
                    name = str(raw.get("name") or "").strip()
                    if not name:
                        raise ValueError("empty name")
                    for branch_id in row_branch_ids:
                        db.add(
                            Counterparty(
                                external_id=str(raw.get("external_id") or ""),
                                name=name,
                                holding_id=str(raw.get("holding_id") or ""),
                                branch_id=branch_id,
                                source_type="excel",
                            )
                        )
                        success_by_branch.setdefault(branch_id, 0)
                        success_by_branch[branch_id] += 1

                elif data_type == "delivery_points":
                    name = str(raw.get("name") or "").strip()
                    if not name:
                        raise ValueError("empty name")
                    for branch_id in row_branch_ids:
                        db.add(
                            DeliveryPoint(
                                external_id=str(raw.get("external_id") or ""),
                                counterparty_id=str(raw.get("counterparty_id") or ""),
                                name=name,
                                address=str(raw.get("address") or ""),
                                branch_id=branch_id,
                                source_type="excel",
                            )
                        )
                        success_by_branch.setdefault(branch_id, 0)
                        success_by_branch[branch_id] += 1

                success += 1
            except Exception as exc:
                failed += 1
                logs.append({"row": row_no, "error": str(exc)})

        job.rows_total = total
        job.rows_success = success
        job.rows_failed = failed
        job.status = "success" if failed == 0 else "partial"
        job.log_json = json.dumps(logs[:500], ensure_ascii=False)
        job.finished_at = datetime.utcnow()
        for branch_id, count in success_by_branch.items():
            _upsert_status(db=db, branch_id=branch_id, data_type=data_type, rows_count=count, status=job.status)
        db.commit()
        db.refresh(job)
        return job
    except Exception as exc:
        db.rollback()
        job = db.get(ReferenceImportJob, job.id)
        if job is not None:
            job.status = "error"
            job.error = str(exc)
            job.finished_at = datetime.utcnow()
            job.log_json = json.dumps(logs[:500], ensure_ascii=False)
        for branch_id in branch_ids:
            _upsert_status(db=db, branch_id=branch_id, data_type=data_type, rows_count=0, status="error", error=str(exc))
        db.commit()
        if job is None:
            raise
        db.refresh(job)
        return job

