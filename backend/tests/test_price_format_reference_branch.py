from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import (
    BendRange,
    BranchCost,
    BranchStock,
    CalculatedPrice,
    CompetitorPrice,
    CompetitorPriceList,
    ListItem,
    MarkupRange,
    NoCompetitorMarkupRange,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    PriceList,
    PricingContext,
    Product,
    ProductRating,
    ReferenceUpdateStatus,
    RoundingRule,
    UniversalList,
)
from backend.app.services.pricing import calculate_prices
from backend.app.services.pricing_workflow.validation import build_workflow_status


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _format(db, *, rounding=None):
    pf = PriceFormat(code="REF-BRANCH-FMT", name="Reference Branch Format", branch="Almaty")
    db.add(pf)
    db.flush()
    if rounding is not None:
        pf.rounding_rule_id = rounding.id
    db.add(MarkupRange(price_format_id=pf.id, cost_from=0, cost_to=None, markup_percent=0.15))
    db.add(NoCompetitorMarkupRange(price_format_id=pf.id, cost_from=0, cost_to=None, markup_percent=0.03))
    db.add(BendRange(price_format_id=pf.id, price_from=0, bend_percent=0.5))
    db.flush()
    return pf


def _product(db, *, code="REF-BRANCH-SKU", cost=125):
    row = Product(code=code, name=code, cost=cost)
    db.add(row)
    db.flush()
    return row


def _branch_stock(db, product, *, branch_id="1", stock=1):
    db.add(BranchStock(branch_id=branch_id, product_id=product.id, sku=product.code, stock=stock))
    db.flush()


def _branch_cost(db, product, *, branch_id="1", cost=100):
    db.add(BranchCost(branch_id=branch_id, product_id=product.id, sku=product.code, cost=cost))
    db.flush()


def _active_reference(db, *, branch_id="1", data_type="stock"):
    model = BranchStock if data_type == "stock" else BranchCost
    row = ReferenceUpdateStatus(
        branch_id=str(branch_id),
        branch_name=str(branch_id),
        data_type=data_type,
        status="success",
        rows_count=db.query(model).filter(model.branch_id == str(branch_id)).count(),
        last_updated_at=datetime(2026, 1, 1, 8, 0, 0),
    )
    db.add(row)
    db.flush()
    return row


def _list_item(db, product, list_type, value, *, pf):
    row = UniversalList(
        code=f"UL-{list_type}-{product.code}",
        name=f"{list_type} list",
        type=list_type,
        status="active",
        price_format_id=pf.id,
    )
    db.add(row)
    db.flush()
    db.add(ListItem(universal_list_id=row.id, product_id=product.id, value=value))
    db.flush()
    return row


def _assigned_provisor_source(db, pf, product, *, source_key="9999", price=170):
    source_name = f"provisor:{source_key}"
    price_list = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key=source_key,
        display_name=f"Provisor {source_key}",
        supplier=f"Provisor {source_key}",
        branch_name="External",
        competitor_name=f"Provisor {source_key}",
        price_date=date.today(),
    )
    db.add(price_list)
    db.flush()
    db.add(
        PriceFormatCompetitorAssignment(
            price_format_id=pf.id,
            competitor_price_list_id=price_list.id,
            is_active=True,
            coefficient=1,
        )
    )
    db.add(CompetitorPrice(price_format_id=pf.id, product_id=None, source_name=source_name, supplier=source_name, coefficient=1))
    db.add(
        CompetitorPrice(
            price_format_id=pf.id,
            product_id=product.id,
            source_name=source_name,
            supplier=source_name,
            source_price=price,
            coefficient=1,
            match_type="direct",
        )
    )
    db.flush()
    return source_name


def _calculation_fingerprint(row: CalculatedPrice) -> dict:
    return {
        "product_id": row.product_id,
        "cost": Decimal(str(row.cost)),
        "base_price": Decimal(str(row.base_price)),
        "competitor_price": Decimal(str(row.competitor_price)) if row.competitor_price is not None else None,
        "lowest_competitor_price": Decimal(str(row.lowest_competitor_price)) if row.lowest_competitor_price is not None else None,
        "chosen_competitor_price": Decimal(str(row.chosen_competitor_price)) if row.chosen_competitor_price is not None else None,
        "price_from_competitor": Decimal(str(row.price_from_competitor)) if row.price_from_competitor is not None else None,
        "final_price": Decimal(str(row.final_price)),
        "rating_global": row.rating_global,
        "rating_local": row.rating_local,
        "applied_source_name": row.applied_source_name,
        "applied_source_type": row.applied_source_type,
        "applied_rule_name": row.applied_rule_name,
        "applied_rule_type": row.applied_rule_type,
        "applied_rule_value": Decimal(str(row.applied_rule_value)) if row.applied_rule_value is not None else None,
        "applied_list_id": row.applied_list_id,
        "applied_list_ids": row.applied_list_ids,
        "bend_percent_used": Decimal(str(row.bend_percent_used)) if row.bend_percent_used is not None else None,
        "markup_percent_used": Decimal(str(row.markup_percent_used)) if row.markup_percent_used is not None else None,
        "zone": row.zone,
    }


