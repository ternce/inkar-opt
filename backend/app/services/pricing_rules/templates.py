from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import (
    BendTemplate,
    BendTemplateRow,
    MarkupTemplate,
    MarkupTemplateRow,
    NoCompetitorMarkupTemplate,
    NoCompetitorMarkupTemplateRow,
    RoundingRule,
)
from .common import touch, validate_ranges
from ...timezone import local_iso, now_kz_naive


TEMPLATE_SPECS = {
    "markup": (MarkupTemplate, MarkupTemplateRow, "markup_percent", "markupPercent"),
    "bend": (BendTemplate, BendTemplateRow, "bend_percent", "bendPercent"),
    "no_competitor": (NoCompetitorMarkupTemplate, NoCompetitorMarkupTemplateRow, "markup_percent", "markupPercent"),
}


def row_to_dict(row, value_attr: str, value_key: str) -> dict:
    return {
        "id": row.id,
        "costFrom": float(row.cost_from),
        "costTo": float(row.cost_to) if row.cost_to is not None else None,
        value_key: float(getattr(row, value_attr)),
        "sortOrder": row.sort_order,
    }


def template_to_dict(template, kind: str, *, include_rows: bool = True) -> dict:
    _, _, value_attr, value_key = TEMPLATE_SPECS[kind]
    return {
        "id": template.id,
        "code": template.code,
        "name": template.name,
        "description": template.description,
        "isActive": bool(template.is_active),
        "createdAt": local_iso(template.created_at) if template.created_at else "",
        "updatedAt": local_iso(template.updated_at) if template.updated_at else "",
        "rows": [
            row_to_dict(row, value_attr, value_key)
            for row in sorted(getattr(template, "rows", []), key=lambda x: (x.sort_order, float(x.cost_from)))
        ]
        if include_rows
        else [],
    }


def list_templates(*, db: Session, kind: str) -> list[dict]:
    model, _, _, _ = TEMPLATE_SPECS[kind]
    rows = db.execute(select(model).order_by(model.updated_at.desc(), model.id.desc())).scalars().all()
    return [template_to_dict(row, kind) for row in rows]


def get_template(*, db: Session, kind: str, template_id: int):
    model, _, _, _ = TEMPLATE_SPECS[kind]
    row = db.get(model, template_id)
    if row is None:
        raise ValueError("template not found")
    return row


def upsert_template(*, db: Session, kind: str, payload: dict, template_id: int | None = None):
    model, row_model, value_attr, value_key = TEMPLATE_SPECS[kind]
    code = str(payload.get("code") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not code:
        raise ValueError("code is required")
    if not name:
        raise ValueError("name is required")

    existing = db.execute(select(model).where(model.code == code)).scalars().first()
    if existing is not None and (template_id is None or int(existing.id) != int(template_id)):
        raise ValueError("code must be unique")

    row = db.get(model, template_id) if template_id is not None else None
    if row is None:
        row = model(code=code, name=name)
        db.add(row)
        db.flush()

    row.code = code
    row.name = name
    row.description = str(payload.get("description") or "").strip()
    if payload.get("isActive") is not None:
        row.is_active = bool(payload.get("isActive"))
    touch(row)

    if isinstance(payload.get("rows"), list):
        normalized = validate_ranges(payload["rows"], value_key)
        for old in list(row.rows):
            db.delete(old)
        db.flush()
        for idx, item in enumerate(normalized):
            db.add(
                row_model(
                    template_id=row.id,
                    cost_from=float(item["cost_from"]),
                    cost_to=float(item["cost_to"]) if item["cost_to"] is not None else None,
                    **{value_attr: float(item[value_key])},
                    sort_order=int(item.get("sort_order", idx)),
                )
            )
    db.commit()
    db.refresh(row)
    return row


def copy_template(*, db: Session, kind: str, template_id: int):
    source = get_template(db=db, kind=kind, template_id=template_id)
    payload = template_to_dict(source, kind)
    payload["code"] = f"{payload['code']}_copy_{int(now_kz_naive().timestamp())}"
    payload["name"] = f"{payload['name']} (копия)"
    return upsert_template(db=db, kind=kind, payload=payload)


def rounding_to_dict(row: RoundingRule) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "mode": row.mode,
        "precision": row.precision,
        "step": float(row.step) if row.step is not None else None,
        "isActive": bool(row.is_active),
        "createdAt": local_iso(row.created_at) if row.created_at else "",
        "updatedAt": local_iso(row.updated_at) if row.updated_at else "",
    }


def list_rounding_rules(*, db: Session) -> list[dict]:
    rows = db.execute(select(RoundingRule).order_by(RoundingRule.updated_at.desc(), RoundingRule.id.desc())).scalars().all()
    return [rounding_to_dict(row) for row in rows]


def upsert_rounding_rule(*, db: Session, payload: dict, rule_id: int | None = None) -> RoundingRule:
    code = str(payload.get("code") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not code:
        raise ValueError("code is required")
    if not name:
        raise ValueError("name is required")
    mode = str(payload.get("mode") or "math").strip()
    if mode not in {"math", "up", "down"}:
        raise ValueError("mode must be math, up or down")
    precision = int(payload.get("precision") if payload.get("precision") is not None else 2)
    if precision not in {0, 1, 2}:
        raise ValueError("precision must be 0, 1 or 2")
    step_raw = payload.get("step")
    step = None if step_raw in (None, "") else float(step_raw)
    if step is not None and step <= 0:
        raise ValueError("step must be positive")

    existing = db.execute(select(RoundingRule).where(RoundingRule.code == code)).scalars().first()
    if existing is not None and (rule_id is None or int(existing.id) != int(rule_id)):
        raise ValueError("code must be unique")

    row = db.get(RoundingRule, rule_id) if rule_id is not None else None
    if row is None:
        row = RoundingRule(code=code, name=name)
        db.add(row)
        db.flush()
    row.code = code
    row.name = name
    row.mode = mode
    row.precision = precision
    row.step = step
    if payload.get("isActive") is not None:
        row.is_active = bool(payload.get("isActive"))
    touch(row)
    db.commit()
    db.refresh(row)
    return row
