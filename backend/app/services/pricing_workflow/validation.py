from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import (
    BranchCost,
    BranchStock,
    CompetitorPriceList,
    PriceFormat,
    PricingContext,
    Product,
    ProductRating,
    ReferenceUpdateStatus,
)
from ..competitor_assignments import get_assigned_competitor_price_lists


def _days_old(value: date | datetime | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        d = value.date()
    else:
        d = value
    return max(0, (date.today() - d).days)


def _status(kind: str, label: str, ok: bool, message: str, severity: str = "info") -> dict:
    return {"kind": kind, "label": label, "ok": ok, "severity": "ok" if ok else severity, "message": message}


def build_workflow_status(
    *,
    db: Session,
    pricing_context_id: int,
    price_format_id: int,
    competitor_source_ids: list[int] | None = None,
) -> dict:
    context = db.get(PricingContext, pricing_context_id)
    pf = db.get(PriceFormat, price_format_id)
    if context is None or pf is None:
        return {"warnings": [_status("setup", "Контекст", False, "Не выбран контекст или ценовой формат", "error")], "items": []}

    competitor_source_ids = [int(x) for x in (competitor_source_ids or []) if str(x).strip().lstrip("-").isdigit()]
    if competitor_source_ids:
        competitor_lists = db.execute(select(CompetitorPriceList).where(CompetitorPriceList.id.in_(competitor_source_ids))).scalars().all()
    else:
        competitor_lists = [item.price_list for item in get_assigned_competitor_price_lists(db=db, price_format_id=int(pf.id))]

    oldest_competitor_days = None
    outdated_competitors = 0
    missing_price_dates = 0
    for row in competitor_lists:
        days = _days_old(row.price_date)
        if days is None:
            missing_price_dates += 1
            continue
        oldest_competitor_days = days if oldest_competitor_days is None else max(oldest_competitor_days, days)
        if days > 2:
            outdated_competitors += 1

    ref_rows = {
        row.data_type: row
        for row in db.execute(
            select(ReferenceUpdateStatus).where(ReferenceUpdateStatus.branch_id == str(context.branch_id))
        ).scalars().all()
    }
    product_count = int(db.execute(select(func.count(Product.id))).scalar() or 0)
    stock_count = int(
        db.execute(select(func.count(BranchStock.id)).where(BranchStock.branch_id == str(context.branch_id))).scalar() or 0
    )
    cost_count = int(
        db.execute(select(func.count(BranchCost.id)).where(BranchCost.branch_id == str(context.branch_id))).scalar() or 0
    )
    local_rating_count = int(
        db.execute(
            select(func.count(ProductRating.id))
            .where(ProductRating.branch_id == str(context.branch_id))
            .where(ProductRating.rating_type == "local")
        ).scalar()
        or 0
    )

    items = [
        _status(
            "competitors",
            "Цены конкурентов",
            bool(competitor_lists) and outdated_competitors == 0 and missing_price_dates == 0,
            (
                f"Выбрано источников: {len(competitor_lists)}. Самые старые цены: {oldest_competitor_days} дн."
                if competitor_lists and oldest_competitor_days is not None
                else f"Выбрано источников: {len(competitor_lists)}. У {missing_price_dates} нет даты цен."
            ),
            "warning",
        ),
        _status("products", "Номенклатура", product_count > 0, f"Товаров в базе: {product_count}", "error"),
        _status("stock", "Остатки", stock_count > 0, f"Строк остатков по филиалу: {stock_count}", "warning"),
        _status("cost", "Себестоимость", cost_count > 0 or product_count > 0, f"Строк себестоимости по филиалу: {cost_count}", "warning"),
        _status("rating_local", "Локальный рейтинг", local_rating_count > 0, f"Строк локального рейтинга: {local_rating_count}", "warning"),
    ]

    for code, label in (("stock", "Статус остатков"), ("cost", "Статус себестоимости"), ("rating_local", "Статус локального рейтинга")):
        row = ref_rows.get(code)
        if row and row.last_updated_at:
            days = _days_old(row.last_updated_at)
            items.append(
                _status(
                    f"reference_{code}",
                    label,
                    days is not None and days <= 2,
                    f"Последнее обновление: {row.last_updated_at.isoformat()} ({days} дн.)",
                    "warning",
                )
            )

    warnings = [item for item in items if not item["ok"]]
    return {
        "context": {"id": context.id, "name": context.name, "branchId": context.branch_id, "region": context.region},
        "priceFormat": {"id": pf.id, "code": pf.code, "name": pf.name, "pricingRuleId": pf.pricing_rule_id},
        "items": items,
        "warnings": warnings,
        "canGenerate": product_count > 0 and bool(competitor_lists),
    }
