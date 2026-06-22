from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import (
    BendRange,
    CalculatedPrice,
    CompetitorPrice,
    ListItem,
    MarkupRange,
    NoCompetitorMarkupRange,
    PriceFormat,
    PriceList,
    Product,
    RoundingRule,
    UniversalList,
    UniversalListPriceFormat,
)
from backend.app.services.competitors.code_mappings import list_catalog_code_mappings
from backend.app.main import _competitor_column_title, _generated_item_dict
from backend.app.services.pricing import calculate_price_for_product, calculate_price_zone, calculate_prices
from backend.app.services.pricing_workflow.analytics import build_workflow_analytics
from backend.app.services.pricing_workflow.exports import _export_zone


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


def _list_item(db, product, list_type, value, *, pf=None, status="active", start_date=None, end_date=None):
    row = UniversalList(
        code=f"UL-{list_type}-{product.code}",
        name=f"{list_type} list",
        type=list_type,
        status=status,
        start_date=start_date,
        end_date=end_date,
        price_format_id=pf.id if pf is not None else None,
    )
    db.add(row)
    db.flush()
    db.add(ListItem(universal_list_id=row.id, product_id=product.id, value=value))
    db.flush()
    return row


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Медсервис (Костанай) — Медсервис (Костанай) — Aksai4/83", "Медсервис (Костанай) — Aksai4/83"),
        ("Стофарм средняя цена (Астана) — Стофарм средняя цена (Астана) — magMereke", "Стофарм средняя цена (Астана) — magMereke"),
        ("Optimus pharm (Астана) — Optimus pharm (Астана) — 8mkr8a", "Optimus pharm (Астана) — 8mkr8a"),
        ("Источник — Другой филиал — account", "Источник — Другой филиал — account"),
    ],
)
def test_competitor_column_title_removes_only_adjacent_duplicates(raw, expected):
    assert _competitor_column_title(raw) == expected


def test_bend_is_selected_by_competitor_price_not_product_cost():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=2500)
    _competitor(db, pf, product, "manual:expensive", 9000)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(debug["bend_percent_used"]) == 0.1
    assert float(debug["chosen_competitor_price"]) == 9000
    assert float(price) == 8991.0


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

    assert debug["chosen_competitor_source"] == "manual:high"
    assert debug["chosen_competitor_rank"] == 3
    assert float(price) == 2994.0
    assert debug["zone"] == "right"


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
    assert float(price) == 117.65
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
    assert float(price) == 125.0
    assert debug["zone"] is None
    assert debug["log"] == "Нет цен выбранных конкурентов. Применена глобальная шкала маржи для товаров без конкурентов."
    assert "20" not in debug["log"]


def test_no_competitor_list_override_keeps_percentage_only_in_second_log():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=100)
    row = _list_item(db, product, "fixed_markup", 10.5, pf=pf)

    calculate_prices(
        db=db,
        price_format_code=pf.code,
        price_list_number="PL-NO-COMP-LIST-LOG",
        as_of=date.today(),
        activation_date=None,
        user="test",
        force_new_price_list=True,
    )

    cp = db.query(CalculatedPrice).one()
    payload = _generated_item_dict(db, cp, product, pf, None)
    assert "%" not in payload["pricingCalculationLog"]
    assert payload["listOverrideLog"]["listType"] == "fixed_markup"
    assert payload["listOverrideLog"]["displayValue"] == "10.5%"
    assert payload["appliedListId"] == row.id


@pytest.mark.parametrize(
    ("cost", "margin_fraction", "expected_mdc"),
    [
        (250, 0.25, 333.33),
        (1000, 0.25, 1333.33),
    ],
)
def test_global_margin_range_uses_margin_formula(cost, margin_fraction, expected_mdc):
    db = _session()
    pf = _format(db)
    db.query(MarkupRange).filter(MarkupRange.price_format_id == pf.id).delete()
    db.add(MarkupRange(price_format_id=pf.id, cost_from=0, cost_to=None, markup_percent=margin_fraction))
    db.flush()
    product = _product(db, cost=cost)

    _price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert round(float(debug["mdc_price"]), 2) == expected_mdc


