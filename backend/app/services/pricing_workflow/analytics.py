from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import CalculatedPrice, PriceList, PricingWorkflowRun, Product
from ..pricing import calculate_price_zone
from .snapshot import loads_snapshot


def _f(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _load_list_ids(value: str | None) -> list[int]:
    try:
        parsed = json.loads(value or "[]")
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[int] = []
    for item in parsed:
        try:
            out.append(int(item))
        except Exception:
            pass
    return out


def _lowest_competitor_price(row: CalculatedPrice) -> object:
    return row.lowest_competitor_price if row.lowest_competitor_price is not None else row.competitor_price


def _derived_zone(row: CalculatedPrice) -> str:
    zone, _reference, _deviation = calculate_price_zone(
        row.final_price,
        chosen_competitor_price=getattr(row, "chosen_competitor_price", None),
        lowest_competitor_price=_lowest_competitor_price(row),
    )
    return zone


def _source_label(source_name: str, snapshot: dict) -> str:
    if not source_name:
        return "legacy/unknown"
    sources = ((snapshot.get("runSources") or {}).get("selectedCompetitorSources") or []) if snapshot else []
    for source in sources:
        key = f"{source.get('sourceType')}:{source.get('sourceKey')}"
        if key == source_name:
            return source.get("displayName") or source.get("supplier") or source_name
    if source_name.startswith("percentile:"):
        return source_name.replace("percentile:", "Персентиль: ")
    return source_name


def _is_no_competitor_rule_reason(reason: object) -> bool:
    text = str(reason or "").strip().casefold()
    if not text:
        return False
    return (
        "no_competitor" in text
        or "no competitor" in text
        or ("нет цен" in text and "конкур" in text)
        or "без конкурент" in text
    )


def build_workflow_analytics(*, db: Session, price_list_id: int) -> dict:
    rows = db.execute(select(CalculatedPrice).where(CalculatedPrice.price_list_id == price_list_id)).scalars().all()
    total = len(rows)
    pl = db.get(PriceList, price_list_id)
    snapshot = loads_snapshot(pl.run_snapshot_json, {}) if pl is not None else {}
    run_lists = loads_snapshot(pl.run_lists_json, []) if pl is not None else []

    product_ids = [row.product_id for row in rows]
    product_map = {
        row.id: row
        for row in db.execute(select(Product).where(Product.id.in_(product_ids))).scalars().all()
    } if product_ids else {}
    list_names = {
        int(item.get("id")): (item.get("name") or str(item.get("id")))
        for item in run_lists
        if isinstance(item, dict) and item.get("id") is not None
    }

    zone_counts = {"left": 0, "optimal": 0, "right": 0, "no-data": 0}
    right_zone_reasons = {
        "right_due_to_mdc_floor": 0,
        "right_due_to_chosen_higher_competitor": 0,
        "right_due_to_universal_override": 0,
        "right_other": 0,
    }
    reason_counts: dict[str, int] = {}
    markup_buckets = {"<0%": 0, "0-5%": 0, "5-10%": 0, "10-20%": 0, "20%+": 0}
    competitor_usage_counts: dict[str, int] = {}
    percentile_usage_counts: dict[str, int] = {}
    list_usage_counts: dict[int, int] = {}
    final_prices: list[float] = []
    markups: list[float] = []
    bends: list[float] = []
    top_changed: list[dict] = []
    prevented_by_mdc = 0
    with_competitors = 0
    used_percentile = 0
    used_substitute = 0
    changed_price_count = 0
    no_competitor_rule_applied = 0

    for row in rows:
        zone = _derived_zone(row)
        zone_counts[zone if zone in zone_counts else "no-data"] += 1

        reason = row.applied_reason or ""
        if "минимальн" in reason.lower() or "mdc" in reason.lower() or "мдц" in reason.lower():
            prevented_by_mdc += 1
        if _is_no_competitor_rule_reason(reason):
            no_competitor_rule_applied += 1
            reason_counts["no_competitor_rule_applied"] = reason_counts.get("no_competitor_rule_applied", 0) + 1

        reason_key = "no_competitor" if row.competitor_price is None else "competitor"
        reason_counts[reason_key] = reason_counts.get(reason_key, 0) + 1

        source_name = row.applied_source_name or ("legacy/unknown" if row.competitor_price is not None else "")
        row_used_percentile = bool(row.used_percentile) or source_name.startswith("percentile:")
        if row.competitor_price is not None:
            with_competitors += 1
            if row_used_percentile:
                percentile_usage_counts[source_name] = percentile_usage_counts.get(source_name, 0) + 1
            else:
                competitor_usage_counts[source_name] = competitor_usage_counts.get(source_name, 0) + 1
        if row_used_percentile:
            used_percentile += 1
        if row.used_substitute:
            used_substitute += 1

        applied_list_ids = _load_list_ids(row.applied_list_ids)
        for list_id in applied_list_ids:
            list_usage_counts[list_id] = list_usage_counts.get(list_id, 0) + 1

        final = _f(row.final_price)
        cost = _f(row.cost)
        base = _f(row.base_price)
        competitor_price = _f(_lowest_competitor_price(row))
        chosen_competitor_price = _f(getattr(row, "chosen_competitor_price", None))
        _right_zone, reference_price_decimal, _deviation = calculate_price_zone(
            row.final_price,
            chosen_competitor_price=chosen_competitor_price,
            lowest_competitor_price=competitor_price,
        )
        reference_price = _f(reference_price_decimal)
        if zone == "right" and competitor_price not in (None, 0):
            if applied_list_ids:
                right_zone_reasons["right_due_to_universal_override"] += 1
            elif base is not None and reference_price is not None and base > reference_price:
                right_zone_reasons["right_due_to_mdc_floor"] += 1
            else:
                right_zone_reasons["right_other"] += 1
        if final is not None:
            final_prices.append(final)
        if final is not None and base not in (None, 0):
            change_percent = (final - float(base)) / float(base) * 100
            if abs(change_percent) > 0.01:
                changed_price_count += 1
            product = product_map.get(row.product_id)
            top_changed.append(
                {
                    "sku": product.code if product else "",
                    "name": product.name if product else "",
                    "oldPrice": base,
                    "newPrice": final,
                    "changePercent": round(change_percent, 2),
                    "zone": zone,
                }
            )
        if final is not None and cost not in (None, 0):
            markup = (final - float(cost)) / float(cost) * 100
            markups.append(markup)
            if markup < 0:
                markup_buckets["<0%"] += 1
            elif markup < 5:
                markup_buckets["0-5%"] += 1
            elif markup < 10:
                markup_buckets["5-10%"] += 1
            elif markup < 20:
                markup_buckets["10-20%"] += 1
            else:
                markup_buckets["20%+"] += 1
        bend_used = _f(getattr(row, "bend_percent_used", None))
        if bend_used is not None and row.price_from_competitor is not None:
            bends.append(bend_used)
        elif row.price_from_competitor is not None and getattr(row, "chosen_competitor_price", None):
            chosen = _f(row.chosen_competitor_price)
            after_bend = _f(row.price_from_competitor)
            if chosen not in (None, 0) and after_bend is not None:
                bends.append((chosen - after_bend) / chosen * 100)

    competitor_usage = [
        {"source": source, "label": _source_label(source, snapshot if isinstance(snapshot, dict) else {}), "skuCount": count}
        for source, count in sorted(competitor_usage_counts.items(), key=lambda item: item[1], reverse=True)[:25]
    ]
    percentile_usage = [
        {"source": source, "label": _source_label(source, snapshot if isinstance(snapshot, dict) else {}), "skuCount": count}
        for source, count in sorted(percentile_usage_counts.items(), key=lambda item: item[1], reverse=True)[:25]
    ]
    universal_list_usage = [
        {"listId": list_id, "name": list_names.get(list_id, str(list_id)), "skuCount": count}
        for list_id, count in sorted(list_usage_counts.items(), key=lambda item: item[1], reverse=True)
    ]
    top_changed = sorted(top_changed, key=lambda item: abs(item.get("changePercent") or 0), reverse=True)[:25]
    avg_markup = round(sum(markups) / len(markups), 2) if markups else 0
    avg_bend = round(sum(bends) / len(bends), 2) if bends else 0

    analytics = {
        "summary": {
            "skuTotal": total,
            "withCompetitors": with_competitors,
            "withoutCompetitors": total - with_competitors,
            "leftZone": zone_counts["left"],
            "optimalZone": zone_counts["optimal"],
            "rightZone": zone_counts["right"],
            "noDataZone": zone_counts["no-data"],
            "noCompetitorRuleApplied": no_competitor_rule_applied,
            "noIntersection": zone_counts["no-data"],
            "averageMarkup": avg_markup,
            "averageBendPercent": avg_bend,
            "averageFinalPrice": round(sum(final_prices) / len(final_prices), 2) if final_prices else 0,
            "minPrice": min(final_prices) if final_prices else None,
            "maxPrice": max(final_prices) if final_prices else None,
            "percentileUsage": used_percentile,
            "substituteUsage": used_substitute,
            "changedPriceCount": changed_price_count,
        },
        "zones": [
            {"name": "left", "label": "Левое плечо", "value": zone_counts["left"]},
            {"name": "optimal", "label": "Зона логичности", "value": zone_counts["optimal"]},
            {"name": "right", "label": "Правое плечо", "value": zone_counts["right"]},
            {"name": "no-data", "label": "Не пересек", "value": zone_counts["no-data"]},
        ],
        "markupHistogram": [{"bucket": key, "value": value} for key, value in markup_buckets.items()],
        "competitorUsage": competitor_usage,
        "percentileUsage": percentile_usage,
        "universalListUsage": universal_list_usage,
        "topChangedProducts": top_changed,
        "productsWithoutCompetitors": total - with_competitors,
        "noCompetitorRuleApplied": no_competitor_rule_applied,
        "productsBelowMdcPrevented": prevented_by_mdc,
        "rightZoneReasons": right_zone_reasons,
        "productsWithSubstituteMatches": used_substitute,
        "reasonCounts": reason_counts,
        "exportSummary": {
            "skuTotal": total,
            "changedPriceCount": changed_price_count,
            "noCompetitor": total - with_competitors,
            "noCompetitorRuleApplied": no_competitor_rule_applied,
            "usedPercentile": used_percentile,
            "usedSubstitute": used_substitute,
            "averageMarkup": avg_markup,
        },
        "snapshot": snapshot,
        "runSources": loads_snapshot(pl.run_sources_json, {}) if pl is not None else {},
        "runRule": loads_snapshot(pl.run_rule_json, {}) if pl is not None else {},
        "runLists": run_lists,
        "runReferenceVersions": loads_snapshot(pl.run_reference_versions_json, {}) if pl is not None else {},
        "runPercentileConfig": loads_snapshot(pl.run_percentile_config_json, {}) if pl is not None else {},
    }
    return analytics


def analytics_for_run(*, db: Session, run: PricingWorkflowRun) -> dict:
    if run.price_list_id:
        return build_workflow_analytics(db=db, price_list_id=run.price_list_id)
    if run.analytics_json:
        try:
            parsed = json.loads(run.analytics_json)
            if isinstance(parsed, dict) and parsed:
                return parsed
        except Exception:
            pass
    return {}
