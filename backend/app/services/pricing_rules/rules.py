from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...models import (
    BendRange,
    BendTemplate,
    MarkupRange,
    MarkupTemplate,
    NoCompetitorMarkupRange,
    NoCompetitorMarkupTemplate,
    PriceFormat,
    PricingRule,
    RoundingRule,
)
from .common import touch
from .templates import rounding_to_dict, template_to_dict


def pricing_rule_to_dict(row: PricingRule, *, include_templates: bool = True) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "description": row.description,
        "regionScope": row.region_scope,
        "branchScope": row.branch_scope,
        "markupTemplateId": row.markup_template_id,
        "bendTemplateId": row.bend_template_id,
        "noCompetitorTemplateId": row.no_competitor_template_id,
        "roundingRuleId": row.rounding_rule_id,
        "isActive": bool(row.is_active),
        "createdAt": row.created_at.isoformat() if row.created_at else "",
        "updatedAt": row.updated_at.isoformat() if row.updated_at else "",
        "markupTemplate": template_to_dict(row.markup_template, "markup") if include_templates and getattr(row, "markup_template", None) else None,
        "bendTemplate": template_to_dict(row.bend_template, "bend") if include_templates and getattr(row, "bend_template", None) else None,
        "noCompetitorTemplate": template_to_dict(row.no_competitor_template, "no_competitor") if include_templates and getattr(row, "no_competitor_template", None) else None,
        "roundingRule": rounding_to_dict(row.rounding_rule) if include_templates and getattr(row, "rounding_rule", None) else None,
    }


def _attach_templates(db: Session, rows: list[PricingRule]) -> None:
    markup_ids = {r.markup_template_id for r in rows if r.markup_template_id}
    bend_ids = {r.bend_template_id for r in rows if r.bend_template_id}
    no_comp_ids = {r.no_competitor_template_id for r in rows if r.no_competitor_template_id}
    rounding_ids = {r.rounding_rule_id for r in rows if r.rounding_rule_id}
    markups = {r.id: r for r in db.execute(select(MarkupTemplate).where(MarkupTemplate.id.in_(markup_ids))).scalars().all()} if markup_ids else {}
    bends = {r.id: r for r in db.execute(select(BendTemplate).where(BendTemplate.id.in_(bend_ids))).scalars().all()} if bend_ids else {}
    no_comps = {r.id: r for r in db.execute(select(NoCompetitorMarkupTemplate).where(NoCompetitorMarkupTemplate.id.in_(no_comp_ids))).scalars().all()} if no_comp_ids else {}
    roundings = {r.id: r for r in db.execute(select(RoundingRule).where(RoundingRule.id.in_(rounding_ids))).scalars().all()} if rounding_ids else {}
    for row in rows:
        row.markup_template = markups.get(row.markup_template_id)
        row.bend_template = bends.get(row.bend_template_id)
        row.no_competitor_template = no_comps.get(row.no_competitor_template_id)
        row.rounding_rule = roundings.get(row.rounding_rule_id)


def list_pricing_rules(*, db: Session) -> list[dict]:
    rows = db.execute(select(PricingRule).order_by(PricingRule.updated_at.desc(), PricingRule.id.desc())).scalars().all()
    _attach_templates(db, rows)
    return [pricing_rule_to_dict(row, include_templates=False) for row in rows]


def get_pricing_rule(*, db: Session, rule_id: int) -> PricingRule:
    row = db.get(PricingRule, rule_id)
    if row is None:
        raise ValueError("pricing rule not found")
    _attach_templates(db, [row])
    return row