def test_fixed_markup_25_uses_margin_formula_for_mdc():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=250)
    _list_item(db, product, "fixed_markup", 25, pf=pf)

    _price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(debug["mdc_markup_percent"]) == 25.0
    assert round(float(debug["mdc_price"]), 2) == 333.33


def test_fixed_price_list_overrides_generated_price():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=100)
    row = _list_item(db, product, "fixed_price", 1200, pf=pf)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(price) == 1200
    assert debug["reason"] == "fixed_price_list"
    assert debug["applied_list_ids"] == [row.id]


def test_fixed_price_2500_is_final_price_not_percent_markup():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=100)
    row = _list_item(db, product, "fixed_price", 2500, pf=pf)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(price) == 2500.0
    assert float(debug["mdc_markup_percent"]) == 15.0
    assert round(float(debug["mdc_price"]), 2) == 117.65
    assert debug["applied_rule_type"] == "fixed_price"
    assert debug["applied_list_ids"] == [row.id]


def test_fixed_markup_list_overrides_default_markup():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=100)
    row = _list_item(db, product, "fixed_markup", 5, pf=pf)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(price) == 105.27
    assert debug["reason"] == "mdc_floor_after_rounding"
    assert debug["applied_list_ids"] == [row.id]
    assert debug["applied_rule_type"] == "fixed_markup"
    assert float(debug["mdc_markup_percent"]) == 5.0
    assert round(float(debug["mdc_price"]), 2) == 105.26


def test_fixed_markup_one_is_one_percent_not_one_hundred_percent():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=100)
    row = _list_item(db, product, "fixed_markup", 1, pf=pf)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(price) == 101.02
    assert debug["applied_rule_type"] == "fixed_markup"
    assert debug["applied_list_ids"] == [row.id]
    assert float(debug["mdc_markup_percent"]) == 1.0
    assert round(float(debug["mdc_price"]), 2) == 101.01


def test_lists_management_active_fixed_markup_m2m_overrides_default_markup():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=1000)
    row = UniversalList(code="UL-FIXED-MARKUP", name="Fixed markup", type="fixed_markup", status="Активный")
    db.add(row)
    db.flush()
    db.add(ListItem(universal_list_id=row.id, product_id=product.id, value=33))
    db.add(UniversalListPriceFormat(universal_list_id=row.id, price_format_id=pf.id))
    db.flush()

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(price) == 1492.54
    assert debug["reason"] == "no_competitor_markup"
    assert debug["applied_list_ids"] == [row.id]
    assert debug["applied_rule_type"] == "fixed_markup"
    assert float(debug["applied_rule_value"]) == 33.0
    assert float(debug["mdc_markup_percent"]) == 33.0
    assert round(float(debug["mdc_price"]), 2) == 1492.54
    assert debug["applied_list_name"] == "Fixed markup"
    assert "Нет цен выбранных конкурентов" in debug["log"]
    assert "Applied Rule" not in debug["log"]


@pytest.mark.parametrize(
    ("critical_margin", "expected_mdc", "expected_price"),
    [(10, 111.11, 111.12), (35, 153.85, 153.85)],
)
def test_critical_markup_overrides_global_margin_for_mdc_and_no_competitor(critical_margin, expected_mdc, expected_price):
    db = _session()
    pf = _format(db)
    db.query(MarkupRange).filter(MarkupRange.price_format_id == pf.id).delete()
    db.query(NoCompetitorMarkupRange).filter(NoCompetitorMarkupRange.price_format_id == pf.id).delete()
    db.add(MarkupRange(price_format_id=pf.id, cost_from=0, cost_to=None, markup_percent=0.25))
    db.add(NoCompetitorMarkupRange(price_format_id=pf.id, cost_from=0, cost_to=None, markup_percent=0.25))
    product = _product(db, cost=100)
    row = _list_item(db, product, "critical_markup", critical_margin, pf=pf)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert round(float(price), 2) == expected_price
    assert float(debug["effective_markup_percent"]) == critical_margin
    assert float(debug["mdc_markup_percent"]) == critical_margin
    assert round(float(debug["mdc_price"]), 2) == expected_mdc
    assert debug["applied_list_ids"] == [row.id]


