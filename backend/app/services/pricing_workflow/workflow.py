from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import (
    CalculatedPrice,
    CompetitorPriceList,
    PriceFormat,
    PriceList,
    PricingContext,
    PricingWorkflowRun,
    Product,
)
from ..competitor_matching import rebuild_competitor_prices_for_selected
from ..competitor_percentiles import recalculate_competitor_percentiles
from ..competitor_price_lists import list_competitor_price_lists, save_selected_competitor_price_lists_only
from ..competitor_assignments import get_assigned_competitor_price_lists
from ..pricing import AMBIGUOUS_LIST_TYPES, calculate_price_zone, calculate_prices
from ...timezone import local_iso, now_kz_naive
from .analytics import build_workflow_analytics
from .snapshot import build_generate_snapshot, dumps_snapshot, loads_snapshot


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: str | None, fallback: object) -> object:
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _apply_snapshot(target: object, snapshot: dict) -> None:
    setattr(target, "generated_by", str(snapshot.get("generatedBy") or ""))
    setattr(target, "run_sources_json", dumps_snapshot(snapshot.get("runSources") or {}))
    setattr(target, "run_rule_json", dumps_snapshot(snapshot.get("runRule") or {}))
    setattr(target, "run_lists_json", dumps_snapshot(snapshot.get("runLists") or []))
    setattr(target, "run_reference_versions_json", dumps_snapshot(snapshot.get("runReferenceVersions") or {}))
    setattr(target, "run_percentile_config_json", dumps_snapshot(snapshot.get("runPercentileConfig") or {}))
    setattr(target, "run_snapshot_json", dumps_snapshot(snapshot))


def price_format_to_workflow_dict(row: PriceFormat) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "branch": row.branch,
        "pricingRule": row.pricing_rule,
        "pricingRuleId": row.pricing_rule_id,
        "status": "active",
        "updatedAt": local_iso(row.created_at) if row.created_at else "",
    }


def list_workflow_price_formats(*, db: Session) -> list[dict]:
    rows = db.execute(select(PriceFormat).order_by(PriceFormat.code.asc())).scalars().all()
    return [price_format_to_workflow_dict(row) for row in rows]


def _unique_price_list_number(*, db: Session, requested: str, run_id: int) -> str:
    base = requested.strip()
    if not base:
        base = f"price_list_wf{run_id}"
    if db.execute(select(PriceList.id).where(PriceList.number == base)).scalar() is None:
        return base
    candidate = f"{base}_wf{run_id}"
    if db.execute(select(PriceList.id).where(PriceList.number == candidate)).scalar() is None:
        return candidate
    idx = 2
    while True:
        candidate = f"{base}_wf{run_id}_{idx}"
        if db.execute(select(PriceList.id).where(PriceList.number == candidate)).scalar() is None:
            return candidate
        idx += 1


