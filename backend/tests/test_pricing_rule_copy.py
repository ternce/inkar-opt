import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.deps import get_db
from backend.app.main import app
from backend.app.models import (
    BendTemplate,
    BendTemplateRow,
    MarkupTemplate,
    MarkupTemplateRow,
    NoCompetitorMarkupTemplate,
    NoCompetitorMarkupTemplateRow,
    PriceFormat,
    PricingRule,
    RoundingRule,
)
from backend.app.services.pricing_rules import rules as rule_service


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _client():
    Session = _db()

    def override_db():
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    return TestClient(app), Session


def _full_source(db):
    markup = MarkupTemplate(code="SRC_M", name="Source markup", description="m", is_active=False)
    bend = BendTemplate(code="SRC_B", name="Source bend", description="b")
    no_comp = NoCompetitorMarkupTemplate(code="SRC_N", name="Source no competitor", description="n")
    rounding = RoundingRule(code="SRC_R", name="Source rounding", mode="up", precision=0, step=5, is_active=False)
    db.add_all([markup, bend, no_comp, rounding])
    db.flush()
    db.add_all(
        [
            MarkupTemplateRow(template_id=markup.id, cost_from=0, cost_to=99, markup_percent=10, sort_order=0),
            MarkupTemplateRow(template_id=markup.id, cost_from=100, cost_to=None, markup_percent=12, sort_order=1),
            BendTemplateRow(template_id=bend.id, cost_from=0, cost_to=None, bend_percent=0.2, sort_order=0),
            NoCompetitorMarkupTemplateRow(template_id=no_comp.id, cost_from=0, cost_to=None, markup_percent=8, sort_order=0),
        ]
    )
    rule = PricingRule(
        code="SRC_RULE",
        name="Source rule",
        description="source desc",
        region_scope="KZ",
        branch_scope="A",
        markup_template_id=markup.id,
        bend_template_id=bend.id,
        no_competitor_template_id=no_comp.id,
        rounding_rule_id=rounding.id,
    )
    db.add(rule)
    db.flush()
    return rule


def _copy(db, source_id, *, code="COPIED_RULE", name="Copied rule"):
    return rule_service.copy_pricing_rule(db=db, rule_id=source_id, payload={"code": code, "name": name})


def test_copy_rule_with_all_linked_settings_gets_new_templates_and_rows():
    Session = _db()
    with Session() as db:
        source = _full_source(db)
        db.commit()
        copied = _copy(db, source.id)

        assert copied.id != source.id
        assert copied.markup_template_id != source.markup_template_id
        assert copied.bend_template_id != source.bend_template_id
        assert copied.no_competitor_template_id != source.no_competitor_template_id
        assert copied.rounding_rule_id != source.rounding_rule_id
        assert copied.markup_template.name == "Copied rule — наценка"
        assert copied.bend_template.name == "Copied rule — прогиб"
        assert copied.no_competitor_template.name == "Copied rule — без конкурентов"
        assert copied.rounding_rule.name == "Copied rule — округление"
        assert [float(row.markup_percent) for row in copied.markup_template.rows] == [10, 12]
        assert [row.sort_order for row in copied.markup_template.rows] == [0, 1]
        assert copied.markup_template.rows[0].id != source.markup_template.rows[0].id
        assert copied.rounding_rule.mode == "up"
        assert copied.rounding_rule.precision == 0
        assert float(copied.rounding_rule.step) == 5


def test_copy_rule_with_only_some_linked_settings_and_empty_rule():
    Session = _db()
    with Session() as db:
        markup = MarkupTemplate(code="ONLY_M", name="Only markup")
        db.add(markup)
        db.flush()
        db.add(MarkupTemplateRow(template_id=markup.id, cost_from=0, cost_to=None, markup_percent=11, sort_order=0))
        partial = PricingRule(code="PARTIAL", name="Partial", markup_template_id=markup.id)
        empty = PricingRule(code="EMPTY", name="Empty")
        db.add_all([partial, empty])
        db.commit()

        copied_partial = _copy(db, partial.id, code="PARTIAL_COPY", name="Partial copy")
        copied_empty = _copy(db, empty.id, code="EMPTY_COPY", name="Empty copy")

        assert copied_partial.markup_template_id is not None
        assert copied_partial.bend_template_id is None
        assert copied_partial.rounding_rule_id is None
        assert copied_empty.markup_template_id is None
        assert copied_empty.bend_template_id is None
        assert copied_empty.no_competitor_template_id is None
        assert copied_empty.rounding_rule_id is None