def test_reference_branch_keeps_calculation_identical_when_organizational_branch_changes():
    db = _session()
    rounding = RoundingRule(code="R1", name="R1", mode="nearest", step=1)
    db.add(rounding)
    db.flush()
    pf = _format(db, rounding=rounding)
    pf.branch = "Алматы"
    pf.reference_branch_id = "1"
    pf.pricing_rule = "Rule A"
    product = _product(db, code="REF-BRANCH-SKU", cost=125)
    _branch_stock(db, product, branch_id="1", stock=7)
    _branch_cost(db, product, branch_id="1", cost=125)
    db.add(ProductRating(branch_id="", product_id=product.id, sku=product.code, rating_type="global", rating=11))
    db.add(ProductRating(branch_id="1", product_id=product.id, sku=product.code, rating_type="local", rating=22))
    ul = _list_item(db, product, "min_price", 160, pf=pf)
    source_name = _assigned_provisor_source(db, pf, product, source_key="9999", price=170)
    _active_reference(db, branch_id="1", data_type="stock")
    _active_reference(db, branch_id="1", data_type="cost")
    db.commit()

    first_count = calculate_prices(
        db=db,
        price_format_code=pf.code,
        price_list_number="REF-BRANCH-ALMATY",
        as_of=date.today(),
        activation_date=None,
        user="test",
        force_new_price_list=True,
    )
    first = db.query(CalculatedPrice).join(PriceList).filter(PriceList.number == "REF-BRANCH-ALMATY").one()
    first_fingerprint = _calculation_fingerprint(first)

    pf.branch = "Есик"
    db.commit()
    second_count = calculate_prices(
        db=db,
        price_format_code=pf.code,
        price_list_number="REF-BRANCH-ESIK",
        as_of=date.today(),
        activation_date=None,
        user="test",
        force_new_price_list=True,
    )
    second = db.query(CalculatedPrice).join(PriceList).filter(PriceList.number == "REF-BRANCH-ESIK").one()

    assert first_count == second_count == 1
    assert _calculation_fingerprint(second) == first_fingerprint
    assert first_fingerprint["cost"] == Decimal("125.0000")
    assert first_fingerprint["rating_local"] == 22
    assert first_fingerprint["applied_source_name"] == source_name
    assert first_fingerprint["competitor_price"] == Decimal("170.0000")
    assert first_fingerprint["applied_rule_name"] == "Rule A"
    assert first_fingerprint["applied_rule_type"] == "min_price"
    assert first_fingerprint["applied_list_id"] == ul.id
    assert first_fingerprint["final_price"] == Decimal("169.0000")
    assert first_fingerprint["zone"] == "left"


def test_empty_reference_branch_id_preserves_legacy_branch_calculation():
    db = _session()
    pf = _format(db)
    pf.branch = "Алматы"
    pf.reference_branch_id = ""
    product = _product(db, code="LEGACY-REF-BRANCH", cost=100)
    _branch_stock(db, product, branch_id="1", stock=5)
    _branch_cost(db, product, branch_id="1", cost=100)
    db.add(ProductRating(branch_id="1", product_id=product.id, sku=product.code, rating_type="local", rating=33))
    db.add(ProductRating(branch_id="Есик", product_id=product.id, sku=product.code, rating_type="local", rating=99))
    _active_reference(db, branch_id="1", data_type="stock")
    _active_reference(db, branch_id="1", data_type="cost")
    db.commit()

    count = calculate_prices(
        db=db,
        price_format_code=pf.code,
        price_list_number="REF-BRANCH-LEGACY",
        as_of=date.today(),
        activation_date=None,
        user="test",
        force_new_price_list=True,
    )
    row = db.query(CalculatedPrice).join(PriceList).filter(PriceList.number == "REF-BRANCH-LEGACY").one()

    assert count == 1
    assert row.product_id == product.id
    assert Decimal(str(row.cost)) == Decimal("100.0000")
    assert row.rating_local == 33


def test_workflow_readiness_uses_reference_branch_for_organizational_branch():
    db = _session()
    pf = _format(db)
    pf.branch = "Есик"
    pf.reference_branch_id = "1"
    product = _product(db, code="WF-REF-BRANCH", cost=100)
    _branch_stock(db, product, branch_id="1", stock=4)
    _branch_cost(db, product, branch_id="1", cost=100)
    db.add(ProductRating(branch_id="1", product_id=product.id, sku=product.code, rating_type="local", rating=44))
    _active_reference(db, branch_id="1", data_type="stock")
    _active_reference(db, branch_id="1", data_type="cost")
    context = PricingContext(branch_id="Есик", region="Almaty", sales_channel="retail", name="ctx-esik")
    db.add(context)
    db.flush()
    _assigned_provisor_source(db, pf, product, source_key="9999", price=170)
    db.commit()

    status = build_workflow_status(db=db, pricing_context_id=context.id, price_format_id=pf.id)
    by_kind = {item["kind"]: item for item in status["items"]}

    assert status["context"]["branchId"] == "Есик"
    assert status["priceFormat"]["referenceBranchId"] == "1"
    assert by_kind["stock"]["ok"] is True
    assert by_kind["cost"]["ok"] is True
    assert by_kind["rating_local"]["ok"] is True