def create_workflow_run(*, db: Session, payload: dict) -> PricingWorkflowRun:
    context_id = int(payload.get("pricing_context_id") or payload.get("pricingContextId") or 0)
    price_format_id = int(payload.get("price_format_id") or payload.get("priceFormatId") or 0)
    context = db.get(PricingContext, context_id)
    pf = db.get(PriceFormat, price_format_id)
    if context is None:
        raise ValueError("pricing context not found")
    if pf is None:
        raise ValueError("price format not found")

    competitor_sources = payload.get("competitor_sources") or payload.get("competitorSources") or []
    percentile_sources = payload.get("percentile_sources") or payload.get("percentileSources") or []
    if not isinstance(competitor_sources, list):
        raise ValueError("competitor_sources must be list")
    if not isinstance(percentile_sources, list):
        raise ValueError("percentile_sources must be list")
    user = str(payload.get("user") or "Workflow").strip()

    selected_ids: list[int] = []
    coefficients: dict[int, float] = {}
    for item in competitor_sources:
        if not isinstance(item, dict) or item.get("enabled") is False:
            continue
        source_id = int(item.get("id") or 0)
        if source_id <= 0:
            continue
        selected_ids.append(source_id)
        try:
            coefficients[source_id] = float(item.get("coefficient") or 1)
        except Exception:
            coefficients[source_id] = 1.0
    if not selected_ids:
        raise ValueError("select at least one competitor source")

    run = PricingWorkflowRun(
        pricing_context_id=context.id,
        price_format_id=pf.id,
        pricing_rule_id=pf.pricing_rule_id,
        competitor_sources_json=_json(competitor_sources),
        percentile_sources_json=_json(percentile_sources),
        generated_by=user,
        started_at=now_kz_naive(),
        status="running",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        save_selected_competitor_price_lists_only(
            db=db,
            price_format_code=pf.code,
            selected_ids=selected_ids,
            coefficients=coefficients,
        )
        selected_count = len(get_assigned_competitor_price_lists(db=db, price_format_id=int(pf.id)))
        if selected_count <= 0:
            raise ValueError("selected competitor sources are not available for calculation")
        percentile_mode = (pf.competitor_price_mode or "regular") == "percentile"
        if not percentile_mode:
            rebuild_competitor_prices_for_selected(db=db, price_format_id=pf.id, commit_between_lists=True)
            recalculate_competitor_percentiles(db=db, price_format_id=pf.id)
        db.commit()

        activation_date = None
        raw_activation = payload.get("activation_date") or payload.get("activationDate")
        if isinstance(raw_activation, str) and raw_activation.strip():
            activation_date = date.fromisoformat(raw_activation.strip())
        as_of = activation_date or date.today()
        price_list_number = str(payload.get("price_list_number") or payload.get("priceListNumber") or "").strip()
        if not price_list_number:
            price_list_number = f"{pf.code}_{context.branch_id}_{as_of.isoformat()}_wf{run.id}"
        price_list_number = _unique_price_list_number(db=db, requested=price_list_number, run_id=int(run.id))
        region_id = int(context.branch_id) if str(context.branch_id).isdigit() else None

        snapshot = build_generate_snapshot(
            db=db,
            price_format=pf,
            branch_id=str(context.branch_id or ""),
            as_of=as_of,
            generated_by=user,
            activation_date=activation_date,
            requested_competitor_sources=competitor_sources,
            requested_percentile_sources=percentile_sources,
        )
        _apply_snapshot(run, snapshot)
        db.commit()

        calculated_count = calculate_prices(
            db=db,
            price_format_code=pf.code,
            price_list_number=price_list_number,
            as_of=as_of,
            activation_date=activation_date,
            user=user,
            region_id=region_id,
            force_new_price_list=True,
        )
        if calculated_count <= 0:
            raise ValueError("Нет товаров для расчёта. Сначала загрузите номенклатуру.")

        pl = db.execute(select(PriceList).where(PriceList.number == price_list_number)).scalars().first()
        if pl is None:
            raise ValueError("price list was not created")
        _apply_snapshot(pl, snapshot)
        db.flush()
        analytics = build_workflow_analytics(db=db, price_list_id=pl.id)
        run.price_list_id = pl.id
        run.price_list_number = pl.number
        _apply_snapshot(run, snapshot)
        run.analytics_json = _json(analytics)
        run.status = "success"
        run.finished_at = now_kz_naive()
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:
        db.rollback()
        run = db.get(PricingWorkflowRun, run.id)
        if run is not None:
            run.status = "error"
            run.error = str(exc)
            run.finished_at = now_kz_naive()
            db.commit()
            db.refresh(run)
            return run
        raise


def run_to_dict(*, db: Session, run: PricingWorkflowRun, include_items: bool = False) -> dict:
    context = db.get(PricingContext, run.pricing_context_id)
    pf = db.get(PriceFormat, run.price_format_id)
    analytics = build_workflow_analytics(db=db, price_list_id=run.price_list_id) if run.price_list_id else _loads(run.analytics_json, {})
    out = {
        "id": run.id,
        "pricingContextId": run.pricing_context_id,
        "priceFormatId": run.price_format_id,
        "pricingRuleId": run.pricing_rule_id,
        "priceListNumber": run.price_list_number,
        "startedAt": local_iso(run.started_at) if run.started_at else "",
        "finishedAt": local_iso(run.finished_at) if run.finished_at else "",
        "status": run.status,
        "error": run.error,
        "context": {"name": context.name, "region": context.region, "salesChannel": context.sales_channel} if context else None,
        "priceFormat": price_format_to_workflow_dict(pf) if pf else None,
        "competitorSources": _loads(run.competitor_sources_json, []),
        "percentileSources": _loads(run.percentile_sources_json, []),
        "snapshot": loads_snapshot(run.run_snapshot_json, {}),
        "runSources": loads_snapshot(run.run_sources_json, {}),
        "runRule": loads_snapshot(run.run_rule_json, {}),
        "runLists": loads_snapshot(run.run_lists_json, []),
        "runReferenceVersions": loads_snapshot(run.run_reference_versions_json, {}),
        "runPercentileConfig": loads_snapshot(run.run_percentile_config_json, {}),
        "analytics": analytics,
    }
    if include_items and run.price_list_id:
        rows = (
            db.execute(
                select(CalculatedPrice, Product)
                .join(Product, Product.id == CalculatedPrice.product_id)
                .where(CalculatedPrice.price_list_id == run.price_list_id)
                .order_by(Product.name.asc())
                .limit(1000)
            )
            .all()
        )
        out["items"] = [
            {
                "sku": product.code,
                "name": product.name,
                "globalRating": cp.rating_global,
                "localRating": cp.rating_local,
                "global_rating": cp.rating_global,
                "local_rating": cp.rating_local,
                "cost": float(cp.cost),
                "basePrice": float(cp.base_price),
                "competitorPrice": float(cp.competitor_price) if cp.competitor_price is not None else None,
                "finalPrice": float(cp.final_price),
                "zone": calculate_price_zone(
                    cp.final_price,
                    chosen_competitor_price=cp.chosen_competitor_price,
                    lowest_competitor_price=cp.lowest_competitor_price if cp.lowest_competitor_price is not None else cp.competitor_price,
                )[0],
                "reason": cp.applied_reason,
                "appliedRuleType": cp.applied_rule_type or "",
                "appliedRuleValue": float(cp.applied_rule_value) if cp.applied_rule_value is not None else None,
                "appliedListId": int(cp.applied_list_id) if cp.applied_list_id is not None else None,
                "appliedRuleAmbiguous": (cp.applied_rule_type or "") in AMBIGUOUS_LIST_TYPES,
            }
            for cp, product in rows
        ]
    return out


def list_workflow_competitors(*, db: Session, price_format_id: int) -> dict:
    pf = db.get(PriceFormat, price_format_id)
    if pf is None:
        raise ValueError("price format not found")
    rows = list_competitor_price_lists(db=db, price_format_code=pf.code)
    sources = [
        {
            "id": int(row.get("id") or 0),
            "sourceType": row.get("sourceType") or "",
            "sourceKey": row.get("sourceKey") or "",
            "region": row.get("branchName") or row.get("region") or "Без филиала",
            "competitor": row.get("competitorName") or row.get("supplier") or row.get("name") or "Конкурент",
            "name": row.get("sourceName") or row.get("name") or row.get("sourceKey") or "",
            "priceDate": row.get("priceDate") or "",
            "updatedAt": row.get("updatedAt") or row.get("sourceUpdatedAt") or "",
            "coefficient": float(row.get("coefficient") or 1),
            "priority": 100,
            "enabled": bool(row.get("isSelected")),
        }
        for row in rows
    ]
    grouped: dict[str, dict] = {}
    for source in sources:
        region = source["region"]
        competitor = source["competitor"]
        grouped.setdefault(region, {"region": region, "competitors": {}})
        grouped[region]["competitors"].setdefault(competitor, {"name": competitor, "sources": []})
        grouped[region]["competitors"][competitor]["sources"].append(source)
    return {
        "sources": sources,
        "groups": [
            {"region": region, "competitors": list(data["competitors"].values())}
            for region, data in grouped.items()
        ],
    }