def test_fixed_markup_only_overrides_mdc_and_keeps_competitor_bend_workflow():
    db = _session()
    pf = _format(db)
    db.query(MarkupRange).filter(MarkupRange.price_format_id == pf.id).delete()
    db.query(NoCompetitorMarkupRange).filter(NoCompetitorMarkupRange.price_format_id == pf.id).delete()
    db.query(BendRange).filter(BendRange.price_format_id == pf.id).delete()
    db.add(MarkupRange(price_format_id=pf.id, cost_from=0, cost_to=None, markup_percent=0.25))
    db.add(NoCompetitorMarkupRange(price_format_id=pf.id, cost_from=0, cost_to=None, markup_percent=0.25))
    db.add(BendRange(price_format_id=pf.id, price_from=0, bend_percent=5))
    db.flush()
    product = _product(db, code="000000000001000218", cost=278)
    row = UniversalList(code="010101", name="010101", type="fixed_markup", status="active")
    db.add(row)
    db.flush()
    db.add(ListItem(universal_list_id=row.id, product_id=product.id, value=5.5))
    db.add(UniversalListPriceFormat(universal_list_id=row.id, price_format_id=pf.id))
    _competitor(db, pf, product, "manual:c1", 242.95)
    _competitor(db, pf, product, "manual:c2", 275)
    _competitor(db, pf, product, "manual:c3", 315)
    db.flush()

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(debug["effective_markup_percent"]) == 5.5
    assert round(float(debug["mdc_price"]), 2) == 294.18
    assert float(debug["selected_competitor_price"]) == 315
    assert float(debug["chosen_competitor_price"]) == 315
    assert float(debug["bend_percent_used"]) == 5
    assert round(float(debug["competitor_candidate_price"]), 2) == 299.25
    assert round(float(debug["price_from_competitor"]), 2) == 299.25
    assert round(float(price), 2) == 299.25
    assert round(float(debug["final_price"]), 2) == 299.25
    assert debug["applied_rule_type"] == "fixed_markup"
    assert float(debug["applied_rule_value"]) == 5.5
    assert debug["applied_list_id"] == row.id
    assert debug["applied_list_name"] == "010101"
    assert [round(float(item["candidate"]), 2) for item in debug["rejected_competitors"]] == [230.80, 261.25]


def test_fixed_markup_sets_mdc_and_competitor_below_mdc_uses_mdc():
    db = _session()
    pf = _format(db)
    db.query(BendRange).filter(BendRange.price_format_id == pf.id).delete()
    db.add(BendRange(price_format_id=pf.id, price_from=0, bend_percent=0))
    db.flush()
    product = _product(db, cost=90.72)
    row = _list_item(db, product, "fixed_markup", 33, pf=pf)
    _competitor(db, pf, product, "manual:low", 12)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(price) == 135.41
    assert debug["reason"] == "mdc_floor_after_rounding"
    assert debug["applied_list_ids"] == [row.id]
    assert debug["applied_rule_type"] == "fixed_markup"
    assert float(debug["applied_rule_value"]) == 33.0
    assert float(debug["mdc_markup_percent"]) == 33.0
    assert round(float(debug["mdc_price"]), 2) == 135.4
    assert float(debug["competitor_candidate_price"]) == 12.0
    assert float(debug["final_price"]) == 135.41


