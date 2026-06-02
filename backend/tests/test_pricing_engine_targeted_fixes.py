from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import (
    BendRange,
    CalculatedPrice,
    CompetitorPrice,
    MarkupRange,
    NoCompetitorMarkupRange,
    PriceFormat,
    PriceList,
    Product,
    RoundingRule,
)
from backend.app.services.competitors.code_mappings import list_catalog_code_mappings
from backend.app.main import _generated_item_dict
from backend.app.services.pricing import calculate_price_for_product, calculate_price_zone, calculate_prices
from backend.app.services.pricing_workflow.analytics import build_workflow_analytics


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _format(db, *, rounding=None):
    pf = PriceFormat(code="0001", name="0001", branch="Almaty")
    db.add(pf)
    db.flush()
    if rounding is not None:
        pf.rounding_rule_id = rounding.id
    db.add(MarkupRange(price_format_id=pf.id, cost_from=0, cost_to=None, markup_percent=0.15))
    db.add(NoCompetitorMarkupRange(price_format_id=pf.id, cost_from=0, cost_to=None, markup_percent=0.03))
    db.add(BendRange(price_format_id=pf.id, price_from=0, bend_percent=0.5))
    db.add(BendRange(price_format_id=pf.id, price_from=2000, bend_percent=0.2))
    db.add(BendRange(price_format_id=pf.id, price_from=5000, bend_percent=0.1))
    db.flush()
    return pf


def _product(db, *, code="SKU-1", cost=2500):
    row = Product(code=code, name=code, cost=cost)
    db.add(row)
    db.flush()
    return row


def _competitor(db, pf, product, source, price):
    db.add(CompetitorPrice(price_format_id=pf.id, product_id=None, source_name=source, supplier=source, coefficient=1))
    db.add(CompetitorPrice(price_format_id=pf.id, product_id=product.id, source_name=source, supplier=source, source_price=price, coefficient=1))
    db.flush()


def test_bend_is_selected_by_cost_not_competitor_price():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=2500)
    _competitor(db, pf, product, "manual:expensive", 9000)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(debug["bend_percent_used"]) == 0.2
    assert float(debug["chosen_competitor_price"]) == 9000
    assert float(price) == 8982.0


def test_competitor_iteration_chooses_first_candidate_at_or_above_mdc():
    db = _session()
    rounding = RoundingRule(code="R01", name="R01", mode="math", precision=2, step=0.01)
    db.add(rounding)
    db.flush()
    pf = _format(db, rounding=rounding)
    product = _product(db, cost=2500)
    _competitor(db, pf, product, "manual:low", 2850)
    _competitor(db, pf, product, "manual:second", 2900)
    _competitor(db, pf, product, "manual:high", 3000)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert debug["chosen_competitor_source"] == "manual:second"
    assert debug["chosen_competitor_rank"] == 2
    assert float(price) == 2894.2
    assert debug["zone"] == "left"


def test_competitor_price_never_falls_below_mdc_after_rounding():
    db = _session()
    rounding = RoundingRule(code="DOWN", name="DOWN", mode="down", precision=2, step=0.01)
    db.add(rounding)
    db.flush()
    pf = _format(db, rounding=rounding)
    db.query(MarkupRange).filter(MarkupRange.price_format_id == pf.id).delete()
    db.add(MarkupRange(price_format_id=pf.id, cost_from=0, cost_to=None, markup_percent=0))
    product = _product(db, cost=100.009)
    _competitor(db, pf, product, "manual:low", 100)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(debug["base_price"]) == 100.009
    assert float(price) == 100.01
    assert price >= debug["base_price"]


def test_rounding_rule_from_price_format_is_used():
    db = _session()
    rounding = RoundingRule(code="UP05", name="UP05", mode="up", precision=2, step=0.05)
    db.add(rounding)
    db.flush()
    pf = _format(db, rounding=rounding)
    product = _product(db, cost=10)
    _competitor(db, pf, product, "manual:source", 12.01)

    price, _debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(price) == 11.95