def test_copy_does_not_modify_source_and_copied_edits_are_independent():
    Session = _db()
    with Session() as db:
        source = _full_source(db)
        db.commit()
        source_id = source.id
        copied = _copy(db, source_id)
        copied_markup_id = copied.markup_template_id
        copied_rounding_id = copied.rounding_rule_id
        source_markup_id = source.markup_template_id
        source_rounding_id = source.rounding_rule_id

        db.get(MarkupTemplate, copied_markup_id).rows[0].markup_percent = 99
        db.get(RoundingRule, copied_rounding_id).mode = "down"
        db.commit()

        source_markup = db.get(MarkupTemplate, source_markup_id)
        source_rounding = db.get(RoundingRule, source_rounding_id)
        assert [float(row.markup_percent) for row in source_markup.rows] == [10, 12]
        assert source_rounding.mode == "up"
        assert db.get(PricingRule, source_id).markup_template_id == source_markup_id


def test_deleting_copied_rule_does_not_damage_source():
    Session = _db()
    with Session() as db:
        source = _full_source(db)
        db.commit()
        copied = _copy(db, source.id)
        copied_id = copied.id
        source_markup_id = source.markup_template_id

        rule_service.delete_pricing_rule(db=db, rule_id=copied_id)

        assert db.get(PricingRule, copied_id) is None
        assert db.get(PricingRule, source.id) is not None
        assert db.get(MarkupTemplate, source_markup_id) is not None


def test_copy_handles_missing_linked_template_with_warning():
    Session = _db()
    with Session() as db:
        source = PricingRule(code="BROKEN", name="Broken", markup_template_id=9999)
        db.add(source)
        db.commit()

        copied = _copy(db, source.id)
        payload = rule_service.pricing_rule_to_dict(copied)

        assert copied.markup_template_id is None
        assert payload["copyWarnings"] == ["markupTemplate missing"]


def test_copy_rollback_on_failure(monkeypatch):
    Session = _db()
    with Session() as db:
        source = _full_source(db)
        db.commit()

        def fail_rounding(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(rule_service, "_copy_rounding_rule", fail_rounding)
        with pytest.raises(RuntimeError):
            _copy(db, source.id)

        assert db.query(PricingRule).filter(PricingRule.code == "COPIED_RULE").count() == 0
        assert db.query(MarkupTemplate).filter(MarkupTemplate.code.like("COPIED_RULE%")).count() == 0


def test_copy_api_invalid_source_and_duplicate_rule_name():
    client, Session = _client()
    try:
        with Session() as db:
            source = _full_source(db)
            db.commit()
            source_id = source.id

        invalid = client.post("/api/pricing-rules", json={"code": "NEW", "name": "New", "copyFromRuleId": 9999})
        duplicate_name = client.post("/api/pricing-rules", json={"code": "NEW", "name": "Source rule", "copyFromRuleId": source_id})

        assert invalid.status_code == 400
        assert "pricing rule not found" in invalid.json()["detail"]
        assert duplicate_name.status_code == 400
        assert duplicate_name.json()["detail"] == "name must be unique"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_copy_does_not_copy_price_format_assignments():
    Session = _db()
    with Session() as db:
        source = _full_source(db)
        db.flush()
        db.add(PriceFormat(code="FMT", name="FMT", pricing_rule_id=source.id, pricing_rule=source.code))
        db.commit()

        copied = _copy(db, source.id)

        assert db.query(PriceFormat).filter(PriceFormat.pricing_rule_id == source.id).count() == 1
        assert db.query(PriceFormat).filter(PriceFormat.pricing_rule_id == copied.id).count() == 0