def test_fixed_markup_sets_mdc_and_competitor_above_mdc_wins():
    db = _session()
    pf = _format(db)
    db.query(BendRange).filter(BendRange.price_format_id == pf.id).delete()
    db.add(BendRange(price_format_id=pf.id, price_from=0, bend_percent=0))
    db.flush()
    product = _product(db, cost=90.72)
    row = _list_item(db, product, "fixed_markup", 33, pf=pf)
    _competitor(db, pf, product, "manual:high", 200)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(price) == 200.0
    assert debug["reason"] == "competitor_bend"
    assert debug["applied_list_ids"] == [row.id]
    assert debug["applied_rule_type"] == "fixed_markup"
    assert float(debug["applied_rule_value"]) == 33.0
    assert float(debug["mdc_markup_percent"]) == 33.0
    assert round(float(debug["mdc_price"]), 2) == 135.4
    assert float(debug["competitor_candidate_price"]) == 200.0
    assert float(debug["final_price"]) == 200.0


def test_min_and_max_markup_lists_apply_bounds():
    db = _session()
    pf = _format(db)
    product_min = _product(db, code="MIN-MARKUP", cost=100)
    product_max = _product(db, code="MAX-MARKUP", cost=100)
    _competitor(db, pf, product_min, "manual:min", 105)
    _competitor(db, pf, product_max, "manual:max", 200)
    min_list = _list_item(db, product_min, "min_markup", 25, pf=pf)
    max_list = _list_item(db, product_max, "max_markup", 10, pf=pf)

    min_price, min_debug = calculate_price_for_product(db=db, product=product_min, price_format=pf, as_of=date.today())
    max_price, max_debug = calculate_price_for_product(db=db, product=product_max, price_format=pf, as_of=date.today())

    assert float(min_price) == 133.34
    assert min_debug["applied_list_ids"] == [min_list.id]
    assert float(max_price) == 199.0
    assert max_debug["applied_list_ids"] == [max_list.id]


def test_min_and_max_price_lists_apply_bounds():
    db = _session()
    pf = _format(db)
    product_min = _product(db, code="MIN-PRICE", cost=100)
    product_max = _product(db, code="MAX-PRICE", cost=100)
    _competitor(db, pf, product_min, "manual:min-price", 105)
    _competitor(db, pf, product_max, "manual:max-price", 200)
    min_list = _list_item(db, product_min, "min_price", 150, pf=pf)
    max_list = _list_item(db, product_max, "max_price", 130, pf=pf)

    min_price, min_debug = calculate_price_for_product(db=db, product=product_min, price_format=pf, as_of=date.today())
    max_price, max_debug = calculate_price_for_product(db=db, product=product_max, price_format=pf, as_of=date.today())

    assert float(min_price) == 150.0
    assert min_debug["applied_list_ids"] == [min_list.id]
    assert float(max_price) == 130.0
    assert max_debug["applied_list_ids"] == [max_list.id]


def test_min_price_1200_clamps_final_price_upward():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=100)
    row = _list_item(db, product, "min_price", 1200, pf=pf)
    _competitor(db, pf, product, "manual:below-min-price", 500)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(price) == 1200.0
    assert debug["reason"] == "min_price_floor"
    assert debug["applied_list_ids"] == [row.id]
    assert debug["applied_rule_type"] == "min_price"
    assert float(debug["applied_rule_value"]) == 1200.0


@pytest.mark.parametrize(
    ("calculated_price", "min_price", "rounding_step", "expected"),
    [
        (7226, 7300, 72.26, 7300),
        (2163, 2200, 43.26, 2200),
        (8000, 7300, 0.01, 8000),
    ],
)
def test_min_price_is_hard_final_bound_after_rounding(calculated_price, min_price, rounding_step, expected):
    db = _session()
    rounding = RoundingRule(code=f"DOWN-{rounding_step}", name="Down", mode="down", precision=2, step=rounding_step)
    db.add(rounding)
    db.flush()
    pf = _format(db, rounding=rounding)
    db.query(BendRange).filter(BendRange.price_format_id == pf.id).delete()
    db.add(BendRange(price_format_id=pf.id, price_from=0, bend_percent=0))
    product = _product(db, code=f"MIN-{calculated_price}", cost=100)
    row = _list_item(db, product, "min_price", min_price, pf=pf)
    _competitor(db, pf, product, f"manual:{calculated_price}", calculated_price)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(price) == expected
    assert float(debug["final_price"]) == expected
    assert debug["applied_list_ids"] == [row.id]


