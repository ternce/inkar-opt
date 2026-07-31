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


def _client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override_db():
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    return TestClient(app), Session


def test_pricing_rule_detail_returns_linked_template_settings():
    client, Session = _client()
    try:
        with Session() as db:
            markup = MarkupTemplate(code="M", name="Markup")
            bend = BendTemplate(code="B", name="Bend")
            no_competitor = NoCompetitorMarkupTemplate(code="N", name="No competitor")
            rounding = RoundingRule(code="R", name="Rounding", mode="math", precision=0)
            db.add_all([markup, bend, no_competitor, rounding])
            db.flush()
            db.add(MarkupTemplateRow(template_id=markup.id, cost_from=0, cost_to=999, markup_percent=12, sort_order=0))
            db.add(BendTemplateRow(template_id=bend.id, cost_from=0, cost_to=None, bend_percent=0.25, sort_order=0))
            db.add(NoCompetitorMarkupTemplateRow(template_id=no_competitor.id, cost_from=0, cost_to=None, markup_percent=7, sort_order=0))
            rule = PricingRule(
                code="RULE-A",
                name="Rule A",
                markup_template_id=markup.id,
                bend_template_id=bend.id,
                no_competitor_template_id=no_competitor.id,
                rounding_rule_id=rounding.id,
            )
            db.add(rule)
            db.commit()
            rule_id = rule.id

        response = client.get(f"/api/pricing-rules/{rule_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["markupTemplateId"] == payload["markupTemplate"]["id"]
        assert payload["bendTemplateId"] == payload["bendTemplate"]["id"]
        assert payload["noCompetitorTemplateId"] == payload["noCompetitorTemplate"]["id"]
        assert payload["roundingRuleId"] == payload["roundingRule"]["id"]
        assert payload["markupTemplate"]["rows"][0]["markupPercent"] == 12
        assert payload["bendTemplate"]["rows"][0]["bendPercent"] == 0.25
        assert payload["noCompetitorTemplate"]["rows"][0]["markupPercent"] == 7
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_pricing_rule_detail_handles_rule_without_settings():
    client, Session = _client()
    try:
        with Session() as db:
            rule = PricingRule(code="EMPTY", name="Empty rule")
            db.add(rule)
            db.commit()
            rule_id = rule.id

        response = client.get(f"/api/pricing-rules/{rule_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["markupTemplateId"] is None
        assert payload["bendTemplateId"] is None
        assert payload["noCompetitorTemplateId"] is None
        assert payload["roundingRuleId"] is None
        assert payload["markupTemplate"] is None
        assert payload["bendTemplate"] is None
        assert payload["noCompetitorTemplate"] is None
        assert payload["roundingRule"] is None
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_pricing_rule_detail_handles_missing_linked_template_without_fallback():
    client, Session = _client()
    try:
        with Session() as db:
            rule = PricingRule(code="MISSING", name="Missing template", markup_template_id=9999)
            db.add(rule)
            db.commit()
            rule_id = rule.id

        response = client.get(f"/api/pricing-rules/{rule_id}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["markupTemplateId"] == 9999
        assert payload["markupTemplate"] is None
        assert payload["bendTemplate"] is None
        assert payload["noCompetitorTemplate"] is None
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_pricing_rule_detail_does_not_change_existing_price_format_settings():
    client, Session = _client()
    try:
        with Session() as db:
            markup = MarkupTemplate(code="READ", name="Read only")
            db.add(markup)
            db.flush()
            db.add(MarkupTemplateRow(template_id=markup.id, cost_from=0, cost_to=None, markup_percent=99, sort_order=0))
            rule = PricingRule(code="READ-RULE", name="Read rule", markup_template_id=markup.id)
            pf = PriceFormat(code="FMT-READ", name="FMT-READ", branch="A", pricing_rule="old")
            db.add_all([rule, pf])
            db.commit()
            rule_id = rule.id
            format_id = pf.id

        response = client.get(f"/api/pricing-rules/{rule_id}")

        assert response.status_code == 200
        with Session() as db:
            pf = db.get(PriceFormat, format_id)
            assert pf.pricing_rule == "old"
            assert pf.pricing_rule_id is None
            assert pf.rounding_rule_id is None
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_applying_pricing_rule_persists_applied_template_identity_in_settings():
    client, Session = _client()
    try:
        with Session() as db:
            markup = MarkupTemplate(code="APPLY-M", name="Applied markup")
            bend = BendTemplate(code="APPLY-B", name="Applied bend")
            no_competitor = NoCompetitorMarkupTemplate(code="APPLY-N", name="Applied no competitor")
            rounding = RoundingRule(code="APPLY-R", name="Applied rounding", mode="math", precision=2)
            db.add_all([markup, bend, no_competitor, rounding])
            db.flush()
            db.add(MarkupTemplateRow(template_id=markup.id, cost_from=0, cost_to=1000, markup_percent=20, sort_order=0))
            db.add(BendTemplateRow(template_id=bend.id, cost_from=0, cost_to=None, bend_percent=0.5, sort_order=0))
            db.add(NoCompetitorMarkupTemplateRow(template_id=no_competitor.id, cost_from=0, cost_to=None, markup_percent=15, sort_order=0))
            rule = PricingRule(
                code="APPLY-RULE",
                name="Apply rule",
                markup_template_id=markup.id,
                bend_template_id=bend.id,
                no_competitor_template_id=no_competitor.id,
                rounding_rule_id=rounding.id,
            )
            pf = PriceFormat(code="FMT-APPLY", name="FMT-APPLY", branch="A")
            db.add_all([rule, pf])
            db.commit()
            ids = {
                "rule": rule.id,
                "markup": markup.id,
                "bend": bend.id,
                "no_competitor": no_competitor.id,
                "rounding": rounding.id,
            }

        applied = client.post("/api/price-formats/FMT-APPLY/pricing-rule", json={"pricingRuleId": ids["rule"]})
        assert applied.status_code == 200

        settings = client.get("/api/price-formats/FMT-APPLY/settings")
        assert settings.status_code == 200
        payload = settings.json()
        assert payload["appliedMarkupTemplateId"] == ids["markup"]
        assert payload["appliedBendTemplateId"] == ids["bend"]
        assert payload["appliedNoCompetitorTemplateId"] == ids["no_competitor"]
        assert payload["appliedRoundingRuleId"] == ids["rounding"]
        assert payload["recommendedMarkups"][0]["markupPercent"] == 20
        assert payload["bendRanges"][0]["bendPercent"] == 0.5
        assert payload["noCompetitorMarkups"][0]["markupPercent"] == 15
    finally:
        app.dependency_overrides.pop(get_db, None)
