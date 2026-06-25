from __future__ import annotations

import csv
import io
import json

from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import CalculatedPrice, PriceList, PricingWorkflowRun, Product
from ..pricing import calculate_price_zone
from .analytics import analytics_for_run


def _export_zone(cp: CalculatedPrice) -> str:
    zone, _reference, _deviation = calculate_price_zone(
        cp.final_price,
        chosen_competitor_price=cp.chosen_competitor_price,
        lowest_competitor_price=cp.lowest_competitor_price if cp.lowest_competitor_price is not None else cp.competitor_price,
    )
    return zone or "no-data"


def _price_rows(*, db: Session, run: PricingWorkflowRun) -> list[dict]:
    if not run.price_list_id:
        return []
    rows = (
        db.execute(
            select(CalculatedPrice, Product)
            .join(Product, Product.id == CalculatedPrice.product_id)
            .where(CalculatedPrice.price_list_id == run.price_list_id)
            .order_by(Product.name.asc())
        )
        .all()
    )
    return [
        {
            "SKU": product.code,
            "Название": product.name,
            "Себестоимость": float(cp.cost),
            "МДЦ": float(cp.base_price),
            "Цена конкурента": float(cp.competitor_price) if cp.competitor_price is not None else "",
            "Цена после прогиба": float(cp.price_from_competitor) if cp.price_from_competitor is not None else "",
            "Итоговая цена": float(cp.final_price),
            "Зона": _export_zone(cp),
            "Причина": cp.applied_reason,
        }
        for cp, product in rows
    ]


def _analytics_rows(analytics: dict) -> list[dict]:
    rows: list[dict] = []
    summary = analytics.get("summary") if isinstance(analytics.get("summary"), dict) else {}
    for key, value in summary.items():
        rows.append({"Раздел": "summary", "Показатель": key, "Значение": value})
    for item in analytics.get("zones") or []:
        rows.append({"Раздел": "zones", "Показатель": item.get("label") or item.get("name"), "Значение": item.get("value")})
    for item in analytics.get("markupHistogram") or []:
        rows.append({"Раздел": "markup", "Показатель": item.get("bucket"), "Значение": item.get("value")})
    for item in analytics.get("competitorUsage") or []:
        rows.append({"Раздел": "competitor_usage", "Показатель": item.get("source"), "Значение": item.get("skuCount")})
    for item in analytics.get("percentileUsage") or []:
        rows.append({"Раздел": "percentile_usage", "Показатель": item.get("source"), "Значение": item.get("skuCount")})
    return rows


def export_workflow_run(*, db: Session, run_id: int, fmt: str = "csv", include: str = "price") -> tuple[str, bytes, str]:
    run = db.get(PricingWorkflowRun, run_id)
    if run is None:
        raise ValueError("workflow run not found")
    fmt = (fmt or "csv").strip().lower()
    include = (include or "price").strip().lower()
    if fmt not in {"csv", "xlsx"}:
        raise ValueError("format must be csv or xlsx")

    if include == "analytics":
        rows = _analytics_rows(analytics_for_run(db=db, run=run))
    elif include == "pricing_reasons":
        rows = [row for row in _price_rows(db=db, run=run)]
    elif include == "competitor_details":
        rows = [
            {"Тип": "competitor", **item}
            for item in (json.loads(run.competitor_sources_json or "[]") if run.competitor_sources_json else [])
            if isinstance(item, dict)
        ] + [
            {"Тип": "percentile", **item}
            for item in (json.loads(run.percentile_sources_json or "[]") if run.percentile_sources_json else [])
            if isinstance(item, dict)
        ]
    else:
        rows = _price_rows(db=db, run=run)

    filename = f"pricing_workflow_{run.id}_{include}.{fmt}"
    if fmt == "csv":
        buffer = io.StringIO()
        fieldnames = list(rows[0].keys()) if rows else ["empty"]
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        return filename, buffer.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"

    wb = Workbook()
    ws = wb.active
    ws.title = include[:31] or "export"
    headers = list(rows[0].keys()) if rows else ["empty"]
    ws.append(headers)
    for row in rows:
        ws.append([row.get(header, "") for header in headers])
    bio = io.BytesIO()
    wb.save(bio)
    return filename, bio.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
