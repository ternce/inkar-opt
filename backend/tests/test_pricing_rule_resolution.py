from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.deps import get_current_user, get_db, require_write_access
from backend.app.main import app
from backend.app.models import (
    AppUser,
    BendTemplate,
    BendTemplateRow,
    MarkupTemplate,
    MarkupTemplateRow,
    NoCompetitorMarkupTemplate,
    NoCompetitorMarkupTemplateRow,
    PriceFormat,
    PriceList,
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

    def override_user():
        return AppUser(id=1, username="test-admin", display_name="Test admin", role="admin", is_active=True)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[require_write_access] = override_user
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


def test_pricing_rule_update_without_selector_edits_preserves_relationships():
    client, Session = _client()
    try:
        with Session() as db:
            markup = MarkupTemplate(code="KEEP-M", name="Keep markup")
            bend = BendTemplate(code="KEEP-B", name="Keep bend")
            no_competitor = NoCompetitorMarkupTemplate(code="KEEP-N", name="Keep no competitor")
            rounding = RoundingRule(code="KEEP-R", name="Keep rounding", mode="math", precision=2)
            db.add_all([markup, bend, no_competitor, rounding])
            db.flush()
            rule = PricingRule(
                code="KEEP-RULE",
                name="Keep rule",
                markup_template_id=markup.id,
                bend_template_id=bend.id,
                no_competitor_template_id=no_competitor.id,
                rounding_rule_id=rounding.id,
            )
            db.add(rule)
            db.commit()
            ids = {
                "rule": rule.id,
                "markup": markup.id,
                "bend": bend.id,
                "no_competitor": no_competitor.id,
                "rounding": rounding.id,
            }

        detail = client.get(f"/api/pricing-rules/{ids['rule']}")
        assert detail.status_code == 200
        payload = detail.json()
        response = client.patch(f"/api/pricing-rules/{ids['rule']}", json=payload)

        assert response.status_code == 200
        saved = response.json()
        assert saved["markupTemplateId"] == ids["markup"]
        assert saved["bendTemplateId"] == ids["bend"]
        assert saved["noCompetitorTemplateId"] == ids["no_competitor"]
        assert saved["roundingRuleId"] == ids["rounding"]
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_pricing_rule_update_changes_one_relationship_without_clearing_others():
    client, Session = _client()
    try:
        with Session() as db:
            markup = MarkupTemplate(code="ONE-M", name="One markup")
            old_bend = BendTemplate(code="ONE-B1", name="Old bend")
            new_bend = BendTemplate(code="ONE-B2", name="New bend")
            no_competitor = NoCompetitorMarkupTemplate(code="ONE-N", name="One no competitor")
            rounding = RoundingRule(code="ONE-R", name="One rounding", mode="math", precision=2)
            db.add_all([markup, old_bend, new_bend, no_competitor, rounding])
            db.flush()
            rule = PricingRule(
                code="ONE-RULE",
                name="One rule",
                markup_template_id=markup.id,
                bend_template_id=old_bend.id,
                no_competitor_template_id=no_competitor.id,
                rounding_rule_id=rounding.id,
            )
            db.add(rule)
            db.commit()
            ids = {
                "rule": rule.id,
                "markup": markup.id,
                "new_bend": new_bend.id,
                "no_competitor": no_competitor.id,
                "rounding": rounding.id,
            }

        payload = client.get(f"/api/pricing-rules/{ids['rule']}").json()
        payload["bendTemplateId"] = ids["new_bend"]
        response = client.patch(f"/api/pricing-rules/{ids['rule']}", json=payload)

        assert response.status_code == 200
        saved = response.json()
        assert saved["markupTemplateId"] == ids["markup"]
        assert saved["bendTemplateId"] == ids["new_bend"]
        assert saved["noCompetitorTemplateId"] == ids["no_competitor"]
        assert saved["roundingRuleId"] == ids["rounding"]
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_price_format_branch_formats_display_linked_pricing_rule_name_without_assignment_change():
    client, Session = _client()
    try:
        with Session() as db:
            rule = PricingRule(code="111111", name="Общее_1,25")
            db.add(rule)
            db.flush()
            pf = PriceFormat(code="PF-LINKED", name="Linked", branch="Pavlodar", pricing_rule="111111", pricing_rule_id=rule.id)
            db.add(pf)
            db.commit()
            ids = {"format": pf.id, "rule": rule.id}

        response = client.get("/api/pricing-workflow/branch-formats", params={"branch_id": "Pavlodar"})

        assert response.status_code == 200
        payload = response.json()
        row = next(item for item in payload if item["code"] == "PF-LINKED")
        assert row["pricingRule"] == "Общее_1,25"
        assert row["pricingRuleName"] == "Общее_1,25"
        assert row["pricingRuleCode"] == "111111"
        assert row["pricingRuleId"] == ids["rule"]
        with Session() as db:
            pf = db.get(PriceFormat, ids["format"])
            assert pf.pricing_rule == "111111"
            assert pf.pricing_rule_id == ids["rule"]
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_price_format_settings_display_legacy_code_as_pricing_rule_name_when_code_matches():
    client, Session = _client()
    try:
        with Session() as db:
            db.add(PricingRule(code="111112", name="Общее_0,75"))
            db.add(PriceFormat(code="PF-LEGACY-CODE", name="Legacy code", branch="Kostanay", pricing_rule="111112"))
            db.commit()

        response = client.get("/api/price-formats/PF-LEGACY-CODE/settings")

        assert response.status_code == 200
        payload = response.json()
        assert payload["pricingRule"] == "Общее_0,75"
        assert payload["pricingRuleName"] == "Общее_0,75"
        assert payload["pricingRuleCode"] == "111112"
        assert payload["pricingRuleId"] is None
        with Session() as db:
            pf = db.execute(select(PriceFormat).where(PriceFormat.code == "PF-LEGACY-CODE")).scalars().first()
            assert pf.pricing_rule == "111112"
            assert pf.pricing_rule_id is None
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_price_format_settings_preserve_raw_legacy_pricing_rule_when_no_code_match():
    client, Session = _client()
    try:
        with Session() as db:
            db.add(PriceFormat(code="PF-RAW", name="Raw", branch="Kostanay", pricing_rule="legacy raw"))
            db.commit()

        response = client.get("/api/price-formats/PF-RAW/settings")

        assert response.status_code == 200
        payload = response.json()
        assert payload["pricingRule"] == "legacy raw"
        assert payload["pricingRuleName"] == "legacy raw"
        assert payload["pricingRuleCode"] == ""
        assert payload["pricingRuleId"] is None
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_price_format_settings_display_empty_rule_as_existing_api_empty_value():
    client, Session = _client()
    try:
        with Session() as db:
            db.add(PriceFormat(code="PF-NONE", name="No rule", branch="Semey"))
            db.commit()

        response = client.get("/api/price-formats/PF-NONE/settings")

        assert response.status_code == 200
        payload = response.json()
        assert payload["pricingRule"] == ""
        assert payload["pricingRuleName"] == ""
        assert payload["pricingRuleCode"] == ""
        assert payload["pricingRuleId"] is None
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_generated_price_lists_display_canonical_pricing_rule_name():
    client, Session = _client()
    try:
        with Session() as db:
            rule = PricingRule(code="01", name="Общее_1,0")
            db.add(rule)
            db.flush()
            pf = PriceFormat(code="PF-GENERATED", name="Generated", branch="Kostanay", pricing_rule="01", pricing_rule_id=rule.id)
            db.add(pf)
            db.flush()
            db.add(PriceList(number="PL-GENERATED", price_format_id=pf.id, user="tester"))
            db.commit()

        response = client.get("/api/generated-price-lists", params={"format_code": "PF-GENERATED"})

        assert response.status_code == 200
        payload = response.json()
        assert payload[0]["pricingRule"] == "Общее_1,0"
        assert payload[0]["pricingRuleName"] == "Общее_1,0"
        assert payload[0]["pricingRuleCode"] == "01"
    finally:
        app.dependency_overrides.pop(get_db, None)