def test_api_payload_keeps_pricing_and_max_price_list_logs_independent():
    db = _session()
    pf = _format(db)
    db.query(BendRange).filter(BendRange.price_format_id == pf.id).delete()
    db.add(BendRange(price_format_id=pf.id, price_from=0, bend_percent=0))
    product = _product(db, code="DUAL-LOG-MAX", cost=100)
    row = _list_item(db, product, "max_price", 820, pf=pf)
    _competitor(db, pf, product, "manual:first", 900)

    calculate_prices(
        db=db,
        price_format_code=pf.code,
        price_list_number="PL-DUAL-LOG-MAX",
        as_of=date.today(),
        activation_date=None,
        user="test",
        force_new_price_list=True,
    )

    cp = db.query(CalculatedPrice).one()
    payload = _generated_item_dict(db, cp, product, pf, None)
    assert payload["pricingCalculationLog"].startswith("Цена рассчитана относительно 1-й цены конкурента")
    assert payload["listOverrideLog"] == {
        "listName": "max_price list",
        "listCode": "UL-max_price-DUAL-LOG-MAX",
        "listType": "max_price",
        "value": 820.0,
        "displayValue": "820",
        "action": "Применено ограничение максимальной цены.",
        "ambiguous": False,
    }
    assert payload["appliedListId"] == row.id
    assert float(payload["finalPrice"]) == 820.0


def test_max_price_3000_clamps_final_price_downward():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=100)
    row = _list_item(db, product, "max_price", 3000, pf=pf)
    _competitor(db, pf, product, "manual:above-max-price", 5000)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(price) == 3000.0
    assert debug["reason"] == "max_price_cap"
    assert debug["applied_list_ids"] == [row.id]
    assert debug["applied_rule_type"] == "max_price"
    assert float(debug["applied_rule_value"]) == 3000.0


def test_exclusion_list_marks_product_without_normal_pricing():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=100)
    row = _list_item(db, product, "exclude_from_pricing", 1, pf=pf)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(price) == 100.0
    assert debug["reason"] == "exclude_from_pricing_list"
    assert debug["excluded_from_pricing"] is True
    assert debug["applied_list_ids"] == [row.id]


def test_excluded_product_is_absent_from_generated_price_rows():
    db = _session()
    pf = _format(db)
    excluded = _product(db, code="EXCLUDED", cost=100)
    included = _product(db, code="INCLUDED", cost=100)
    _list_item(db, excluded, "exclude_from_pricing", 1, pf=pf)

    count = calculate_prices(
        db=db,
        price_format_code=pf.code,
        price_list_number="PL-EXCLUSION",
        as_of=date.today(),
        activation_date=None,
        user="test",
        force_new_price_list=True,
    )

    assert count == 1
    rows = db.query(CalculatedPrice).all()
    assert [row.product_id for row in rows] == [included.id]


def test_exclusion_removes_existing_row_when_price_list_is_regenerated():
    db = _session()
    pf = _format(db)
    excluded = _product(db, code="LATE-EXCLUDED", cost=100)
    included = _product(db, code="STILL-INCLUDED", cost=100)

    initial_count = calculate_prices(
        db=db,
        price_format_code=pf.code,
        price_list_number="PL-REGENERATED-EXCLUSION",
        as_of=date.today(),
        activation_date=None,
        user="test",
    )
    assert initial_count == 2
    assert db.query(CalculatedPrice).filter(CalculatedPrice.product_id == excluded.id).one_or_none() is not None

    _list_item(db, excluded, "exclude_from_pricing", 1, pf=pf)
    regenerated_count = calculate_prices(
        db=db,
        price_format_code=pf.code,
        price_list_number="PL-REGENERATED-EXCLUSION",
        as_of=date.today(),
        activation_date=None,
        user="test",
    )

    assert regenerated_count == 1
    assert db.query(CalculatedPrice).filter(CalculatedPrice.product_id == excluded.id).one_or_none() is None
    assert db.query(CalculatedPrice).filter(CalculatedPrice.product_id == included.id).one_or_none() is not None