def test_no_competitor_candidate_below_mdc_is_bumped_to_mdc():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=100)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert debug["reason"] == "no_competitor_markup_bumped_to_mdc"
    assert float(price) == 115.0
    assert price >= debug["base_price"]


def test_no_competitor_candidate_above_mdc_is_kept():
    db = _session()
    pf = _format(db)
    db.query(NoCompetitorMarkupRange).filter(NoCompetitorMarkupRange.price_format_id == pf.id).delete()
    db.add(NoCompetitorMarkupRange(price_format_id=pf.id, cost_from=0, cost_to=None, markup_percent=0.20))
    db.flush()
    product = _product(db, cost=100)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert debug["reason"] == "no_competitor_markup"
    assert float(price) == 120.0


def test_calculate_prices_stores_explicit_competitor_fields():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=2500)
    _competitor(db, pf, product, "manual:low", 2850)
    _competitor(db, pf, product, "manual:second", 2900)

    calculate_prices(
        db=db,
        price_format_code=pf.code,
        price_list_number="PL-1",
        as_of=date.today(),
        activation_date=None,
        user="test",
        force_new_price_list=True,
    )

    cp = db.query(CalculatedPrice).one()
    assert float(cp.lowest_competitor_price) == 2850
    assert float(cp.chosen_competitor_price) == 2900
    assert float(cp.price_from_competitor) == 2894.2
    assert float(cp.bend_percent_used) == 0.2
    assert float(cp.markup_percent_used) == 15
    assert cp.zone == "left"


def test_zone_uses_chosen_competitor_when_lowest_is_below_mdc():
    zone, reference, deviation = calculate_price_zone(
        2894.2,
        chosen_competitor_price=2900,
        lowest_competitor_price=2850,
    )

    assert zone == "left"
    assert float(reference) == 2900
    assert float(deviation) < 0


def test_zone_optimal_within_three_percent_above_chosen_competitor():
    zone, reference, deviation = calculate_price_zone(
        1020,
        chosen_competitor_price=1000,
        lowest_competitor_price=900,
    )

    assert zone == "optimal"
    assert float(reference) == 1000
    assert round(float(deviation), 2) == 0.02


def test_zone_right_more_than_three_percent_above_chosen_competitor():
    zone, reference, deviation = calculate_price_zone(
        1050,
        chosen_competitor_price=1000,
        lowest_competitor_price=900,
    )

    assert zone == "right"
    assert float(reference) == 1000
    assert round(float(deviation), 2) == 0.05


def test_zone_falls_back_to_lowest_competitor_when_chosen_missing():
    zone, reference, deviation = calculate_price_zone(
        1020,
        chosen_competitor_price=None,
        lowest_competitor_price=1000,
    )

    assert zone == "optimal"
    assert float(reference) == 1000
    assert round(float(deviation), 2) == 0.02


def test_zone_no_data_without_any_competitor_reference():
    zone, reference, deviation = calculate_price_zone(
        1020,
        chosen_competitor_price=None,
        lowest_competitor_price=None,
    )

    assert zone == "no-data"
    assert reference is None
    assert deviation is None


def test_generated_item_markup_is_rule_markup_and_actual_margin_is_separate():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=100)
    pl = PriceList(number="PL-XLSX", price_format_id=pf.id, user="test")
    db.add(pl)
    db.flush()
    cp = CalculatedPrice(
        price_list_id=pl.id,
        product_id=product.id,
        cost=100,
        base_price=115,
        competitor_price=None,
        final_price=120,
        markup_percent_used=15,
        zone="no-data",
    )
    db.add(cp)
    db.flush()

    item = _generated_item_dict(db, cp, product, pf, None)

    assert item["markupPercent"] == 15
    assert item["actualMarginPercent"] == 20


