from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import (
    BendRange,
    BranchCost,
    BranchStock,
    CompetitorPriceList,
    CompetitorPriceListItem,
    CompetitorPricePercentile,
    ListItem,
    MarkupRange,
    NoCompetitorMarkupRange,
    PriceFormat,
    PricingRule,
    Product,
    ProductExtra,
    ProductRating,
    ReferenceImportJob,
    UniversalList,
    UniversalListPriceFormat,
)
from ..competitor_assignments import get_assigned_competitor_price_lists
from ...timezone import now_kz_naive


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def dumps_snapshot(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=_jsonable)


def loads_snapshot(value: str | None, fallback: object) -> object:
    try:
        parsed = json.loads(value or "")
        return parsed if parsed not in (None, "") else fallback
    except Exception:
        return fallback


def _range_rows(db: Session, model: type, price_format_id: int, order_field: Any) -> list[dict]:
    rows = db.execute(select(model).where(model.price_format_id == price_format_id).order_by(order_field.asc())).scalars().all()
    out: list[dict] = []
    for row in rows:
        item = {"id": row.id}
        for attr in ("cost_from", "cost_to", "price_from", "markup_percent", "bend_percent"):
            if hasattr(row, attr):
                item[attr] = _jsonable(getattr(row, attr))
        out.append(item)
    return out


def _is_active_list(row: UniversalList) -> bool:
    status = str(row.status or "").casefold()
    if status in {"inactive", "disabled", "archived", "неактивный", "не активный", "архивный"}:
        return False
    return (
        status.startswith("active")
        or status.startswith("enabled")
        or "актив" in status
        or "Р°РєС‚РёРІ" in status
    ) and "не актив" not in status


def _active_lists_snapshot(db: Session, pf: PriceFormat, as_of: date) -> list[dict]:
    direct_rows = (
        db.execute(
            select(UniversalList)
            .where((UniversalList.price_format_id.is_(None)) | (UniversalList.price_format_id == pf.id))
            .where((UniversalList.start_date.is_(None)) | (UniversalList.start_date <= as_of))
            .where((UniversalList.end_date.is_(None)) | (UniversalList.end_date >= as_of))
            .order_by(UniversalList.id.asc())
        )
        .scalars()
        .all()
    )
    linked_rows = (
        db.execute(
            select(UniversalList)
            .join(UniversalListPriceFormat, UniversalListPriceFormat.universal_list_id == UniversalList.id)
            .where(UniversalListPriceFormat.price_format_id == pf.id)
            .where((UniversalList.start_date.is_(None)) | (UniversalList.start_date <= as_of))
            .where((UniversalList.end_date.is_(None)) | (UniversalList.end_date >= as_of))
            .order_by(UniversalList.id.asc())
        )
        .scalars()
        .all()
    )
    rows = {row.id: row for row in [*direct_rows, *linked_rows] if _is_active_list(row)}
    counts = {}
    if rows:
        counts = dict(
            db.execute(
                select(ListItem.universal_list_id, func.count(ListItem.id))
                .where(ListItem.universal_list_id.in_(list(rows.keys())))
                .group_by(ListItem.universal_list_id)
            ).all()
        )
    return [
        {
            "id": row.id,
            "code": row.code,
            "name": row.name,
            "type": row.type,
            "status": row.status,
            "startDate": row.start_date,
            "endDate": row.end_date,
            "priceFormatId": row.price_format_id,
            "itemCount": int(counts.get(row.id, 0)),
        }
        for row in rows.values()
    ]


def _sources_snapshot(db: Session, pf: PriceFormat, requested_sources: list[dict], requested_percentiles: list[dict]) -> dict:
    selected = get_assigned_competitor_price_lists(db=db, price_format_id=int(pf.id))
    counts = {}
    if selected:
        counts = dict(
            db.execute(
                select(CompetitorPriceListItem.price_list_id, func.count(CompetitorPriceListItem.id))
                .where(CompetitorPriceListItem.price_list_id.in_([item.price_list.id for item in selected]))
                .group_by(CompetitorPriceListItem.price_list_id)
            ).all()
        )
    return {
        "requestedCompetitorSources": requested_sources,
        "requestedPercentileSources": requested_percentiles,
        "selectedCompetitorSources": [
            {
                "id": row.id,
                "sourceType": row.source_type,
                "sourceKey": row.source_key,
                "displayName": row.display_name,
                "supplier": row.supplier,
                "branchId": row.branch_id,
                "branchName": row.branch_name,
                "competitorName": row.competitor_name,
                "accountId": row.account_id,
                "accountLogin": row.account_login,
                "externalPriceListId": row.external_price_list_id,
                "coefficient": _jsonable(item.assignment.coefficient),
                "priceDate": row.price_date,
                "sourceUpdatedAt": row.source_updated_at,
                "syncBatchId": row.sync_batch_id,
                "itemsCount": int(counts.get(row.id, 0)),
            }
            for item in selected
            for row in [item.price_list]
        ],
    }