def test_no_bend_disables_bend_only_for_matching_product():
    db = _session()
    pf = _format(db)
    product = _product(db, code="NO-BEND", cost=100)
    control = _product(db, code="CONTROL", cost=100)
    row = _list_item(db, product, "no_bend", 1, pf=pf)
    _competitor(db, pf, product, "manual:no-bend", 200)
    _competitor(db, pf, control, "manual:control", 200)

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())
    control_price, control_debug = calculate_price_for_product(db=db, product=control, price_format=pf, as_of=date.today())

    assert float(price) == 200.0
    assert float(debug["bend_percent_used"]) == 0.0
    assert debug["applied_list_ids"] == [row.id]
    assert float(control_price) == 199.0
    assert float(control_debug["bend_percent_used"]) == 0.5
    assert control_debug["applied_list_ids"] == []


def test_same_type_list_conflict_aborts_generation_with_clear_message():
    db = _session()
    pf = _format(db)
    product = _product(db, code="CONFLICT-SKU", cost=100)
    first = _list_item(db, product, "fixed_price", 111, pf=pf)
    second = _list_item(db, product, "fixed_price", 222, pf=pf)

    with pytest.raises(ValueError) as exc_info:
        calculate_prices(
            db=db,
            price_format_code=pf.code,
            price_list_number="PL-CONFLICT",
            as_of=date.today(),
            activation_date=None,
            user="test",
            force_new_price_list=True,
        )

    message = str(exc_info.value)
    assert "Lists Management conflicts detected" in message
    assert "SKU=CONFLICT-SKU" in message
    assert "type=fixed_price" in message
    assert f"#{first.id}" in message
    assert f"#{second.id}" in message


def test_unconfirmed_rule_is_explicitly_marked_ambiguous_in_diagnostics_and_api():
    db = _session()
    pf = _format(db)
    product = _product(db, code="AMBIGUOUS", cost=100)
    row = _list_item(db, product, "min_markup", 25, pf=pf)

    calculate_prices(
        db=db,
        price_format_code=pf.code,
        price_list_number="PL-AMBIGUOUS",
        as_of=date.today(),
        activation_date=None,
        user="test",
        force_new_price_list=True,
    )

    cp = db.query(CalculatedPrice).one()
    payload = _generated_item_dict(db, cp, product, pf, None)
    assert cp.applied_rule_type == "min_markup"
    assert "AMBIGUOUS" not in cp.applied_reason
    assert payload["appliedRuleAmbiguous"] is True
    assert payload["appliedRule"] == {
        "applied_rule_type": "min_markup",
        "applied_rule_value": 25.0,
        "list_id": row.id,
        "list_name": "min_markup list",
        "list_code": "UL-min_markup-AMBIGUOUS",
        "ambiguous": True,
    }
    assert payload["listOverrideLog"]["ambiguous"] is True


def test_many_to_many_list_assignment_affects_matching_format_only():
    db = _session()
    pf = _format(db)
    other_pf = PriceFormat(code="0002", name="0002", branch="Almaty")
    db.add(other_pf)
    db.flush()
    db.add(MarkupRange(price_format_id=other_pf.id, cost_from=0, cost_to=None, markup_percent=0.15))
    product = _product(db, cost=100)
    row = UniversalList(code="UL-M2M", name="M2M", type="fixed_price", status="active")
    db.add(row)
    db.flush()
    db.add(ListItem(universal_list_id=row.id, product_id=product.id, value=777))
    db.add(UniversalListPriceFormat(universal_list_id=row.id, price_format_id=pf.id))
    db.flush()

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())
    other_price, other_debug = calculate_price_for_product(db=db, product=product, price_format=other_pf, as_of=date.today())

    assert float(price) == 777.0
    assert debug["applied_list_ids"] == [row.id]
    assert float(other_price) == 117.65
    assert other_debug["applied_list_ids"] == []