def test_mapping_response_exposes_mapping_and_generated_coverage():
    db = _session()
    pf = _format(db)
    product_a = _product(db, code="A", cost=10)
    product_b = _product(db, code="B", cost=20)
    pl = PriceList(number="PL-COV", price_format_id=pf.id, user="test")
    db.add(pl)
    db.flush()
    db.add(CalculatedPrice(price_list_id=pl.id, product_id=product_a.id, cost=10, base_price=11, competitor_price=12, final_price=12, zone="right"))
    db.add(CalculatedPrice(price_list_id=pl.id, product_id=product_b.id, cost=20, base_price=22, competitor_price=None, final_price=21, zone="no-data"))
    db.flush()

    payload = list_catalog_code_mappings(db=db, platform="provisor", price_format_id=pf.id, limit=10)

    metric = payload["metrics"][0]
    assert "mappingCoveragePercent" in metric
    assert metric["generatedPricingCoverage"]["total"] == 2
    assert metric["generatedPricingCoverage"]["withCompetitors"] == 1
    assert metric["generatedPricingCoverage"]["withoutCompetitors"] == 1
    assert metric["generatedPricingCoverage"]["coveragePercent"] == 50


def test_analytics_uses_selected_price_list_and_explicit_bend():
    db = _session()
    pf = _format(db)
    product_a = _product(db, code="A", cost=100)
    product_b = _product(db, code="B", cost=100)
    first = PriceList(number="FIRST", price_format_id=pf.id, user="test")
    second = PriceList(number="SECOND", price_format_id=pf.id, user="test")
    db.add_all([first, second])
    db.flush()
    db.add(CalculatedPrice(price_list_id=first.id, product_id=product_a.id, cost=100, base_price=115, competitor_price=120, chosen_competitor_price=120, price_from_competitor=119.4, bend_percent_used=0.5, final_price=119.4, zone="left"))
    db.add(CalculatedPrice(price_list_id=second.id, product_id=product_b.id, cost=100, base_price=115, competitor_price=None, final_price=103, zone="no-data"))
    db.flush()

    analytics = build_workflow_analytics(db=db, price_list_id=first.id)

    assert analytics["summary"]["skuTotal"] == 1
    assert analytics["summary"]["withCompetitors"] == 1
    assert analytics["summary"]["withoutCompetitors"] == 0
    assert analytics["summary"]["averageBendPercent"] == 0.5


def test_analytics_splits_right_zone_reasons():
    db = _session()
    pf = _format(db)
    products = [_product(db, code=f"R{i}", cost=100) for i in range(3)]
    pl = PriceList(number="RIGHTS", price_format_id=pf.id, user="test")
    db.add(pl)
    db.flush()
    db.add(CalculatedPrice(price_list_id=pl.id, product_id=products[0].id, cost=100, base_price=115, competitor_price=100, final_price=115, zone="right"))
    db.add(CalculatedPrice(price_list_id=pl.id, product_id=products[1].id, cost=100, base_price=90, competitor_price=100, chosen_competitor_price=130, price_from_competitor=129, final_price=129, zone="right"))
    db.add(CalculatedPrice(price_list_id=pl.id, product_id=products[2].id, cost=100, base_price=90, competitor_price=100, final_price=120, zone="right", applied_list_ids="[7]"))
    db.flush()

    analytics = build_workflow_analytics(db=db, price_list_id=pl.id)

    assert analytics["rightZoneReasons"]["right_due_to_mdc_floor"] == 1
    assert analytics["rightZoneReasons"]["right_due_to_chosen_higher_competitor"] == 0
    assert analytics["rightZoneReasons"]["right_due_to_universal_override"] == 1


def test_analytics_recalculates_old_rows_without_chosen_competitor():
    db = _session()
    pf = _format(db)
    product = _product(db, code="OLD", cost=100)
    pl = PriceList(number="OLD-ZONE", price_format_id=pf.id, user="test")
    db.add(pl)
    db.flush()
    db.add(
        CalculatedPrice(
            price_list_id=pl.id,
            product_id=product.id,
            cost=100,
            base_price=115,
            competitor_price=100,
            final_price=102,
            zone="right",
        )
    )
    db.flush()

    analytics = build_workflow_analytics(db=db, price_list_id=pl.id)

    assert analytics["summary"]["optimalZone"] == 1
    assert analytics["summary"]["rightZone"] == 0