def _rule_snapshot(db: Session, pf: PriceFormat) -> dict:
    rule = db.get(PricingRule, pf.pricing_rule_id) if pf.pricing_rule_id else None
    return {
        "priceFormat": {
            "id": pf.id,
            "code": pf.code,
            "name": pf.name,
            "branch": pf.branch,
            "pricingRule": pf.pricing_rule,
            "pricingRuleId": pf.pricing_rule_id,
            "competitorPriceMode": pf.competitor_price_mode,
            "percentileNumber": pf.percentile_number,
            "progib": _jsonable(pf.progib),
        },
        "pricingRule": {
            "id": rule.id,
            "code": rule.code,
            "name": rule.name,
            "description": rule.description,
            "regionScope": rule.region_scope,
            "branchScope": rule.branch_scope,
            "markupTemplateId": rule.markup_template_id,
            "bendTemplateId": rule.bend_template_id,
            "noCompetitorTemplateId": rule.no_competitor_template_id,
            "roundingRuleId": rule.rounding_rule_id,
        }
        if rule
        else None,
        "markupRanges": _range_rows(db, MarkupRange, pf.id, MarkupRange.cost_from),
        "bendRanges": _range_rows(db, BendRange, pf.id, BendRange.price_from),
        "noCompetitorMarkupRanges": _range_rows(db, NoCompetitorMarkupRange, pf.id, NoCompetitorMarkupRange.cost_from),
    }


def _reference_versions_snapshot(db: Session, branch_id: str) -> dict:
    def count(model: type, *criteria: Any) -> int:
        stmt = select(func.count(model.id))
        for criterion in criteria:
            stmt = stmt.where(criterion)
        return int(db.execute(stmt).scalar() or 0)

    latest_jobs = {}
    for data_type in ("stock", "cost", "rating_global", "rating_local", "products"):
        row = (
            db.execute(
                select(ReferenceImportJob)
                .where(ReferenceImportJob.data_type == data_type)
                .order_by(ReferenceImportJob.finished_at.desc(), ReferenceImportJob.created_at.desc(), ReferenceImportJob.id.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        latest_jobs[data_type] = {
            "jobId": row.id,
            "status": row.status,
            "filename": row.filename,
            "rowsTotal": row.rows_total,
            "rowsSuccess": row.rows_success,
            "createdAt": row.created_at,
            "finishedAt": row.finished_at,
        } if row else None
    return {
        "branchId": branch_id,
        "capturedAt": now_kz_naive(),
        "counts": {
            "products": count(Product),
            "productExtras": int(db.execute(select(func.count(ProductExtra.product_id))).scalar() or 0),
            "stockRows": count(BranchStock, BranchStock.branch_id == branch_id),
            "costRows": count(BranchCost, BranchCost.branch_id == branch_id),
            "globalRatingRows": count(ProductRating, ProductRating.rating_type == "global"),
            "localRatingRows": count(ProductRating, ProductRating.branch_id == branch_id, ProductRating.rating_type == "local"),
        },
        "latestImportJobs": latest_jobs,
    }


def _percentile_snapshot(db: Session, pf: PriceFormat) -> dict:
    rows = (
        db.execute(
            select(
                CompetitorPricePercentile.branch_name,
                CompetitorPricePercentile.competitor_name,
                CompetitorPricePercentile.percentile,
                func.count(CompetitorPricePercentile.id),
                func.max(CompetitorPricePercentile.updated_at),
            )
            .where(CompetitorPricePercentile.price_format_id == pf.id)
            .group_by(
                CompetitorPricePercentile.branch_name,
                CompetitorPricePercentile.competitor_name,
                CompetitorPricePercentile.percentile,
            )
            .order_by(CompetitorPricePercentile.branch_name.asc(), CompetitorPricePercentile.competitor_name.asc())
        )
        .all()
    )
    return {
        "mode": pf.competitor_price_mode,
        "defaultPercentile": pf.percentile_number,
        "groups": [
            {
                "branchName": branch_name,
                "competitorName": competitor_name,
                "percentile": percentile,
                "rowsCount": int(count or 0),
                "updatedAt": updated_at,
            }
            for branch_name, competitor_name, percentile, count, updated_at in rows
        ],
    }


def build_generate_snapshot(
    *,
    db: Session,
    price_format: PriceFormat,
    branch_id: str,
    as_of: date,
    generated_by: str,
    activation_date: date | None,
    requested_competitor_sources: list[dict],
    requested_percentile_sources: list[dict],
) -> dict:
    run_sources = _sources_snapshot(db, price_format, requested_competitor_sources, requested_percentile_sources)
    run_rule = _rule_snapshot(db, price_format)
    run_lists = _active_lists_snapshot(db, price_format, as_of)
    run_reference_versions = _reference_versions_snapshot(db, branch_id)
    run_percentile_config = _percentile_snapshot(db, price_format)
    return {
        "schemaVersion": 1,
        "generatedBy": generated_by,
        "createdAt": now_kz_naive(),
        "activationDate": activation_date,
        "asOf": as_of,
        "branchId": branch_id,
        "priceFormatCode": price_format.code,
        "runSources": run_sources,
        "runRule": run_rule,
        "runLists": run_lists,
        "runReferenceVersions": run_reference_versions,
        "runPercentileConfig": run_percentile_config,
    }