def test_inactive_and_out_of_date_lists_are_ignored():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=100)
    _list_item(db, product, "fixed_price", 1, pf=pf, status="inactive")
    _list_item(db, product, "fixed_price", 2, pf=pf, start_date=date(2030, 1, 1))
    _list_item(db, product, "fixed_price", 3, pf=pf, end_date=date(2020, 1, 1))

    price, debug = calculate_price_for_product(db=db, product=product, price_format=pf, as_of=date.today())

    assert float(price) == 117.65
    assert debug["applied_list_ids"] == []


def test_calculate_prices_stores_explicit_competitor_fields():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=2500)
    _competitor(db, pf, product, "manual:low", 2900)
    _competitor(db, pf, product, "manual:second", 3000)

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
    assert float(cp.lowest_competitor_price) == 2900
    assert float(cp.chosen_competitor_price) == 3000
    assert float(cp.price_from_competitor) == 2994.0
    assert float(cp.bend_percent_used) == 0.2
    assert float(cp.markup_percent_used) == 15
    assert cp.zone == "right"


def test_calculate_prices_stores_applied_list_diagnostics():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=100)
    row = _list_item(db, product, "fixed_price", 333, pf=pf)

    calculate_prices(
        db=db,
        price_format_code=pf.code,
        price_list_number="PL-LIST",
        as_of=date.today(),
        activation_date=None,
        user="test",
        force_new_price_list=True,
    )

    cp = db.query(CalculatedPrice).one()
    assert float(cp.final_price) == 333
    assert cp.applied_list_ids == f"[{row.id}]"
    assert cp.applied_list_id == row.id
    assert cp.applied_rule_type == "fixed_price"
    assert float(cp.applied_rule_value) == 333


def test_calculate_prices_stores_mdc_markup_diagnostics():
    db = _session()
    pf = _format(db)
    db.query(BendRange).filter(BendRange.price_format_id == pf.id).delete()
    db.add(BendRange(price_format_id=pf.id, price_from=0, bend_percent=0))
    db.flush()
    product = _product(db, cost=90.72)
    row = _list_item(db, product, "fixed_markup", 33, pf=pf)
    _competitor(db, pf, product, "manual:high", 200)

    calculate_prices(
        db=db,
        price_format_code=pf.code,
        price_list_number="PL-MDC-DIAG",
        as_of=date.today(),
        activation_date=None,
        user="test",
        force_new_price_list=True,
    )

    cp = db.query(CalculatedPrice).one()
    assert float(cp.final_price) == 200
    assert cp.applied_list_ids == f"[{row.id}]"
    assert cp.applied_list_id == row.id
    assert cp.applied_rule_type == "fixed_markup"
    assert float(cp.applied_rule_value) == 33
    assert float(cp.mdc_markup_percent) == 33
    assert round(float(cp.mdc_price), 2) == 135.4
    assert float(cp.competitor_candidate_price) == 200
    assert "Цена рассчитана" in cp.applied_reason
    assert "Applied Rule" not in cp.applied_reason


def test_generated_item_payload_exposes_applied_list_rule_details():
    db = _session()
    pf = _format(db)
    product = _product(db, cost=100)
    row = _list_item(db, product, "fixed_price", 2500, pf=pf)

    calculate_prices(
        db=db,
        price_format_code=pf.code,
        price_list_number="PL-ITEM-DIAG",
        as_of=date.today(),
        activation_date=None,
        user="test",
        force_new_price_list=True,
    )

    cp = db.query(CalculatedPrice).one()
    payload = _generated_item_dict(db, cp, product, pf, None)
    assert payload["appliedRuleType"] == "fixed_price"
    assert payload["appliedRuleValue"] == 2500.0
    assert payload["appliedListId"] == row.id
    assert payload["appliedListName"] == "fixed_price list"
    assert payload["appliedRule"] == {
        "applied_rule_type": "fixed_price",
        "applied_rule_value": 2500.0,
        "list_id": row.id,
        "list_name": "fixed_price list",
        "list_code": "UL-fixed_price-SKU-1",
        "ambiguous": False,
    }
    assert payload["pricingCalculationLog"] == cp.applied_reason
    assert payload["listOverrideLog"] == {
        "listName": "fixed_price list",
        "listCode": "UL-fixed_price-SKU-1",
        "listType": "fixed_price",
        "value": 2500.0,
        "displayValue": "2500",
        "action": "Применена фиксированная цена из списка.",
        "ambiguous": False,
    }
    assert "fixed_price" not in payload["pricingCalculationLog"]