def upsert_pricing_rule(*, db: Session, payload: dict, rule_id: int | None = None) -> PricingRule:
    code = str(payload.get("code") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not code:
        raise ValueError("code is required")
    if not name:
        raise ValueError("name is required")
    existing = db.execute(select(PricingRule).where(PricingRule.code == code)).scalars().first()
    if existing is not None and (rule_id is None or int(existing.id) != int(rule_id)):
        raise ValueError("code must be unique")
    row = db.get(PricingRule, rule_id) if rule_id is not None else None
    if row is None:
        row = PricingRule(code=code, name=name)
        db.add(row)
        db.flush()
    row.code = code
    row.name = name
    row.description = str(payload.get("description") or "").strip()
    row.region_scope = str(payload.get("regionScope") or payload.get("region_scope") or "").strip()
    row.branch_scope = str(payload.get("branchScope") or payload.get("branch_scope") or "").strip()
    row.markup_template_id = _optional_int(payload.get("markupTemplateId"))
    row.bend_template_id = _optional_int(payload.get("bendTemplateId"))
    row.no_competitor_template_id = _optional_int(payload.get("noCompetitorTemplateId"))
    row.rounding_rule_id = _optional_int(payload.get("roundingRuleId"))
    if payload.get("isActive") is not None:
        row.is_active = bool(payload.get("isActive"))
    touch(row)
    db.commit()
    db.refresh(row)
    _attach_templates(db, [row])
    return row


def _optional_int(value: object) -> int | None:
    if value in (None, "", "none"):
        return None
    return int(value)


def _d(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.000001"))


def _same_decimal(a: object, b: object) -> bool:
    return _d(a) == _d(b)


def _markup_rows_match(template: MarkupTemplate | NoCompetitorMarkupTemplate | None, rows: list[MarkupRange | NoCompetitorMarkupRange]) -> bool:
    if template is None:
        return True
    template_rows = sorted(template.rows, key=lambda x: (x.sort_order, float(x.cost_from)))
    rows_sorted = sorted(rows, key=lambda x: float(x.cost_from))
    if len(template_rows) != len(rows_sorted):
        return False
    for tmpl, row in zip(template_rows, rows_sorted):
        if not _same_decimal(tmpl.cost_from, row.cost_from):
            return False
        if not _same_decimal(tmpl.cost_to, row.cost_to):
            return False
        if not _same_decimal(Decimal(str(tmpl.markup_percent)) / Decimal("100"), row.markup_percent):
            return False
    return True


def _bend_rows_match(template: BendTemplate | None, rows: list[BendRange]) -> bool:
    if template is None:
        return True
    template_rows = sorted(template.rows, key=lambda x: (x.sort_order, float(x.cost_from)))
    rows_sorted = sorted(rows, key=lambda x: float(x.price_from))
    if len(template_rows) != len(rows_sorted):
        return False
    for tmpl, row in zip(template_rows, rows_sorted):
        if not _same_decimal(tmpl.cost_from, row.price_from):
            return False
        if not _same_decimal(tmpl.bend_percent, row.bend_percent):
            return False
    return True


def pricing_rule_application_status(*, db: Session, pf: PriceFormat) -> dict:
    rule = get_pricing_rule(db=db, rule_id=pf.pricing_rule_id) if pf.pricing_rule_id else None
    if rule is None:
        return {
            "ruleId": None,
            "ruleName": "",
            "appliedAt": pf.pricing_rule_applied_at.isoformat() if pf.pricing_rule_applied_at else "",
            "status": "not_applied",
            "isManualChanged": False,
            "tablesUpdated": [],
            "tablesChanged": [],
            "roundingRuleId": int(pf.rounding_rule_id) if pf.rounding_rule_id else None,
        }

    markup_rows = db.execute(select(MarkupRange).where(MarkupRange.price_format_id == pf.id)).scalars().all()
    bend_rows = db.execute(select(BendRange).where(BendRange.price_format_id == pf.id)).scalars().all()
    no_comp_rows = db.execute(select(NoCompetitorMarkupRange).where(NoCompetitorMarkupRange.price_format_id == pf.id)).scalars().all()
    checks = {
        "recommendedMarkups": _markup_rows_match(rule.markup_template, markup_rows),
        "bendRanges": _bend_rows_match(rule.bend_template, bend_rows),
        "noCompetitorMarkups": _markup_rows_match(rule.no_competitor_template, no_comp_rows),
        "rounding": (rule.rounding_rule_id == pf.rounding_rule_id),
    }
    changed = [name for name, ok in checks.items() if not ok]
    try:
        tables_updated = json.loads(pf.pricing_rule_applied_tables_json or "[]")
    except Exception:
        tables_updated = []
    return {
        "ruleId": int(rule.id),
        "ruleName": rule.name,
        "ruleCode": rule.code,
        "appliedAt": pf.pricing_rule_applied_at.isoformat() if pf.pricing_rule_applied_at else "",
        "status": "manual_changed" if changed else "synced",
        "isManualChanged": bool(changed),
        "tablesUpdated": tables_updated if isinstance(tables_updated, list) else [],
        "tablesChanged": changed,
        "roundingRuleId": int(pf.rounding_rule_id) if pf.rounding_rule_id else None,
        "roundingRuleName": rule.rounding_rule.name if getattr(rule, "rounding_rule", None) and pf.rounding_rule_id == rule.rounding_rule_id else "",
    }


def delete_pricing_rule(*, db: Session, rule_id: int) -> None:
    row = db.get(PricingRule, rule_id)
    if row is None:
        raise ValueError("pricing rule not found")
    db.delete(row)
    db.commit()


def copy_pricing_rule(*, db: Session, rule_id: int) -> PricingRule:
    source = get_pricing_rule(db=db, rule_id=rule_id)
    payload = pricing_rule_to_dict(source, include_templates=False)
    payload["code"] = f"{payload['code']}_copy_{int(datetime.utcnow().timestamp())}"
    payload["name"] = f"{payload['name']} (копия)"
    return upsert_pricing_rule(db=db, payload=payload)


def apply_pricing_rule_to_format(*, db: Session, format_code: str, rule_id: int) -> dict:
    pf = db.execute(select(PriceFormat).where(PriceFormat.code == format_code)).scalars().first()
    if pf is None:
        pf = PriceFormat(code=format_code, name=format_code)
        db.add(pf)
        db.flush()
    rule = get_pricing_rule(db=db, rule_id=rule_id)
    pf.pricing_rule_id = rule.id
    pf.pricing_rule = rule.code
    pf.rounding_rule_id = rule.rounding_rule_id
    pf.pricing_rule_applied_at = datetime.utcnow()
    updated_tables: list[str] = []

    if rule.markup_template is not None:
        db.execute(delete(MarkupRange).where(MarkupRange.price_format_id == pf.id))
        for item in sorted(rule.markup_template.rows, key=lambda x: (x.sort_order, float(x.cost_from))):
            db.add(MarkupRange(price_format_id=pf.id, cost_from=float(item.cost_from), cost_to=float(item.cost_to) if item.cost_to is not None else None, markup_percent=float(item.markup_percent) / 100.0))
        updated_tables.append("recommendedMarkups")

    if rule.bend_template is not None:
        db.execute(delete(BendRange).where(BendRange.price_format_id == pf.id))
        for item in sorted(rule.bend_template.rows, key=lambda x: (x.sort_order, float(x.cost_from))):
            db.add(BendRange(price_format_id=pf.id, price_from=float(item.cost_from), bend_percent=float(item.bend_percent)))
        updated_tables.append("bendRanges")

    if rule.no_competitor_template is not None:
        db.execute(delete(NoCompetitorMarkupRange).where(NoCompetitorMarkupRange.price_format_id == pf.id))
        for item in sorted(rule.no_competitor_template.rows, key=lambda x: (x.sort_order, float(x.cost_from))):
            db.add(NoCompetitorMarkupRange(price_format_id=pf.id, cost_from=float(item.cost_from), cost_to=float(item.cost_to) if item.cost_to is not None else None, markup_percent=float(item.markup_percent) / 100.0))
        updated_tables.append("noCompetitorMarkups")

    if rule.rounding_rule_id is not None:
        updated_tables.append("rounding")

    pf.pricing_rule_applied_tables_json = json.dumps(updated_tables, ensure_ascii=False)
    db.commit()
    db.refresh(pf)
    status = pricing_rule_application_status(db=db, pf=pf)
    return {"status": "ok", "formatCode": format_code, "pricingRuleId": rule.id, "appliedRule": status}