def test_zone_uses_lowest_competitor_even_when_chosen_competitor_is_different():
    zone, reference, deviation = calculate_price_zone(
        2894.2,
        chosen_competitor_price=2900,
        lowest_competitor_price=2850,
    )

    assert zone == "optimal"
    assert float(reference) == 2850
    assert float(deviation) > 0


@pytest.mark.parametrize(
    ("final_price", "expected_zone"),
    [(99, "left"), (100, "optimal"), (103, "optimal"), (103.01, "right")],
)
def test_zone_boundaries_are_relative_to_lowest_competitor(final_price, expected_zone):
    zone, reference, _deviation = calculate_price_zone(
        final_price,
        chosen_competitor_price=150,
        lowest_competitor_price=100,
    )

    assert zone == expected_zone
    assert float(reference) == 100


def test_zone_falls_back_to_lowest_competitor_when_chosen_missing():
    zone, reference, deviation = calculate_price_zone(
        1020,
        chosen_competitor_price=None,
        lowest_competitor_price=1000,
    )

    assert zone == "optimal"
    assert float(reference) == 1000
    assert round(float(deviation), 2) == 0.02


def test_zone_is_empty_without_any_competitor_reference():
    zone, reference, deviation = calculate_price_zone(
        1020,
        chosen_competitor_price=None,
        lowest_competitor_price=None,
    )

    assert zone is None
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
        zone="",
    )
    db.add(cp)
    db.flush()

    item = _generated_item_dict(db, cp, product, pf, None)

    assert item["markupPercent"] == 15
    assert item["actualMarginPercent"] == 16.67
    assert item["zone"] is None
    assert _export_zone(cp) == ""


def test_mapping_response_exposes_mapping_and_generated_coverage():
    db = _session()
    pf = _format(db)
    product_a = _product(db, code="A", cost=10)
    product_b = _product(db, code="B", cost=20)
    pl = PriceList(number="PL-COV", price_format_id=pf.id, user="test")
    db.add(pl)
    db.flush()
    db.add(CalculatedPrice(price_list_id=pl.id, product_id=product_a.id, cost=10, base_price=11, competitor_price=12, final_price=12, zone="right"))
    db.add(CalculatedPrice(price_list_id=pl.id, product_id=product_b.id, cost=20, base_price=22, competitor_price=None, final_price=21, zone=""))
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
    db.add(CalculatedPrice(price_list_id=second.id, product_id=product_b.id, cost=100, base_price=115, competitor_price=None, final_price=103, zone=""))
    db.flush()

    analytics = build_workflow_analytics(db=db, price_list_id=first.id)

    assert analytics["summary"]["skuTotal"] == 1
    assert analytics["summary"]["withCompetitors"] == 1
    assert analytics["summary"]["withoutCompetitors"] == 0
    assert analytics["summary"]["averageBendPercent"] == 0.5

    no_competitor_analytics = build_workflow_analytics(db=db, price_list_id=second.id)
    assert no_competitor_analytics["summary"]["withoutCompetitors"] == 1
    assert "noDataZone" not in no_competitor_analytics["summary"]
    assert "noIntersection" not in no_competitor_analytics["summary"]
    assert [item["name"] for item in no_competitor_analytics["zones"]] == ["left", "optimal", "right"]
    assert sum(item["value"] for item in no_competitor_analytics["zones"]) == 0


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
