from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from sqlalchemy import select, delete, func, or_
from sqlalchemy.orm import Session

from ..models import (
    BranchCost,
    BranchStock,
    Product,
    PriceFormat,
    MarkupRange,
    NoCompetitorMarkupRange,
    BendRange,
    UniversalList,
    UniversalListPriceFormat,
    ListItem,
    CompetitorPrice,
    CompetitorPriceList,
    CompetitorPricePercentile,
    RegularCompetitorPricePercentile,
    PriceList,
    CalculatedPrice,
    ProductRating,
    PricingRule,
    ReferenceImportJob,
    ReferenceUpdateStatus,
    RoundingRule,
)
from .. import data
from ..timezone import local_iso
from .competitor_matching import rebuild_competitor_prices_for_selected
from .competitor_percentiles import emit_percentile_group_keys
from .competitor_percentiles import REGIONAL_SCOPE, REGULAR_COMPETITOR_SCOPE
from .competitors.identity import canonical_regular_competitor_identity
from .competitors.percentiles.sources import PERCENTILE_SOURCE_COMPETITOR, PERCENTILE_SOURCE_EMIT, is_emit_source_key, percentile_source_id
from .competitor_assignments import get_assigned_competitor_price_lists, propagate_emit_assignments_to_new_price_format
from .references.types import canonical_branch_id
from .regions import allowed_provisor_source_names_for_city_id, city_id_from_branch

LIST_TYPE_FIXED_PRICE = "fixed_price"
LIST_TYPE_MIN_PRICE = "min_price"
LIST_TYPE_MAX_PRICE = "max_price"
LIST_TYPE_FIXED_MARKUP = "fixed_markup"
LIST_TYPE_MIN_MARKUP = "min_markup"
LIST_TYPE_CRITICAL_MARKUP = "critical_markup"
LIST_TYPE_MAX_MARKUP = "max_markup"
LIST_TYPE_NO_BEND = "no_bend"
LIST_TYPE_PERCENTILE_OVERRIDE = "percentile_override"
LIST_TYPE_EXCLUDE_FROM_PRICING = "exclude_from_pricing"
LIST_TYPE_MEMORANDUM = "memorandum"

AMBIGUOUS_LIST_TYPES = {
    LIST_TYPE_MIN_MARKUP,
    LIST_TYPE_CRITICAL_MARKUP,
    LIST_TYPE_MAX_MARKUP,
    LIST_TYPE_PERCENTILE_OVERRIDE,
}

LIST_TYPE_ALIASES = {
    "fixed_price": LIST_TYPE_FIXED_PRICE,
    "min_price": LIST_TYPE_MIN_PRICE,
    "max_price": LIST_TYPE_MAX_PRICE,
    "fixed_markup": LIST_TYPE_FIXED_MARKUP,
    "min_markup": LIST_TYPE_MIN_MARKUP,
    "critical_markup": LIST_TYPE_CRITICAL_MARKUP,
    "max_markup": LIST_TYPE_MAX_MARKUP,
    "no_bend": LIST_TYPE_NO_BEND,
    "percentile_override": LIST_TYPE_PERCENTILE_OVERRIDE,
    "exclude_from_pricing": LIST_TYPE_EXCLUDE_FROM_PRICING,
    "memorandum": LIST_TYPE_MEMORANDUM,
    "exclusion": LIST_TYPE_EXCLUDE_FROM_PRICING,
    "markup": LIST_TYPE_FIXED_MARKUP,
}

ACTIVE_LIST_STATUSES = {
    "active",
    "enabled",
    "активен",
    "активный",
    "Р°РєС‚РёРІ",
    "Р°РєС‚РёРІРµРЅ",
    "Р°РєС‚РёРІРЅС‹Р№",
    "РђРєС‚РёРІРЅС‹Р№",
}


# MVP universal list types (RU labels used in UI/Excel).
# NOTE: We accept a couple of common synonyms for backward-compatibility.
LIST_TYPE_FIXED_PRICE = "Фиксированная цена"
LIST_TYPE_MIN_PRICE = "Минимальная цена"
LIST_TYPE_MAX_PRICE = "Максимальная цена"

LIST_TYPE_MIN_MARGIN = "Минимальная наценка"
LIST_TYPE_CRITICAL_MARGIN = "Критичка"
LIST_TYPE_MAX_MARGIN = "Максимальная наценка"


logger = logging.getLogger(__name__)

MISSING_STOCK_REFERENCE_ERROR = (
    "Актуальный справочник остатков отсутствует. "
    "Загрузите остатки перед формированием прайс-листа."
)


# Canonical code values override the legacy label constants above. Pricing
# logic must not depend on UI labels because DB rows may contain either form.
LIST_TYPE_FIXED_PRICE = "fixed_price"
LIST_TYPE_MIN_PRICE = "min_price"
LIST_TYPE_MAX_PRICE = "max_price"
LIST_TYPE_MIN_MARGIN = "min_markup"
LIST_TYPE_CRITICAL_MARGIN = "critical_markup"
LIST_TYPE_MAX_MARGIN = "max_markup"

LIST_TYPE_ALIASES.update(
    {
        "Р¤РёРєСЃРёСЂРѕРІР°РЅРЅР°СЏ С†РµРЅР°": LIST_TYPE_FIXED_PRICE,
        "Р¤РёРєСЃ С†РµРЅР°": LIST_TYPE_FIXED_PRICE,
        "Р¤РёРєСЃ С†РµРЅС‹": LIST_TYPE_FIXED_PRICE,
        "РњРёРЅРёРјР°Р»СЊРЅР°СЏ С†РµРЅР°": LIST_TYPE_MIN_PRICE,
        "РњР°РєСЃРёРјР°Р»СЊРЅР°СЏ С†РµРЅР°": LIST_TYPE_MAX_PRICE,
        "РњРёРЅРёРјР°Р»СЊРЅР°СЏ РЅР°С†РµРЅРєР°": LIST_TYPE_MIN_MARKUP,
        "РљСЂРёС‚РёС‡РµСЃРєР°СЏ РЅР°С†РµРЅРєР°": LIST_TYPE_CRITICAL_MARKUP,
        "РљСЂРёС‚РёС‡РєР°": LIST_TYPE_CRITICAL_MARKUP,
        "РњР°РєСЃРёРјР°Р»СЊРЅР°СЏ РЅР°С†РµРЅРєР°": LIST_TYPE_MAX_MARKUP,
        "РњР°РєСЃ РЅР°С†РµРЅРєР°": LIST_TYPE_MAX_MARKUP,
        "РњР°РєСЃ. РЅР°С†РµРЅРєР°": LIST_TYPE_MAX_MARKUP,
        "Р‘РµР· РїСЂРѕРіРёР±Р°": LIST_TYPE_NO_BEND,
        "Percentile override": LIST_TYPE_PERCENTILE_OVERRIDE,
        "РСЃРєР»СЋС‡РёС‚СЊ РёР· РїРµСЂРµРѕС†РµРЅРєРё": LIST_TYPE_EXCLUDE_FROM_PRICING,
        "РСЃРєР»СЋС‡РёС‚СЊ РёР· СЂР°СЃС‡РµС‚Р°": LIST_TYPE_EXCLUDE_FROM_PRICING,
        "Фиксированная цена": LIST_TYPE_FIXED_PRICE,
        "Минимальная цена": LIST_TYPE_MIN_PRICE,
        "Максимальная цена": LIST_TYPE_MAX_PRICE,
        "Фиксированная наценка": LIST_TYPE_FIXED_MARKUP,
        "Минимальная наценка": LIST_TYPE_MIN_MARKUP,
        "Критическая наценка": LIST_TYPE_CRITICAL_MARKUP,
        "Максимальная наценка": LIST_TYPE_MAX_MARKUP,
        "Без прогиба": LIST_TYPE_NO_BEND,
        "Переопределение персентиля": LIST_TYPE_PERCENTILE_OVERRIDE,
        "Исключить из расчета": LIST_TYPE_EXCLUDE_FROM_PRICING,
        "Исключить из переоценки": LIST_TYPE_EXCLUDE_FROM_PRICING,
    }
)
LIST_TYPE_ALIASES.update({"Меморандум": LIST_TYPE_MEMORANDUM, "РњРµРјРѕСЂР°РЅРґСѓРј": LIST_TYPE_MEMORANDUM})


@dataclass(frozen=True)
class CompetitorResolved:
    competitor_price: Decimal | None
    applied_source: str


@dataclass(frozen=True)
class CompetitorResolvedMany:
    prices: list[tuple[Decimal, str]]  # (computed_price, source_name)
    details: dict[str, dict[str, Decimal]] | None = None


PercentilePriceCache = dict[int, dict[int, list[tuple[Decimal, str, Decimal, Decimal, int]]]]
COMPETITOR_PRICE_MODES = {"regular", "percentile", "mixed"}


@dataclass(frozen=True)
class SelectedSourceMeta:
    selected_sources: set[str]
    labels: dict[str, str]


@dataclass(frozen=True)
class StockGenerationSnapshot:
    branch_id: str
    product_ids: set[int]
    cost_by_product_id: dict[int, Decimal]
    reconciliation: dict


@dataclass
class PricingPreload:
    price_format_id: int
    product_ids: set[int]
    markup_ranges: list[MarkupRange]
    no_competitor_markup_ranges: list[NoCompetitorMarkupRange]
    bend_ranges: list[BendRange]
    rounding_rule: RoundingRule | None
    selected_source_meta: SelectedSourceMeta
    competitor_configs: list[CompetitorPrice]
    competitor_prices_by_product: dict[int, dict[str, list[CompetitorPrice]]]
    ratings_by_product: dict[int, dict[str, int | None]]
    list_matches: dict[tuple[int, str], tuple[Decimal, int]]
    memorandum_caps: dict[int, dict[str, object]]


def _as_decimal(value: object, default: Decimal | None = None) -> Decimal | None:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _latest_reference_job(db: Session, data_type: str, branch_id: str) -> ReferenceImportJob | None:
    branch_token = f'"{branch_id}"'
    return (
        db.execute(
            select(ReferenceImportJob)
            .where(ReferenceImportJob.data_type == data_type)
            .where(ReferenceImportJob.status == "success")
            .where(ReferenceImportJob.branch_ids_json.like(f"%{branch_token}%"))
            .order_by(ReferenceImportJob.finished_at.desc(), ReferenceImportJob.created_at.desc(), ReferenceImportJob.id.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _successful_reference_status(db: Session, data_type: str, branch_id: str) -> ReferenceUpdateStatus | None:
    return (
        db.execute(
            select(ReferenceUpdateStatus)
            .where(ReferenceUpdateStatus.branch_id == branch_id)
            .where(ReferenceUpdateStatus.data_type == data_type)
            .where(ReferenceUpdateStatus.status == "success")
        )
        .scalars()
        .first()
    )


def _load_stock_generation_snapshot(db: Session, branch_id: str) -> StockGenerationSnapshot:
    stock_status = _successful_reference_status(db, "stock", branch_id)
    stock_rows = (
        db.execute(
            select(BranchStock.product_id, BranchStock.sku)
            .where(BranchStock.branch_id == branch_id)
            .where(BranchStock.product_id.is_not(None))
        )
        .all()
    )
    product_ids = {int(product_id) for product_id, _sku in stock_rows if product_id is not None}
    if stock_status is None or not product_ids:
        raise ValueError(MISSING_STOCK_REFERENCE_ERROR)

    cost_status = _successful_reference_status(db, "cost", branch_id)
    cost_rows = []
    if cost_status is not None:
        cost_rows = (
            db.execute(
                select(BranchCost.product_id, BranchCost.cost)
                .where(BranchCost.branch_id == branch_id)
                .where(BranchCost.product_id.is_not(None))
            )
            .all()
        )
    cost_by_product_id: dict[int, Decimal] = {}
    for product_id, raw_cost in cost_rows:
        cost = _as_decimal(raw_cost)
        if product_id is not None and cost is not None and cost > 0:
            cost_by_product_id[int(product_id)] = cost

    stock_job = _latest_reference_job(db, "stock", branch_id)
    uploaded_at = stock_status.last_updated_at
    expires_at = uploaded_at + timedelta(hours=24) if uploaded_at else None
    stock_file_rows = int(stock_job.rows_total if stock_job is not None else stock_status.rows_count or len(stock_rows) or 0)
    invalid_stock_skus = int(stock_job.rows_failed if stock_job is not None else 0)
    stock_distinct_skus = len({str(sku or "").strip() for _product_id, sku in stock_rows if str(sku or "").strip()})
    duplicate_stock_skus = max(0, stock_file_rows - invalid_stock_skus - stock_distinct_skus)
    products_with_cost = len(product_ids & set(cost_by_product_id))
    reconciliation = {
        "stock_source_uploaded_at": local_iso(uploaded_at) if uploaded_at else "",
        "stock_source_expires_at": local_iso(expires_at) if expires_at else "",
        "stock_file_rows": stock_file_rows,
        "stock_distinct_skus": stock_distinct_skus,
        "stock_mapped_products": len(product_ids),
        "duplicate_stock_skus": duplicate_stock_skus,
        "invalid_stock_skus": invalid_stock_skus,
        "products_with_cost": products_with_cost,
        "products_without_cost": max(0, len(product_ids) - products_with_cost),
        "excluded_by_universal_lists": 0,
        "stale_calculated_rows_removed": 0,
        "calculated_rows": 0,
    }
    return StockGenerationSnapshot(
        branch_id=branch_id,
        product_ids=product_ids,
        cost_by_product_id=cost_by_product_id,
        reconciliation=reconciliation,
    )


def normalize_list_type(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return LIST_TYPE_ALIASES.get(text, LIST_TYPE_ALIASES.get(text.casefold(), text))


def _is_active_list(row: UniversalList) -> bool:
    text = str(row.status or "").strip()
    lowered = text.casefold()
    if lowered in {"inactive", "disabled", "archived", "неактивный", "не активный", "архивный"}:
        return False
    if text in ACTIVE_LIST_STATUSES or lowered in ACTIVE_LIST_STATUSES:
        return True
    return (
        lowered.startswith("active")
        or lowered.startswith("enabled")
        or lowered.startswith("актив")
        or lowered.startswith("Р°РєС‚РёРІ")
    )


def _list_percent_as_fraction(value: Decimal) -> Decimal:
    if Decimal("0") < value < Decimal("1"):
        return value
    return value / Decimal("100")


def price_from_margin(cost: Decimal, margin_fraction: Decimal) -> Decimal:
    denominator = Decimal("1") - margin_fraction
    if denominator <= 0:
        raise ValueError("Margin must be below 100%")
    return cost / denominator


def margin_percent_from_price(cost: object, price: object) -> Decimal | None:
    cost_value = _as_decimal(cost)
    price_value = _as_decimal(price)
    if cost_value is None or price_value is None or price_value == 0:
        return None
    return (price_value - cost_value) / price_value * Decimal("100")


def _list_effect(list_id: int, list_type: str, value: Decimal, effect: str) -> dict:
    return {
        "listId": list_id,
        "type": list_type,
        "value": value,
        "effect": effect,
        "ambiguous": list_type in AMBIGUOUS_LIST_TYPES,
    }


def _list_markup_match_effect(
    list_id: int,
    list_type: str,
    value: Decimal,
    effect: str,
    *,
    markup_fraction: Decimal,
) -> dict:
    out = _list_effect(list_id, list_type, value, effect)
    out["markupFraction"] = markup_fraction
    return out


ZONE_OPTIMAL_THRESHOLD = Decimal("0.03")


def zone_reference_price(
    *,
    chosen_competitor_price: object = None,
    lowest_competitor_price: object = None,
) -> Decimal | None:
    lowest = _as_decimal(lowest_competitor_price)
    if lowest is not None and lowest > 0:
        return lowest
    return None


def lowest_available_competitor_price(
    db: Session,
    price_format_id: int,
    product_id: int,
    *,
    pricing_preload: PricingPreload | None = None,
) -> Decimal | None:
    cached = _cached_lowest_competitor_price(pricing_preload, product_id=product_id)
    if pricing_preload is not None:
        return cached
    if cached is not None:
        return cached
    rows = (
        db.execute(
            select(CompetitorPrice.source_name, CompetitorPrice.source_price)
            .where(CompetitorPrice.price_format_id == price_format_id)
            .where(CompetitorPrice.product_id == product_id)
            .where(CompetitorPrice.source_price.is_not(None))
        )
        .all()
    )
    if not rows:
        return None

    config_rows = (
        db.execute(
            select(CompetitorPrice.source_name, CompetitorPrice.coefficient)
            .where(CompetitorPrice.price_format_id == price_format_id)
            .where(CompetitorPrice.product_id.is_(None))
        )
        .all()
    )
    coefficient_by_source = {
        str(row.source_name or ""): (_as_decimal(row.coefficient, Decimal("1")) or Decimal("1"))
        for row in config_rows
    }

    prices: list[Decimal] = []
    for row in rows:
        source_price = _as_decimal(row.source_price)
        if source_price is None or source_price <= 0:
            continue
        coefficient = coefficient_by_source.get(str(row.source_name or ""), Decimal("1"))
        prices.append(source_price * coefficient)
    return min(prices) if prices else None


def zone_reference_for_product(
    *,
    db: Session,
    price_format: PriceFormat,
    product_id: int,
    percentile_price_cache: PercentilePriceCache | None = None,
    pricing_preload: PricingPreload | None = None,
) -> Decimal | None:
    mode = _competitor_price_mode(price_format)
    if mode == "mixed":
        resolved = resolve_all_competitor_prices(
            db,
            price_format,
            product_id,
            percentile_price_cache=percentile_price_cache,
            pricing_preload=pricing_preload,
        )
        return resolved.prices[0][0] if resolved.prices else None
    if mode == "percentile" and percentile_price_cache is not None:
        zone_percentile_number = int(price_format.percentile_number or 10)
        zone_resolved = resolve_percentile_prices_from_cache(
            percentile_price_cache,
            product_id,
            percentile_number=zone_percentile_number,
        )
        return zone_resolved.prices[0][0] if zone_resolved.prices else None
    return lowest_available_competitor_price(db, price_format.id, product_id, pricing_preload=pricing_preload)


def _competitor_price_mode(price_format: PriceFormat) -> str:
    mode = str(price_format.competitor_price_mode or "regular").strip().lower()
    return mode if mode in COMPETITOR_PRICE_MODES else "regular"


def calculate_price_zone(
    final_price: object,
    *,
    chosen_competitor_price: object = None,
    lowest_competitor_price: object = None,
) -> tuple[str | None, Decimal | None, Decimal | None]:
    price = _as_decimal(final_price)
    reference_price = zone_reference_price(
        chosen_competitor_price=chosen_competitor_price,
        lowest_competitor_price=lowest_competitor_price,
    )
    if price is None or reference_price is None:
        return None, reference_price, None

    deviation_pct = (price - reference_price) / reference_price
    if price < reference_price:
        return "left", reference_price, deviation_pct
    if price <= reference_price * (Decimal("1") + ZONE_OPTIMAL_THRESHOLD):
        return "optimal", reference_price, deviation_pct
    return "right", reference_price, deviation_pct


def _selected_source_meta(db: Session, price_format_id: int) -> SelectedSourceMeta:
    labels: dict[str, str] = {}
    for item in get_assigned_competitor_price_lists(db=db, price_format_id=price_format_id):
        row = item.price_list
        src = f"{row.source_type}:{row.source_key}"
        labels[src] = row.display_name or row.supplier or src
    return SelectedSourceMeta(selected_sources=set(labels), labels=labels)


def _markup_percent_from_ranges(ranges: list[MarkupRange], cost: Decimal) -> Decimal:
    if not ranges:
        raise ValueError("Markup ranges are required")
    for r in ranges:
        cost_from = _as_decimal(r.cost_from, Decimal("0"))
        cost_to = _as_decimal(r.cost_to)
        if cost >= cost_from and (cost_to is None or cost <= cost_to):
            return _as_decimal(r.markup_percent, Decimal("0")) or Decimal("0")
    return _as_decimal(ranges[-1].markup_percent, Decimal("0")) or Decimal("0")


def _no_competitor_markup_from_ranges(
    ranges: list[NoCompetitorMarkupRange],
    cost: Decimal,
    *,
    fallback: Decimal,
) -> Decimal:
    if not ranges:
        return fallback
    for r in ranges:
        cost_from = _as_decimal(r.cost_from, Decimal("0")) or Decimal("0")
        cost_to = _as_decimal(r.cost_to)
        if cost >= cost_from and (cost_to is None or cost <= cost_to):
            return _as_decimal(r.markup_percent, fallback) or fallback
    return _as_decimal(ranges[-1].markup_percent, fallback) or fallback


def _bend_percent_from_ranges(
    rows: list[BendRange],
    competitor_price: Decimal,
    *,
    fallback_percent: Decimal,
) -> Decimal:
    if not rows:
        return fallback_percent
    chosen: Decimal | None = None
    for r in rows:
        p_from = _as_decimal(r.price_from, Decimal("0")) or Decimal("0")
        if competitor_price >= p_from:
            chosen = _as_decimal(r.bend_percent, fallback_percent)
        else:
            break
    return chosen if chosen is not None else fallback_percent


def get_markup_percent_by_range(db: Session, price_format_id: int, cost: Decimal) -> Decimal:
    ranges = db.execute(
        select(MarkupRange)
        .where(MarkupRange.price_format_id == price_format_id)
        .order_by(MarkupRange.cost_from.asc())
    ).scalars().all()

    if not ranges:
        raise ValueError("Markup ranges are required")

    for r in ranges:
        cost_from = _as_decimal(r.cost_from, Decimal("0"))
        cost_to = _as_decimal(r.cost_to)
        if cost >= cost_from and (cost_to is None or cost <= cost_to):
            return _as_decimal(r.markup_percent, Decimal("0")) or Decimal("0")

    # если ничего не подошло — берём последний диапазон
    return _as_decimal(ranges[-1].markup_percent, Decimal("0")) or Decimal("0")


def get_no_competitor_markup_percent_by_range(
    db: Session,
    price_format_id: int,
    cost: Decimal,
    *,
    fallback: Decimal,
) -> Decimal:
    ranges = db.execute(
        select(NoCompetitorMarkupRange)
        .where(NoCompetitorMarkupRange.price_format_id == price_format_id)
        .order_by(NoCompetitorMarkupRange.cost_from.asc())
    ).scalars().all()

    if not ranges:
        return fallback

    for r in ranges:
        cost_from = _as_decimal(r.cost_from, Decimal("0")) or Decimal("0")
        cost_to = _as_decimal(r.cost_to)
        if cost >= cost_from and (cost_to is None or cost <= cost_to):
            return _as_decimal(r.markup_percent, fallback) or fallback

    return _as_decimal(ranges[-1].markup_percent, fallback) or fallback


def get_bend_percent_by_price_range(
    db: Session,
    price_format_id: int,
    competitor_price: Decimal,
    *,
    fallback_percent: Decimal,
) -> Decimal:
    rows = db.execute(
        select(BendRange)
        .where(BendRange.price_format_id == price_format_id)
        .order_by(BendRange.price_from.asc())
    ).scalars().all()

    if not rows:
        return fallback_percent

    chosen: Decimal | None = None
    for r in rows:
        p_from = _as_decimal(r.price_from, Decimal("0")) or Decimal("0")
        if competitor_price >= p_from:
            chosen = _as_decimal(r.bend_percent, fallback_percent)
        else:
            break

    return chosen if chosen is not None else fallback_percent


def _rounding_quantum(rule: RoundingRule | None) -> Decimal:
    if rule is not None and rule.step is not None:
        step = _as_decimal(rule.step, None)
        if step is not None and step > 0:
            return step
    precision = 2
    if rule is not None and rule.precision is not None:
        try:
            precision = max(0, min(int(rule.precision), 6))
        except Exception:
            precision = 2
    return Decimal("1").scaleb(-precision)


def _round_price(price: Decimal, rule: RoundingRule | None, *, force_up: bool = False) -> Decimal:
    quantum = _rounding_quantum(rule)
    if quantum <= 0:
        quantum = Decimal("0.01")
    mode = str(rule.mode if rule is not None else "math").strip().lower()
    rounding = ROUND_HALF_UP
    if force_up or mode == "up":
        rounding = ROUND_CEILING
    elif mode == "down":
        rounding = ROUND_FLOOR
    units = (price / quantum).to_integral_value(rounding=rounding)
    rounded = units * quantum
    output_quantum = Decimal("1").scaleb(-max(0, -quantum.as_tuple().exponent))
    return rounded.quantize(output_quantum)


def resolve_competitor_price(
    db: Session,
    price_format_id: int,
    product_id: int,
    *,
    allowed_provisor_sources: set[str] | None = None,
) -> CompetitorResolved:
    # Схема: competitors_prices хранит
    # - записи-настройки источника: product_id IS NULL, fields: source_name, coefficient
    # - записи цен: product_id == product_id, fields: source_name, source_price

    config_rows = db.execute(
        select(CompetitorPrice)
        .where(CompetitorPrice.price_format_id == price_format_id)
        .where(CompetitorPrice.product_id.is_(None))
    ).scalars().all()

    if not config_rows:
        return CompetitorResolved(None, "нет ПЛК")

    best: Decimal | None = None
    best_source = ""
    selected_meta = _selected_source_meta(db, price_format_id)

    for cfg in config_rows:
        source_name = cfg.source_name
        if (
            allowed_provisor_sources is not None
            and isinstance(source_name, str)
            and source_name.startswith("provisor:")
            and source_name not in selected_meta.selected_sources
            and source_name not in allowed_provisor_sources
        ):
            continue
        coefficient = _as_decimal(cfg.coefficient, Decimal("1")) or Decimal("1")

        source_price_raw = (
            db.execute(
                select(CompetitorPrice.source_price)
                .where(CompetitorPrice.price_format_id == price_format_id)
                .where(CompetitorPrice.product_id == product_id)
                .where(CompetitorPrice.source_name == source_name)
                .where(CompetitorPrice.source_price.is_not(None))
                .order_by(CompetitorPrice.source_price.asc())
                .limit(1)
            )
            .scalars()
            .first()
        )

        source_price = _as_decimal(source_price_raw)
        if source_price is None:
            continue

        computed = source_price * coefficient
        if best is None or computed < best:
            best = computed
            best_source = source_name

    if best is None:
        return CompetitorResolved(None, "нет цен ПЛК")

    return CompetitorResolved(best, best_source)


def resolve_competitor_prices(
    db: Session,
    price_format_id: int,
    product_id: int,
    *,
    allowed_provisor_sources: set[str] | None = None,
    pricing_preload: PricingPreload | None = None,
) -> CompetitorResolvedMany:
    if pricing_preload is not None:
        if not pricing_preload.competitor_configs:
            return CompetitorResolvedMany([])
        out: list[tuple[Decimal, str]] = []
        details: dict[str, dict[str, Decimal]] = {}
        selected_meta = pricing_preload.selected_source_meta
        rows_by_source = pricing_preload.competitor_prices_by_product.get(int(product_id), {})
        for cfg in pricing_preload.competitor_configs:
            source_name = str(cfg.source_name or "")
            if (
                allowed_provisor_sources is not None
                and source_name.startswith("provisor:")
                and source_name not in selected_meta.selected_sources
                and source_name not in allowed_provisor_sources
            ):
                continue
            source_rows = rows_by_source.get(source_name, [])
            if not source_rows:
                continue
            source_price = _as_decimal(source_rows[0].source_price)
            if source_price is None or source_price <= 0:
                continue
            coefficient = _as_decimal(cfg.coefficient, Decimal("1")) or Decimal("1")
            adjusted = source_price * coefficient
            out.append((adjusted, source_name))
            details[source_name] = {
                "original_price": source_price,
                "price_coefficient": coefficient,
                "adjusted_price": adjusted,
            }
        out.sort(key=lambda x: x[0])
        return CompetitorResolvedMany(out, details)

    config_rows = db.execute(
        select(CompetitorPrice)
        .where(CompetitorPrice.price_format_id == price_format_id)
        .where(CompetitorPrice.product_id.is_(None))
    ).scalars().all()

    if not config_rows:
        return CompetitorResolvedMany([])

    out: list[tuple[Decimal, str]] = []
    details: dict[str, dict[str, Decimal]] = {}
    selected_meta = _selected_source_meta(db, price_format_id)
    for cfg in config_rows:
        source_name = cfg.source_name
        if (
            allowed_provisor_sources is not None
            and isinstance(source_name, str)
            and source_name.startswith("provisor:")
            and source_name not in selected_meta.selected_sources
            and source_name not in allowed_provisor_sources
        ):
            continue
        coefficient = _as_decimal(cfg.coefficient, Decimal("1")) or Decimal("1")

        source_price_raw = (
            db.execute(
                select(CompetitorPrice.source_price)
                .where(CompetitorPrice.price_format_id == price_format_id)
                .where(CompetitorPrice.product_id == product_id)
                .where(CompetitorPrice.source_name == source_name)
                .where(CompetitorPrice.source_price.is_not(None))
                .order_by(CompetitorPrice.source_price.asc())
                .limit(1)
            )
            .scalars()
            .first()
        )

        source_price = _as_decimal(source_price_raw)
        if source_price is None:
            continue

        adjusted = source_price * coefficient
        out.append((adjusted, source_name))
        details[str(source_name or "")] = {
            "original_price": source_price,
            "price_coefficient": coefficient,
            "adjusted_price": adjusted,
        }

    out.sort(key=lambda x: x[0])
    return CompetitorResolvedMany(out, details)


def resolve_percentile_prices(
    db: Session,
    price_format_id: int,
    product_id: int,
    *,
    percentile_number: int,
) -> CompetitorResolvedMany:
    cache = load_percentile_price_cache(db, price_format_id)
    return resolve_percentile_prices_from_cache(cache, product_id, percentile_number=percentile_number)


def _assigned_percentile_configs(*, db: Session, price_format_id: int) -> dict[str, CompetitorPrice]:
    rows = (
        db.execute(
            select(CompetitorPrice)
            .where(CompetitorPrice.price_format_id == price_format_id)
            .where(CompetitorPrice.product_id.is_(None))
            .where(CompetitorPrice.source_name.like("percentile:%"))
        )
        .scalars()
        .all()
    )
    return {str(row.source_name or ""): row for row in rows if str(row.source_name or "").strip()}


def _percentile_source_name_for_row(row: CompetitorPricePercentile, price_format_id: int) -> str:
    source_key = str(getattr(row, "source_key", "") or "")
    branch = str(row.branch_name or "")
    competitor = str(row.competitor_name or "")
    if is_emit_source_key(source_key) or str(row.percentile_scope or "") == REGIONAL_SCOPE:
        source_id = percentile_source_id(
            percentile_source=PERCENTILE_SOURCE_EMIT,
            price_format_id=price_format_id,
            scope=row.percentile_scope or REGIONAL_SCOPE,
            source_key=source_key,
            region=branch,
            competitor=competitor,
            percentile=row.percentile,
        )
    else:
        source_id = percentile_source_id(
            percentile_source=PERCENTILE_SOURCE_COMPETITOR,
            price_format_id=price_format_id,
            scope="global",
            source_key=source_key,
            region="",
            competitor=competitor,
            percentile=row.percentile,
        )
    return f"percentile:{source_id}"


def _legacy_emit_percentile_source_name(row: CompetitorPricePercentile) -> str:
    return f"percentile:{row.source_key}:{row.competitor_name}:{row.branch_name}:p{row.percentile}"


def _percentile_cache_entry(
    *,
    row: CompetitorPricePercentile,
    source_name: str,
    coefficient: Decimal,
) -> tuple[Decimal, str, Decimal, Decimal, int] | None:
    original = _as_decimal(row.value)
    if original is None or original <= 0:
        return None
    adjusted = original * coefficient
    return (adjusted, source_name, original, coefficient, int(row.percentile))


def _resolve_percentile_rows(
    *,
    rows: list[CompetitorPricePercentile],
    price_format_id: int,
    active_groups: set[tuple[str, str, str]],
    assigned_configs: dict[str, CompetitorPrice],
    legacy_percentile_number: int | None = None,
) -> CompetitorResolvedMany:
    assigned_source_names = set(assigned_configs)
    out: list[tuple[Decimal, str]] = []
    details: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        source_key = str(getattr(row, "source_key", "") or "")
        branch = str(row.branch_name or "")
        competitor = str(row.competitor_name or "")
        source_name = _percentile_source_name_for_row(row, price_format_id)
        cfg = assigned_configs.get(source_name)
        is_assigned_source = source_name in assigned_source_names
        is_active_emit_row = (branch, competitor, source_key) in active_groups or (
            not source_key
            and any(active_branch == branch and active_competitor == competitor for active_branch, active_competitor, _active_source_key in active_groups)
        )

        if is_emit_source_key(source_key) or is_active_emit_row:
            if row.percentile_scope != REGIONAL_SCOPE:
                continue
            if not is_assigned_source:
                if legacy_percentile_number is None or int(row.percentile) != int(legacy_percentile_number):
                    continue
                if (branch, competitor, source_key) not in active_groups and (
                    source_key or not any(active_branch == branch and active_competitor == competitor for active_branch, active_competitor, _active_source_key in active_groups)
                ):
                    continue
                source_name = _legacy_emit_percentile_source_name(row)
                coefficient = Decimal("1")
            else:
                coefficient = _as_decimal(getattr(cfg, "coefficient", None), Decimal("1")) or Decimal("1")
        else:
            if not is_assigned_source:
                continue
            coefficient = _as_decimal(getattr(cfg, "coefficient", None), Decimal("1")) or Decimal("1")

        entry = _percentile_cache_entry(row=row, source_name=source_name, coefficient=coefficient)
        if entry is None:
            continue
        adjusted, src, original, coeff, pct = entry
        out.append((adjusted, src))
        details[src] = {
            "original_price": original,
            "price_coefficient": coeff,
            "adjusted_price": adjusted,
            "percentile_number": Decimal(pct),
            "is_percentile": Decimal("1"),
        }
    out.sort(key=lambda x: x[0])
    return CompetitorResolvedMany(out, details)


def _resolve_percentile_prices_from_rows(
    db: Session,
    price_format_id: int,
    product_id: int,
    *,
    percentile_number: int,
) -> CompetitorResolvedMany:
    active_groups = emit_percentile_group_keys(db=db, price_format_id=price_format_id)
    assigned_configs = _assigned_percentile_configs(db=db, price_format_id=price_format_id)
    if not active_groups and not assigned_configs:
        return CompetitorResolvedMany([])
    rows = (
        db.execute(
            select(CompetitorPricePercentile)
            .where(CompetitorPricePercentile.price_format_id == price_format_id)
            .where(CompetitorPricePercentile.product_id == product_id)
            .where(CompetitorPricePercentile.percentile == percentile_number)
            .where(CompetitorPricePercentile.value.is_not(None))
        )
        .scalars()
        .all()
    )
    return _resolve_percentile_rows(
        rows=rows,
        price_format_id=price_format_id,
        active_groups=active_groups,
        assigned_configs=assigned_configs,
        legacy_percentile_number=percentile_number,
    )


def _assigned_percentile_source_ids(*, db: Session, price_format_id: int) -> set[str]:
    return {source_name.removeprefix("percentile:").strip() for source_name in _assigned_percentile_configs(db=db, price_format_id=price_format_id)}


def _regular_percentile_source_name(
    *,
    price_format_id: int,
    competitor_identity: str,
    competitor_name: str,
    percentile: int,
) -> str:
    source_id = percentile_source_id(
        percentile_source=PERCENTILE_SOURCE_COMPETITOR,
        price_format_id=price_format_id,
        scope=REGULAR_COMPETITOR_SCOPE,
        source_key=competitor_identity,
        region="",
        competitor=competitor_name,
        percentile=percentile,
    )
    return f"percentile:{source_id}"


def _regular_selected_percentile_keys(
    *,
    assigned_configs: dict[str, CompetitorPrice],
    price_format_id: int,
) -> set[tuple[str, int]]:
    return set(
        _regular_selected_percentile_configs(
            assigned_configs=assigned_configs,
            price_format_id=price_format_id,
        )
    )


def _regular_selected_percentile_configs(
    *,
    assigned_configs: dict[str, CompetitorPrice],
    price_format_id: int,
) -> dict[tuple[str, int], CompetitorPrice]:
    prefix = f"percentile:{PERCENTILE_SOURCE_COMPETITOR}:{price_format_id}:{REGULAR_COMPETITOR_SCOPE}:"
    out: dict[tuple[str, int], CompetitorPrice] = {}
    for source_name, cfg in assigned_configs.items():
        if not source_name.startswith(prefix):
            continue
        rest = source_name.removeprefix(prefix)
        marker = ":p"
        if marker not in rest:
            continue
        before_pct, pct_text = rest.rsplit(marker, 1)
        identity = canonical_regular_competitor_identity(before_pct.split("::", 1)[0].strip())
        try:
            pct = int(pct_text)
        except Exception:
            continue
        if identity:
            out.setdefault((identity, pct), cfg)
    return out


def load_percentile_price_cache(db: Session, price_format_id: int) -> PercentilePriceCache:
    active_groups = emit_percentile_group_keys(db=db, price_format_id=price_format_id)
    assigned_configs = _assigned_percentile_configs(db=db, price_format_id=price_format_id)
    if not active_groups and not assigned_configs:
        return {}
    rows = (
        db.execute(
            select(CompetitorPricePercentile)
            .where(CompetitorPricePercentile.price_format_id == price_format_id)
            .where(CompetitorPricePercentile.value.is_not(None))
            .order_by(
                CompetitorPricePercentile.product_id.asc(),
                CompetitorPricePercentile.percentile.asc(),
                CompetitorPricePercentile.value.asc(),
            )
        )
        .scalars()
        .all()
    )
    cache: PercentilePriceCache = {}
    for row in rows:
        resolved = _resolve_percentile_rows(
            rows=[row],
            price_format_id=price_format_id,
            active_groups=active_groups,
            assigned_configs=assigned_configs,
            legacy_percentile_number=int(row.percentile),
        )
        for price, src in resolved.prices:
            details = (resolved.details or {}).get(src, {})
            original = details.get("original_price", price)
            coefficient = details.get("price_coefficient", Decimal("1"))
            percentile_number = int(details.get("percentile_number", Decimal(row.percentile)))
            cache.setdefault(int(row.product_id), {}).setdefault(percentile_number, []).append(
                (price, src, original, coefficient, percentile_number)
            )
    regular_configs = _regular_selected_percentile_configs(assigned_configs=assigned_configs, price_format_id=price_format_id)
    regular_keys = set(regular_configs)
    if regular_keys:
        identities = sorted({identity for identity, _pct in regular_keys})
        percentiles = sorted({pct for _identity, pct in regular_keys})
        regular_rows = (
            db.execute(
                select(RegularCompetitorPricePercentile)
                .where(RegularCompetitorPricePercentile.competitor_identity.in_(identities))
                .where(RegularCompetitorPricePercentile.percentile.in_(percentiles))
                .where(RegularCompetitorPricePercentile.value.is_not(None))
            )
            .scalars()
            .all()
        )
        for row in regular_rows:
            identity = str(row.competitor_identity or "").strip()
            pct = int(row.percentile)
            if (identity, pct) not in regular_keys:
                continue
            cfg = regular_configs.get((identity, pct))
            if cfg is None:
                continue
            cfg_source_name = str(cfg.source_name or "").strip()
            prefix = f"percentile:{PERCENTILE_SOURCE_COMPETITOR}:{price_format_id}:{REGULAR_COMPETITOR_SCOPE}:"
            selected_source_key = ""
            if cfg_source_name.startswith(prefix):
                selected_source_key = cfg_source_name.removeprefix(prefix).rsplit(":p", 1)[0].split("::", 1)[0].strip()
            source_name = cfg_source_name if selected_source_key == identity else ""
            if not source_name:
                source_name = _regular_percentile_source_name(
                    price_format_id=price_format_id,
                    competitor_identity=identity,
                    competitor_name=str(row.competitor_name or identity),
                    percentile=pct,
                )
            original = _as_decimal(row.value)
            if original is None or original <= 0:
                continue
            coefficient = _as_decimal(getattr(cfg, "coefficient", None), Decimal("1")) or Decimal("1")
            adjusted = original * coefficient
            cache.setdefault(int(row.product_id), {}).setdefault(pct, []).append(
                (adjusted, source_name, original, coefficient, pct)
            )
    return cache


def resolve_percentile_prices_from_cache(
    cache: PercentilePriceCache,
    product_id: int,
    *,
    percentile_number: int | None,
) -> CompetitorResolvedMany:
    buckets = cache.get(int(product_id), {})
    entries: list[tuple] = []
    if percentile_number is None:
        for bucket in buckets.values():
            entries.extend(bucket)
    else:
        entries = list(buckets.get(int(percentile_number), []))
    prices: list[tuple[Decimal, str]] = []
    details: dict[str, dict[str, Decimal]] = {}
    for entry in entries:
        if len(entry) >= 5:
            price, src, original, coefficient, pct = entry[:5]
        else:
            price, src = entry[:2]
            original = price
            coefficient = Decimal("1")
            pct = percentile_number or 0
        prices.append((price, src))
        details[src] = {
            "original_price": original,
            "price_coefficient": coefficient,
            "adjusted_price": price,
            "percentile_number": Decimal(int(pct or 0)),
            "is_percentile": Decimal("1"),
        }
    prices.sort(key=lambda x: x[0])
    return CompetitorResolvedMany(prices, details)


def resolve_all_competitor_prices(
    db: Session,
    price_format: PriceFormat,
    product_id: int,
    *,
    allowed_provisor_sources: set[str] | None = None,
    percentile_price_cache: PercentilePriceCache | None = None,
    percentile_number: int | None = None,
    pricing_preload: PricingPreload | None = None,
) -> CompetitorResolvedMany:
    mode = _competitor_price_mode(price_format)
    details: dict[str, dict[str, Decimal]] = {}
    prices: list[tuple[Decimal, str]] = []

    if mode in {"regular", "mixed"}:
        regular = resolve_competitor_prices(
            db,
            price_format.id,
            product_id,
            allowed_provisor_sources=allowed_provisor_sources,
            pricing_preload=pricing_preload,
        )
        prices.extend(regular.prices)
        details.update(regular.details or {})

    if mode in {"percentile", "mixed"}:
        pct = None if mode == "mixed" else int(percentile_number or price_format.percentile_number or 10)
        if percentile_price_cache is not None:
            percentile = resolve_percentile_prices_from_cache(
                percentile_price_cache,
                product_id,
                percentile_number=pct,
            )
        else:
            percentile = resolve_percentile_prices(
                db,
                price_format.id,
                product_id,
                percentile_number=int(pct or price_format.percentile_number or 10),
            )
        prices.extend(percentile.prices)
        details.update(percentile.details or {})

    prices.sort(key=lambda x: x[0])
    return CompetitorResolvedMany(prices, details)


def _active_lists_query(db: Session, price_format_id: int, as_of: date):
    return (
        select(UniversalList)
        .where(UniversalList.status == "Активный")
        .where((UniversalList.price_format_id.is_(None)) | (UniversalList.price_format_id == price_format_id))
        .where((UniversalList.start_date.is_(None)) | (UniversalList.start_date <= as_of))
        .where((UniversalList.end_date.is_(None)) | (UniversalList.end_date >= as_of))
    )


def _find_item_value(
    db: Session, lists: list[UniversalList], product_id: int, list_type: str
) -> Decimal | None:
    match = _find_item_match(db, lists, product_id, list_type)
    return match[0] if match else None


def _active_lists_for_format(db: Session, price_format_id: int, as_of: date) -> list[UniversalList]:
    rows = (
        db.execute(
            select(UniversalList)
            .where((UniversalList.start_date.is_(None)) | (UniversalList.start_date <= as_of))
            .where((UniversalList.end_date.is_(None)) | (UniversalList.end_date >= as_of))
            .order_by(UniversalList.id.asc())
        )
        .scalars()
        .all()
    )
    rows = [row for row in rows if _is_active_list(row)]
    if not rows:
        return []

    links: dict[int, set[int]] = {}
    for list_id, pf_id in db.execute(
        select(UniversalListPriceFormat.universal_list_id, UniversalListPriceFormat.price_format_id)
        .where(UniversalListPriceFormat.universal_list_id.in_([row.id for row in rows]))
    ).all():
        links.setdefault(int(list_id), set()).add(int(pf_id))

    active: list[UniversalList] = []
    for row in rows:
        linked_format_ids = links.get(int(row.id), set())
        direct_pf_id = int(row.price_format_id) if row.price_format_id is not None else None
        if price_format_id in linked_format_ids or direct_pf_id == price_format_id:
            active.append(row)
        elif direct_pf_id is None and not linked_format_ids:
            active.append(row)
    return active


def _find_item_match(
    db: Session,
    lists: list[UniversalList],
    product_id: int,
    list_type: str,
) -> tuple[Decimal, int] | None:
    list_ids = [l.id for l in lists if normalize_list_type(l.type) == list_type]
    if not list_ids:
        return None

    stmt = (
        select(ListItem)
        .where(ListItem.universal_list_id.in_(list_ids))
        .where(ListItem.product_id == product_id)
        .order_by(ListItem.universal_list_id.asc(), ListItem.id.asc())
        .limit(2)
    )
    if list_type == LIST_TYPE_CRITICAL_MARKUP:
        stmt = stmt.where(
            or_(ListItem.special_value.is_(None), ListItem.special_value != "-")
        )
    rows = db.execute(stmt).scalars().all()

    if not rows:
        return None

    if len(rows) > 1:
        details = ", ".join(f"list_id={row.universal_list_id} value={row.value}" for row in rows)
        raise ValueError(
            "Lists Management conflict: "
            f"product_id={product_id} rule_type={list_type} has multiple active matching rules: {details}"
        )

    row = rows[0]
    value = _as_decimal(row.value)
    if value is None and list_type == LIST_TYPE_EXCLUDE_FROM_PRICING:
        value = Decimal("1")
    return (value, int(row.universal_list_id)) if value is not None else None


def _validate_list_rule_conflicts(
    db: Session,
    *,
    lists: list[UniversalList],
    products: list[Product],
    price_format: PriceFormat,
) -> None:
    if not lists or not products:
        return

    list_by_id = {int(row.id): row for row in lists}
    product_by_id = {int(row.id): row for row in products}
    rows = db.execute(
        select(ListItem)
        .where(ListItem.universal_list_id.in_(list(list_by_id)))
        .where(ListItem.product_id.in_(list(product_by_id)))
        .order_by(ListItem.product_id.asc(), ListItem.universal_list_id.asc(), ListItem.id.asc())
    ).scalars().all()

    grouped: dict[tuple[int, str], list[ListItem]] = {}
    for item in rows:
        list_row = list_by_id.get(int(item.universal_list_id))
        list_type = normalize_list_type(list_row.type if list_row is not None else "")
        if not list_type:
            continue
        if list_type == LIST_TYPE_MEMORANDUM:
            continue
        grouped.setdefault((int(item.product_id), list_type), []).append(item)

    conflicts: list[str] = []
    for (product_id, list_type), matches in grouped.items():
        if len(matches) <= 1:
            continue
        product = product_by_id[product_id]
        details = []
        for item in matches:
            list_row = list_by_id[int(item.universal_list_id)]
            details.append(
                f'#{list_row.id} "{list_row.name or list_row.code or list_row.id}" value={item.value}'
            )
        conflicts.append(
            f"SKU={product.code} format={price_format.code} type={list_type}: " + ", ".join(details)
        )

    if conflicts:
        raise ValueError("Lists Management conflicts detected: " + " | ".join(conflicts))


def _find_item_value_any(
    db: Session,
    lists: list[UniversalList],
    product_id: int,
    list_types: list[str],
) -> Decimal | None:
    for t in list_types:
        v = _find_item_value(db, lists, product_id, t)
        if v is not None:
            return v
    return None


def _find_item_match_any(
    db: Session,
    lists: list[UniversalList],
    product_id: int,
    list_types: list[str],
) -> tuple[Decimal, int, str] | None:
    for t in list_types:
        match = _find_item_match(db, lists, product_id, t)
        if match is not None:
            return match[0], match[1], t
    return None


def _source_type(source_name: str) -> str:
    if not source_name:
        return ""
    return source_name.split(":", 1)[0] if ":" in source_name else "competitor"


def _build_list_match_cache(
    db: Session,
    *,
    active_lists: list[UniversalList],
    products: list[Product],
) -> dict[tuple[int, str], tuple[Decimal, int]]:
    if not active_lists or not products:
        return {}
    list_by_id = {int(row.id): row for row in active_lists}
    rows = (
        db.execute(
            select(ListItem)
            .where(ListItem.universal_list_id.in_(list(list_by_id)))
            .where(ListItem.product_id.in_([int(product.id) for product in products]))
            .order_by(ListItem.product_id.asc(), ListItem.universal_list_id.asc(), ListItem.id.asc())
        )
        .scalars()
        .all()
    )
    grouped: dict[tuple[int, str], list[ListItem]] = {}
    for item in rows:
        list_row = list_by_id.get(int(item.universal_list_id))
        list_type = normalize_list_type(list_row.type if list_row is not None else "")
        if not list_type:
            continue
        if list_type == LIST_TYPE_MEMORANDUM:
            continue
        if list_type == LIST_TYPE_CRITICAL_MARKUP and str(item.special_value or "") == "-":
            continue
        grouped.setdefault((int(item.product_id), list_type), []).append(item)

    out: dict[tuple[int, str], tuple[Decimal, int]] = {}
    for key, matches in grouped.items():
        if len(matches) > 1:
            continue
        row = matches[0]
        list_type = key[1]
        value = _as_decimal(row.value)
        if value is None and list_type == LIST_TYPE_EXCLUDE_FROM_PRICING:
            value = Decimal("1")
        if value is not None:
            out[key] = (value, int(row.universal_list_id))
    return out


def _build_memorandum_cap_cache(
    db: Session,
    *,
    active_lists: list[UniversalList],
    products: list[Product],
) -> dict[int, dict[str, object]]:
    list_by_id = {int(row.id): row for row in active_lists if normalize_list_type(row.type) == LIST_TYPE_MEMORANDUM}
    if not list_by_id or not products:
        return {}
    product_ids = [int(product.id) for product in products]
    rows = (
        db.execute(
            select(ListItem)
            .where(ListItem.universal_list_id.in_(list(list_by_id)))
            .where(ListItem.product_id.in_(product_ids))
            .order_by(ListItem.product_id.asc(), ListItem.value.asc(), ListItem.universal_list_id.asc(), ListItem.id.asc())
        )
        .scalars()
        .all()
    )
    grouped: dict[int, list[ListItem]] = {}
    for item in rows:
        value = _as_decimal(item.value)
        if value is None or value <= 0:
            continue
        grouped.setdefault(int(item.product_id), []).append(item)

    out: dict[int, dict[str, object]] = {}
    for product_id, matches in grouped.items():
        best = min(matches, key=lambda item: (_as_decimal(item.value, Decimal("999999999")) or Decimal("999999999"), int(item.universal_list_id), int(item.id)))
        list_row = list_by_id[int(best.universal_list_id)]
        out[product_id] = {
            "value": _as_decimal(best.value) or Decimal("0"),
            "listId": int(best.universal_list_id),
            "listName": str(list_row.name or list_row.code or list_row.id),
            "listCode": str(list_row.code or ""),
            "duplicates": len(matches),
            "duplicateListIds": [int(item.universal_list_id) for item in matches],
        }
    return out


def _memorandum_cap_for_product(
    db: Session,
    *,
    active_lists: list[UniversalList],
    product_id: int,
) -> dict[str, object] | None:
    list_by_id = {int(row.id): row for row in active_lists if normalize_list_type(row.type) == LIST_TYPE_MEMORANDUM}
    if not list_by_id:
        return None
    rows = (
        db.execute(
            select(ListItem)
            .where(ListItem.universal_list_id.in_(list(list_by_id)))
            .where(ListItem.product_id == int(product_id))
            .order_by(ListItem.value.asc(), ListItem.universal_list_id.asc(), ListItem.id.asc())
        )
        .scalars()
        .all()
    )
    matches = [item for item in rows if (_as_decimal(item.value) is not None and (_as_decimal(item.value) or Decimal("0")) > 0)]
    if not matches:
        return None
    best = min(matches, key=lambda item: (_as_decimal(item.value, Decimal("999999999")) or Decimal("999999999"), int(item.universal_list_id), int(item.id)))
    list_row = list_by_id[int(best.universal_list_id)]
    return {
        "value": _as_decimal(best.value) or Decimal("0"),
        "listId": int(best.universal_list_id),
        "listName": str(list_row.name or list_row.code or list_row.id),
        "listCode": str(list_row.code or ""),
        "duplicates": len(matches),
        "duplicateListIds": [int(item.universal_list_id) for item in matches],
    }


def _ratings_cache(
    db: Session,
    *,
    product_ids: list[int],
    branch_id: str,
) -> dict[int, dict[str, int | None]]:
    if not product_ids:
        return {}
    rows = (
        db.execute(
            select(ProductRating)
            .where(ProductRating.product_id.in_(product_ids))
            .where(
                (ProductRating.rating_type == "global")
                | ((ProductRating.rating_type == "local") & (ProductRating.branch_id == branch_id))
            )
            .order_by(ProductRating.updated_at.desc(), ProductRating.id.desc())
        )
        .scalars()
        .all()
    )
    out: dict[int, dict[str, int | None]] = {}
    for row in rows:
        bucket = out.setdefault(int(row.product_id), {"global": None, "local": None})
        key = "local" if row.rating_type == "local" else "global"
        if bucket[key] is None:
            bucket[key] = int(row.rating) if row.rating is not None else None
    return out


def _build_pricing_preload(
    db: Session,
    *,
    price_format: PriceFormat,
    products: list[Product],
    active_lists: list[UniversalList],
    branch_id: str,
) -> PricingPreload:
    product_ids = [int(product.id) for product in products]
    configs = (
        db.execute(
            select(CompetitorPrice)
            .where(CompetitorPrice.price_format_id == price_format.id)
            .where(CompetitorPrice.product_id.is_(None))
        )
        .scalars()
        .all()
    )
    price_rows = (
        db.execute(
            select(CompetitorPrice)
            .where(CompetitorPrice.price_format_id == price_format.id)
            .where(CompetitorPrice.product_id.in_(product_ids))
            .where(CompetitorPrice.source_price.is_not(None))
            .order_by(CompetitorPrice.product_id.asc(), CompetitorPrice.source_name.asc(), CompetitorPrice.source_price.asc(), CompetitorPrice.id.asc())
        )
        .scalars()
        .all()
        if product_ids
        else []
    )
    competitor_prices_by_product: dict[int, dict[str, list[CompetitorPrice]]] = {}
    for row in price_rows:
        if row.product_id is None:
            continue
        competitor_prices_by_product.setdefault(int(row.product_id), {}).setdefault(str(row.source_name or ""), []).append(row)
    rounding_rule = db.get(RoundingRule, price_format.rounding_rule_id) if price_format.rounding_rule_id else None
    return PricingPreload(
        price_format_id=int(price_format.id),
        product_ids=set(product_ids),
        markup_ranges=db.execute(
            select(MarkupRange).where(MarkupRange.price_format_id == price_format.id).order_by(MarkupRange.cost_from.asc())
        ).scalars().all(),
        no_competitor_markup_ranges=db.execute(
            select(NoCompetitorMarkupRange).where(NoCompetitorMarkupRange.price_format_id == price_format.id).order_by(NoCompetitorMarkupRange.cost_from.asc())
        ).scalars().all(),
        bend_ranges=db.execute(
            select(BendRange).where(BendRange.price_format_id == price_format.id).order_by(BendRange.price_from.asc())
        ).scalars().all(),
        rounding_rule=rounding_rule,
        selected_source_meta=_selected_source_meta(db, int(price_format.id)),
        competitor_configs=configs,
        competitor_prices_by_product=competitor_prices_by_product,
        ratings_by_product=_ratings_cache(db, product_ids=product_ids, branch_id=branch_id),
        list_matches=_build_list_match_cache(db, active_lists=active_lists, products=products),
        memorandum_caps=_build_memorandum_cap_cache(db, active_lists=active_lists, products=products),
    )


def _cached_list_match(
    preload: PricingPreload | None,
    product_id: int,
    list_type: str,
) -> tuple[Decimal, int] | None:
    if preload is None:
        return None
    return preload.list_matches.get((int(product_id), list_type))


def _memorandum_match(
    db: Session,
    *,
    product_id: int,
    active_lists: list[UniversalList],
    pricing_preload: PricingPreload | None,
) -> dict[str, object] | None:
    if pricing_preload is not None:
        return pricing_preload.memorandum_caps.get(int(product_id))
    return _memorandum_cap_for_product(db, active_lists=active_lists, product_id=int(product_id))


def _apply_memorandum_cap(
    *,
    price: Decimal,
    debug: dict,
    memorandum: dict[str, object] | None,
) -> tuple[Decimal, dict]:
    if not memorandum:
        debug.update(
            {
                "memorandum_applied": False,
                "memorandum_max_price": None,
                "price_before_memorandum": None,
                "price_after_memorandum": price,
                "memorandum_below_mdc": False,
                "memorandum_list_id": None,
                "memorandum_list_name": "",
                "memorandum_diagnostic_code": "",
                "memorandum_duplicate_conflict": False,
            }
        )
        return price, debug

    cap = _as_decimal(memorandum.get("value"))
    if cap is None or cap <= 0:
        return price, debug
    before = price
    after = min(price, cap)
    mdc = _as_decimal(debug.get("mdc_price") or debug.get("base_price"))
    below_mdc = bool(mdc is not None and cap < mdc)
    changed = after != before
    duplicate_conflict = int(memorandum.get("duplicates") or 0) > 1
    list_id = int(memorandum.get("listId") or 0)
    list_name = str(memorandum.get("listName") or "")
    diagnostic_code = "memorandum_below_mdc" if below_mdc else "memorandum_cap" if changed else ""
    if duplicate_conflict and not diagnostic_code:
        diagnostic_code = "memorandum_duplicate_conflict"

    effects = list(debug.get("applied_list_effects") or [])
    effect = _list_effect(list_id, LIST_TYPE_MEMORANDUM, cap, "memorandum_cap")
    effect.update(
        {
            "listName": list_name,
            "changedFinalPrice": changed,
            "memorandumBelowMdc": below_mdc,
            "duplicateConflict": duplicate_conflict,
            "duplicateListIds": memorandum.get("duplicateListIds") or [],
            "effectMessage": "Цена ограничена меморандумом." if changed else "Меморандум проверен, цена ниже ограничения.",
        }
    )
    effects.append(effect)
    list_ids = sorted({int(item["listId"]) for item in effects if item.get("listId")})
    log = str(debug.get("log") or "")
    if changed:
        log = (
            f"{log} Расчётная цена {before:g} превышает максимальную цену по меморандуму {cap:g}. "
            f"Итоговая цена ограничена до {after:g}."
        ).strip()
    if below_mdc:
        log = (
            f"{log} Максимальная цена по меморандуму {cap:g} ниже МДЦ {mdc:g}. "
            "Применена регулируемая максимальная цена."
        ).strip()
    if duplicate_conflict:
        log = f"{log} Найдено несколько активных строк меморандума; применена минимальная цена.".strip()

    debug.update(
        {
            "final_price": after,
            "reason": "memorandum_cap" if changed else debug.get("reason", ""),
            "log": log,
            "applied_list_effects": effects,
            "applied_list_ids": list_ids,
            "memorandum_applied": changed,
            "memorandum_max_price": cap,
            "price_before_memorandum": before if changed else None,
            "price_after_memorandum": after,
            "memorandum_below_mdc": below_mdc,
            "memorandum_list_id": list_id,
            "memorandum_list_name": list_name,
            "memorandum_diagnostic_code": diagnostic_code,
            "memorandum_duplicate_conflict": duplicate_conflict,
        }
    )
    return after, debug


def _cached_rating(preload: PricingPreload | None, product_id: int, rating_type: str) -> int | None:
    if preload is None:
        return None
    return preload.ratings_by_product.get(int(product_id), {}).get("local" if rating_type == "local" else "global")


def _cached_source_match_type(preload: PricingPreload | None, product_id: int, source_name: str) -> str | None:
    if preload is None or not source_name or source_name.startswith("percentile:"):
        return "" if source_name.startswith("percentile:") else None
    rows = preload.competitor_prices_by_product.get(int(product_id), {}).get(source_name, [])
    return str(rows[0].match_type or "") if rows else ""


def _cached_lowest_competitor_price(
    preload: PricingPreload | None,
    *,
    product_id: int,
) -> Decimal | None:
    if preload is None:
        return None
    coefficient_by_source = {
        str(cfg.source_name or ""): (_as_decimal(cfg.coefficient, Decimal("1")) or Decimal("1"))
        for cfg in preload.competitor_configs
    }
    prices: list[Decimal] = []
    for source_rows in preload.competitor_prices_by_product.get(int(product_id), {}).values():
        for row in source_rows:
            source_price = _as_decimal(row.source_price)
            if source_price is None or source_price <= 0:
                continue
            prices.append(source_price * coefficient_by_source.get(str(row.source_name or ""), Decimal("1")))
    return min(prices) if prices else None


def _diagnostic_price_before_margin_list(
    *,
    db: Session,
    price_format: PriceFormat,
    cost: Decimal,
    markup_percent: Decimal,
    no_competitor_markup_percent: Decimal,
    competitor_prices: list[tuple[Decimal, str]],
    rounding_rule: RoundingRule | None,
    fallback_bend_percent: Decimal,
    bend_ranges: list[BendRange] | None = None,
) -> Decimal:
    mdc = price_from_margin(cost, markup_percent)
    if not competitor_prices:
        price = price_from_margin(cost, no_competitor_markup_percent)
        if price < mdc:
            price = mdc
    else:
        price = mdc
        for competitor_price, _competitor_source in competitor_prices:
            bend_percent = (
                _bend_percent_from_ranges(bend_ranges, competitor_price, fallback_percent=fallback_bend_percent)
                if bend_ranges is not None
                else get_bend_percent_by_price_range(
                    db,
                    price_format.id,
                    competitor_price,
                    fallback_percent=fallback_bend_percent,
                )
            )
            candidate = competitor_price * (Decimal("1") - bend_percent / Decimal("100"))
            if candidate >= mdc:
                price = candidate
                break
    price = _round_price(price, rounding_rule)
    if price < mdc:
        price = _round_price(mdc, rounding_rule, force_up=True)
    return price


def _source_match_type(db: Session, price_format_id: int, product_id: int, source_name: str) -> str:
    if not source_name or source_name.startswith("percentile:"):
        return ""
    row = (
        db.execute(
            select(CompetitorPrice.match_type)
            .where(CompetitorPrice.price_format_id == price_format_id)
            .where(CompetitorPrice.product_id == product_id)
            .where(CompetitorPrice.source_name == source_name)
            .where(CompetitorPrice.source_price.is_not(None))
            .order_by(CompetitorPrice.source_price.asc(), CompetitorPrice.id.asc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    return str(row or "")


def _rating_value(db: Session, product_id: int, rating_type: str, branch_id: str = "") -> int | None:
    stmt = (
        select(ProductRating.rating)
        .where(ProductRating.product_id == product_id)
        .where(ProductRating.rating_type == rating_type)
    )
    if rating_type == "local":
        stmt = stmt.where(ProductRating.branch_id == branch_id)
    row = db.execute(stmt.order_by(ProductRating.updated_at.desc(), ProductRating.id.desc()).limit(1)).scalars().first()
    return int(row) if row is not None else None


def calculate_price_for_product(
    *,
    db: Session,
    product: Product,
    price_format: PriceFormat,
    as_of: date,
    region_id: int | None = None,
    active_lists: list[UniversalList] | None = None,
    percentile_price_cache: PercentilePriceCache | None = None,
    cost_override: object = None,
    pricing_preload: PricingPreload | None = None,
) -> tuple[Decimal, dict]:
    def find_match(list_type: str) -> tuple[Decimal, int] | None:
        if pricing_preload is not None:
            return _cached_list_match(pricing_preload, int(product.id), list_type)
        return _find_item_match(db, active_lists or [], product.id, list_type)

    def rating_value(rating_type: str, branch: str = "") -> int | None:
        if pricing_preload is not None:
            return _cached_rating(pricing_preload, int(product.id), rating_type)
        return _rating_value(db, product.id, rating_type, branch)

    def bend_value(competitor_price: Decimal, *, fallback_percent: Decimal) -> Decimal:
        if pricing_preload is not None:
            return _bend_percent_from_ranges(
                pricing_preload.bend_ranges,
                competitor_price,
                fallback_percent=fallback_percent,
            )
        return get_bend_percent_by_price_range(
            db,
            price_format.id,
            competitor_price,
            fallback_percent=fallback_percent,
        )

    cost = _as_decimal(cost_override if cost_override is not None else product.cost)
    if cost is None or cost <= 0:
        zero = Decimal("0")
        branch_id = str(region_id if region_id is not None else (price_format.branch or ""))
        debug = {
            "cost": zero,
            "markup_percent": None,
            "base_price": zero,
            "competitor_price": None,
            "lowest_competitor_price": None,
            "competitor_source": "",
            "applied_source_name": "",
            "applied_source_type": "",
            "used_percentile": False,
            "used_substitute": False,
            "rating_global": rating_value("global"),
            "rating_local": rating_value("local", branch_id),
            "applied_list_effects": [],
            "applied_list_ids": [],
            "applied_rule_type": "",
            "applied_rule_value": None,
            "applied_list_id": None,
            "applied_list_name": "",
            "applied_rule_ambiguous": False,
            "list_matched": False,
            "list_applied": False,
            "list_changed_final_price": False,
            "list_effect_message": "",
            "excluded_from_pricing": False,
            "bend_percent": zero,
            "bend_percent_used": zero,
            "effective_markup_percent": None,
            "markup_percent_used": None,
            "mdc_markup_percent": None,
            "mdc_price": zero,
            "competitor_candidate_price": None,
            "chosen_competitor_price": None,
            "selected_competitor_price": None,
            "chosen_competitor_source": "",
            "chosen_competitor_rank": None,
            "rejected_competitors": [],
            "price_from_competitor": None,
            "final_price": zero,
            "reason": "missing_cost",
            "log": "Нет себестоимости, расчет не выполнен",
            "zone": None,
            "zone_reference_price": None,
            "deviation_pct": None,
        }
        return zero, debug

    if pricing_preload is not None:
        markup_percent = _markup_percent_from_ranges(pricing_preload.markup_ranges, cost)
        no_competitor_markup_percent = _no_competitor_markup_from_ranges(
            pricing_preload.no_competitor_markup_ranges,
            cost,
            fallback=markup_percent,
        )
    else:
        markup_percent = get_markup_percent_by_range(db, price_format.id, cost)
        no_competitor_markup_percent = get_no_competitor_markup_percent_by_range(
            db,
            price_format.id,
            cost,
            fallback=markup_percent,
        )
    global_markup_percent = markup_percent
    global_no_competitor_markup_percent = no_competitor_markup_percent
    rounding_rule = (
        pricing_preload.rounding_rule
        if pricing_preload is not None
        else db.get(RoundingRule, price_format.rounding_rule_id) if price_format.rounding_rule_id else None
    )
    active_lists = active_lists if active_lists is not None else _active_lists_for_format(db, price_format.id, as_of)
    applied_list_effects: list[dict] = []
    memorandum = _memorandum_match(
        db,
        product_id=int(product.id),
        active_lists=active_lists,
        pricing_preload=pricing_preload,
    )

    fixed_price_match = find_match(LIST_TYPE_FIXED_PRICE)
    if fixed_price_match is not None:
        fixed_price, list_id = fixed_price_match
        list_row = next((row for row in active_lists if int(row.id) == list_id), None)
        list_name = str(list_row.name or list_row.code or list_id) if list_row is not None else str(list_id)
        effect = _list_effect(list_id, LIST_TYPE_FIXED_PRICE, fixed_price, "fixed_price_list")
        effect["listName"] = list_name
        markup_percent_used = markup_percent * Decimal("100")
        diagnostic_mdc = price_from_margin(cost, markup_percent)
        branch_id = str(region_id if region_id is not None else (price_format.branch or ""))
        zone_competitor_price_min = zone_reference_for_product(
            db=db,
            price_format=price_format,
            product_id=product.id,
            percentile_price_cache=percentile_price_cache,
            pricing_preload=pricing_preload,
        )
        zone, zone_reference, deviation_pct = calculate_price_zone(
            fixed_price,
            lowest_competitor_price=zone_competitor_price_min,
        )
        debug = {
            "cost": cost,
            "markup_percent": markup_percent,
            "base_price": diagnostic_mdc,
            "competitor_price": None,
            "lowest_competitor_price": zone_competitor_price_min,
            "competitor_source": "",
            "applied_source_name": "",
            "applied_source_type": "",
            "used_percentile": False,
            "used_substitute": False,
            "rating_global": rating_value("global"),
            "rating_local": rating_value("local", branch_id),
            "applied_list_effects": [effect],
            "applied_list_ids": [list_id],
            "applied_rule_type": LIST_TYPE_FIXED_PRICE,
            "applied_rule_value": fixed_price,
            "applied_list_id": list_id,
            "applied_list_name": list_name,
            "applied_rule_ambiguous": False,
            "list_matched": True,
            "list_applied": True,
            "list_changed_final_price": True,
            "list_effect_message": "fixed_price set final price directly.",
            "excluded_from_pricing": False,
            "bend_percent": Decimal("0"),
            "bend_percent_used": Decimal("0"),
            "effective_markup_percent": markup_percent_used,
            "markup_percent_used": markup_percent_used,
            "mdc_markup_percent": markup_percent_used,
            "mdc_price": diagnostic_mdc,
            "competitor_candidate_price": None,
            "chosen_competitor_price": None,
            "selected_competitor_price": None,
            "chosen_competitor_source": "",
            "chosen_competitor_rank": None,
            "rejected_competitors": [],
            "price_from_competitor": None,
            "final_price": fixed_price,
            "reason": "fixed_price_list",
            "log": "Финальная цена установлена напрямую из фиксированной цены; выбор конкурента и прогиб не применялись.",
            "zone": zone,
            "zone_reference_price": zone_reference,
            "deviation_pct": deviation_pct,
        }
        fixed_price, debug = _apply_memorandum_cap(price=fixed_price, debug=debug, memorandum=memorandum)
        zone, zone_reference, deviation_pct = calculate_price_zone(
            fixed_price,
            lowest_competitor_price=zone_competitor_price_min,
        )
        debug.update({"zone": zone, "zone_reference_price": zone_reference, "deviation_pct": deviation_pct})
        return fixed_price, debug

    fixed_markup_match = find_match(LIST_TYPE_FIXED_MARKUP)
    if fixed_markup_match is not None:
        fixed_markup, list_id = fixed_markup_match
        markup_percent = _list_percent_as_fraction(fixed_markup)
        fixed_markup_mdc = _round_price(price_from_margin(cost, markup_percent), rounding_rule)
        list_row = next((row for row in active_lists if int(row.id) == list_id), None)
        list_name = str(list_row.name or list_row.code or list_id) if list_row is not None else str(list_id)
        effect = _list_markup_match_effect(
            list_id,
            LIST_TYPE_FIXED_MARKUP,
            fixed_markup,
            "fixed_markup_final_price",
            markup_fraction=markup_percent,
        )
        effect["listName"] = list_name
        effect["changedFinalPrice"] = True
        effect["effectMessage"] = "fixed_markup calculated MDC from list margin and used it as final price; competitors and bend were bypassed."
        markup_percent_used = markup_percent * Decimal("100")
        branch_id = str(region_id if region_id is not None else (price_format.branch or ""))
        zone_competitor_price_min = zone_reference_for_product(
            db=db,
            price_format=price_format,
            product_id=product.id,
            percentile_price_cache=percentile_price_cache,
            pricing_preload=pricing_preload,
        )
        zone, zone_reference, deviation_pct = calculate_price_zone(
            fixed_markup_mdc,
            lowest_competitor_price=zone_competitor_price_min,
        )
        debug = {
            "cost": cost,
            "markup_percent": markup_percent,
            "base_price": fixed_markup_mdc,
            "competitor_price": None,
            "lowest_competitor_price": zone_competitor_price_min,
            "competitor_source": "",
            "applied_source_name": "",
            "applied_source_type": "",
            "used_percentile": False,
            "used_substitute": False,
            "rating_global": rating_value("global"),
            "rating_local": rating_value("local", branch_id),
            "applied_list_effects": [effect],
            "applied_list_ids": [list_id],
            "applied_rule_type": LIST_TYPE_FIXED_MARKUP,
            "applied_rule_value": fixed_markup,
            "applied_list_id": list_id,
            "applied_list_name": list_name,
            "applied_rule_ambiguous": False,
            "list_matched": True,
            "list_applied": True,
            "list_changed_final_price": True,
            "list_effect_message": effect["effectMessage"],
            "excluded_from_pricing": False,
            "bend_percent": Decimal("0"),
            "bend_percent_used": Decimal("0"),
            "effective_markup_percent": markup_percent_used,
            "markup_percent_used": markup_percent_used,
            "mdc_markup_percent": markup_percent_used,
            "mdc_price": fixed_markup_mdc,
            "competitor_candidate_price": None,
            "chosen_competitor_price": None,
            "selected_competitor_price": None,
            "chosen_competitor_source": "",
            "chosen_competitor_rank": None,
            "rejected_competitors": [],
            "price_from_competitor": None,
            "final_price": fixed_markup_mdc,
            "reason": "fixed_markup_list_final",
            "log": "Цена рассчитана по списку фиксированной наценки. МДЦ рассчитана по марже из списка и применена как финальная цена. Конкуренты и прогиб не применялись.",
            "zone": zone,
            "zone_reference_price": zone_reference,
            "deviation_pct": deviation_pct,
        }
        fixed_markup_mdc, debug = _apply_memorandum_cap(price=fixed_markup_mdc, debug=debug, memorandum=memorandum)
        zone, zone_reference, deviation_pct = calculate_price_zone(
            fixed_markup_mdc,
            lowest_competitor_price=zone_competitor_price_min,
        )
        debug.update({"zone": zone, "zone_reference_price": zone_reference, "deviation_pct": deviation_pct})
        return fixed_markup_mdc, debug

    effective_city_id = region_id if region_id is not None else city_id_from_branch(price_format.branch)
    allowed_provisor_sources = allowed_provisor_source_names_for_city_id(effective_city_id)
    selected_meta = pricing_preload.selected_source_meta if pricing_preload is not None else _selected_source_meta(db, price_format.id)

    percentile_number = int(price_format.percentile_number or 10)
    percentile_match = find_match(LIST_TYPE_PERCENTILE_OVERRIDE)
    percentile_effect: dict | None = None
    if percentile_match is not None:
        percentile_value, list_id = percentile_match
        percentile_number = max(1, min(99, int(percentile_value)))
        percentile_effect = _list_effect(list_id, LIST_TYPE_PERCENTILE_OVERRIDE, percentile_value, "percentile_override")

    resolved_many = resolve_all_competitor_prices(
        db,
        price_format,
        product.id,
        allowed_provisor_sources=allowed_provisor_sources,
        percentile_price_cache=percentile_price_cache,
        percentile_number=percentile_number,
        pricing_preload=pricing_preload,
    )

    critical_markup_match = find_match(LIST_TYPE_CRITICAL_MARKUP)
    if critical_markup_match is not None and resolved_many.prices:
        critical_markup, list_id = critical_markup_match
        markup_fraction = _list_percent_as_fraction(critical_markup)
        markup_percent = markup_fraction
        applied_list_effects.append(
            _list_markup_match_effect(
                list_id,
                LIST_TYPE_CRITICAL_MARKUP,
                critical_markup,
                "critical_markup_mdc_override",
                markup_fraction=markup_fraction,
            )
        )

    min_markup_match = find_match(LIST_TYPE_MIN_MARKUP)
    if min_markup_match is not None:
        min_markup, list_id = min_markup_match
        markup_fraction = _list_percent_as_fraction(min_markup)
        markup_percent = max(markup_percent, markup_fraction)
        no_competitor_markup_percent = max(no_competitor_markup_percent, markup_fraction)
        applied_list_effects.append(
            _list_markup_match_effect(
                list_id,
                LIST_TYPE_MIN_MARKUP,
                min_markup,
                "min_markup_mdc_floor",
                markup_fraction=markup_fraction,
            )
        )

    max_markup_match = find_match(LIST_TYPE_MAX_MARKUP)
    if max_markup_match is not None:
        max_markup, list_id = max_markup_match
        markup_fraction = _list_percent_as_fraction(max_markup)
        markup_percent = min(markup_percent, markup_fraction)
        no_competitor_markup_percent = min(no_competitor_markup_percent, markup_fraction)
        applied_list_effects.append(
            _list_markup_match_effect(
                list_id,
                LIST_TYPE_MAX_MARKUP,
                max_markup,
                "max_markup_mdc_cap",
                markup_fraction=markup_fraction,
            )
        )

    if percentile_effect is not None:
        applied_list_effects.append(percentile_effect)

    # МДЦ (минимальная допустимая цена) — нижняя граница
    mdc = price_from_margin(cost, markup_percent)

    # For UI/debug only (legacy field name): base_price kept as МДЦ.
    base_price = mdc

    no_bend_match = find_match(LIST_TYPE_NO_BEND)
    fallback_bend_percent = _as_decimal(price_format.progib, Decimal("0")) or Decimal("0")
    if no_bend_match is not None:
        no_bend_value, list_id = no_bend_match
        if no_bend_value != 0:
            fallback_bend_percent = Decimal("0")
            applied_list_effects.append(_list_effect(list_id, LIST_TYPE_NO_BEND, no_bend_value, "no_bend"))
        else:
            no_bend_match = None

    competitor_price_min: Decimal | None = resolved_many.prices[0][0] if resolved_many.prices else None
    competitor_source_min: str = resolved_many.prices[0][1] if resolved_many.prices else ""
    markup_percent_used = markup_percent * Decimal("100")
    competitor_candidate_price: Decimal | None = None

    chosen_competitor: Decimal | None = None
    chosen_source: str = ""
    chosen_competitor_rank: int | None = None
    chosen_bend_percent: Decimal = fallback_bend_percent
    price_from_competitor: Decimal | None = None
    rejected_competitors: list[dict] = []
    competitor_price_details = resolved_many.details or {}

    # По формуле: считаем только от минимальной цены конкурента (Ц1).
    # Если Ц1*(1-прогиб) < МДЦ — берём МДЦ.
    price = mdc
    reason = "mdc_floor"

    if competitor_price_min is None:
        price = price_from_margin(cost, no_competitor_markup_percent)
        reason = "no_competitor_markup"
        no_competitor_candidate_below_mdc = price < mdc
    else:
        no_competitor_candidate_below_mdc = False
        for idx, (competitor_price, competitor_source) in enumerate(resolved_many.prices, start=1):
            if no_bend_match is not None:
                bend_percent = Decimal("0")
            else:
                bend_percent = bend_value(competitor_price, fallback_percent=fallback_bend_percent)
            candidate = competitor_price * (Decimal("1") - bend_percent / Decimal("100"))
            if competitor_candidate_price is None:
                competitor_candidate_price = candidate
            if candidate >= mdc:
                competitor_candidate_price = candidate
                chosen_competitor = competitor_price
                chosen_source = competitor_source
                chosen_competitor_rank = idx
                chosen_bend_percent = bend_percent
                price_from_competitor = candidate
                price = candidate
                reason = "competitor_bend"
                break
            rejected_competitors.append(
                {
                    "rank": idx,
                    "source": competitor_source,
                    "price": competitor_price,
                    "adjusted_price": competitor_price,
                    "original_price": competitor_price_details.get(competitor_source, {}).get("original_price", competitor_price),
                    "price_coefficient": competitor_price_details.get(competitor_source, {}).get("price_coefficient", Decimal("1")),
                    "candidate": candidate,
                    "mdc": mdc,
                }
            )
        else:
            reason = "all_competitors_failed_mdc"

    # Keep the pricing decision independent from any later Lists Management
    # constraint.  List effects must not replace the competitor/MDC explanation.
    pricing_reason = reason

    # Активные списки
    # Active lists were loaded before competitor resolution because some list
    # rules change competitor behavior for the product.

    # MVP constraints priority (deterministic):
    # 1) min/max margin (critical bounds)
    # 2) min/max price
    # 3) fixed price (overrides everything)
    # 4) rounding

    excluded_from_pricing = False
    skip_rounding_floor = False
    min_price_bound: Decimal | None = None

    exclude_match = find_match(LIST_TYPE_EXCLUDE_FROM_PRICING)
    if exclude_match is not None:
        exclude_value, list_id = exclude_match
        if exclude_value != 0:
            price = cost
            reason = "exclude_from_pricing_list"
            excluded_from_pricing = True
            skip_rounding_floor = True
            applied_list_effects.append(_list_effect(list_id, LIST_TYPE_EXCLUDE_FROM_PRICING, exclude_value, reason))

    if not excluded_from_pricing:
        fixed_price_match = find_match(LIST_TYPE_FIXED_PRICE)
        if fixed_price_match is not None:
            fixed_price, list_id = fixed_price_match
            price = fixed_price
            reason = "fixed_price_list"
            skip_rounding_floor = True
            applied_list_effects.append(_list_effect(list_id, LIST_TYPE_FIXED_PRICE, fixed_price, reason))
        else:
            min_price_match = find_match(LIST_TYPE_MIN_PRICE)
            if min_price_match is not None:
                min_price, list_id = min_price_match
                min_price_bound = min_price
                before_min_price = price
                price = max(price, min_price)
                reason = "min_price_floor"
                effect = _list_effect(list_id, LIST_TYPE_MIN_PRICE, min_price, reason)
                effect["changedFinalPrice"] = price != before_min_price
                if not effect["changedFinalPrice"]:
                    effect["effectMessage"] = "min_price checked but did not change final price because calculated price was already above the minimum."
                applied_list_effects.append(effect)

            max_price_match = find_match(LIST_TYPE_MAX_PRICE)
            if max_price_match is not None:
                max_price, list_id = max_price_match
                before_max_price = price
                price = min(price, max_price)
                reason = "max_price_cap"
                skip_rounding_floor = True
                effect = _list_effect(list_id, LIST_TYPE_MAX_PRICE, max_price, reason)
                effect["changedFinalPrice"] = price != before_max_price
                if not effect["changedFinalPrice"]:
                    effect["effectMessage"] = "max_price checked but did not change final price because calculated price was already below the maximum."
                applied_list_effects.append(effect)

    # Округление
    if not skip_rounding_floor:
        price = _round_price(price, rounding_rule)
    if not skip_rounding_floor and price < mdc:
        price = _round_price(mdc, rounding_rule, force_up=True)
        if competitor_price_min is None and no_competitor_candidate_below_mdc:
            reason = "no_competitor_markup_bumped_to_mdc"
        elif reason != "all_competitors_failed_mdc":
            reason = "mdc_floor_after_rounding"
        pricing_reason = reason

    # min_price is a hard final lower bound.  Rounding and MDC finalization run
    # before this guard so no later pricing stage can lower the list value.
    if min_price_bound is not None and price < min_price_bound:
        price = min_price_bound
        reason = "min_price_floor"

    # ЛП/ЗЛ/ПП
    zone_competitor_price_min = competitor_price_min
    if zone_competitor_price_min is None:
        zone_competitor_price_min = lowest_available_competitor_price(
            db,
            price_format.id,
            product.id,
            pricing_preload=pricing_preload,
        )

    zone, zone_reference, deviation_pct = calculate_price_zone(
        price,
        chosen_competitor_price=chosen_competitor,
        lowest_competitor_price=zone_competitor_price_min,
    )

    applied_source = chosen_source or competitor_source_min
    cached_match_type = _cached_source_match_type(pricing_preload, int(product.id), applied_source)
    source_match_type = cached_match_type if cached_match_type is not None else _source_match_type(db, price_format.id, product.id, applied_source)
    branch_id = str(region_id if region_id is not None else (price_format.branch or ""))
    list_names = {int(row.id): str(row.name or row.code or row.id) for row in active_lists}
    for effect in applied_list_effects:
        list_id = effect.get("listId")
        if list_id is not None:
            effect["listName"] = list_names.get(int(list_id), str(list_id))
    primary_list_effect = applied_list_effects[-1] if applied_list_effects else {}
    list_changed_final_price = primary_list_effect.get("changedFinalPrice")
    if primary_list_effect.get("type") in {LIST_TYPE_FIXED_MARKUP, LIST_TYPE_CRITICAL_MARKUP}:
        baseline_price = _diagnostic_price_before_margin_list(
            db=db,
            price_format=price_format,
            cost=cost,
            markup_percent=global_markup_percent,
            no_competitor_markup_percent=global_no_competitor_markup_percent,
            competitor_prices=resolved_many.prices,
            rounding_rule=rounding_rule,
            fallback_bend_percent=fallback_bend_percent,
            bend_ranges=pricing_preload.bend_ranges if pricing_preload is not None else None,
        )
        list_changed_final_price = price != baseline_price
        if not list_changed_final_price:
            primary_list_effect["effectMessage"] = (
                "margin list changed effective MDC parameters but did not change final price because "
                "the selected competitor candidate after bend stayed above MDC."
            )
    if list_changed_final_price is None and primary_list_effect:
        list_changed_final_price = True
    list_effect_message = str(primary_list_effect.get("effectMessage") or primary_list_effect.get("effect") or "")
    calculation_log = _build_calculation_log(
        reason=pricing_reason,
        competitor_prices=resolved_many.prices,
        chosen_source=chosen_source,
        chosen_competitor=chosen_competitor,
        chosen_rank=chosen_competitor_rank,
        no_competitor_markup_percent=no_competitor_markup_percent,
        markup_percent=markup_percent,
        source_labels=selected_meta.labels,
        margin_overridden_by_list=any(
            effect.get("type") in {LIST_TYPE_FIXED_MARKUP, LIST_TYPE_CRITICAL_MARKUP}
            for effect in applied_list_effects
        ),
    )
    chosen_details = (resolved_many.details or {}).get(chosen_source) if chosen_source else None
    if chosen_details:
        calculation_log = (
            f"{calculation_log} Original competitor price: {chosen_details.get('original_price')}; "
            f"priceCoefficient: {chosen_details.get('price_coefficient')}; "
            f"adjusted competitor price: {chosen_details.get('adjusted_price')}."
        )

    debug = {
        "cost": cost,
        "markup_percent": markup_percent,
        "base_price": base_price,
        "competitor_price": competitor_price_min,
        "lowest_competitor_price": zone_competitor_price_min,
        "competitor_source": competitor_source_min or ("нет цен ПЛК" if competitor_price_min is None else ""),
        "applied_source_name": applied_source,
        "applied_source_type": _source_type(applied_source),
        "used_percentile": applied_source.startswith("percentile:"),
        "used_substitute": source_match_type == "provisor_manual_substitute",
        "rating_global": rating_value("global"),
        "rating_local": rating_value("local", branch_id),
        "applied_list_effects": applied_list_effects,
        "applied_list_ids": sorted({int(item["listId"]) for item in applied_list_effects}),
        "applied_rule_type": str(primary_list_effect.get("type") or ""),
        "applied_rule_value": primary_list_effect.get("value"),
        "applied_list_id": primary_list_effect.get("listId"),
        "applied_list_name": str(primary_list_effect.get("listName") or ""),
        "applied_rule_ambiguous": bool(primary_list_effect.get("ambiguous")),
        "list_matched": bool(applied_list_effects),
        "list_applied": bool(applied_list_effects),
        "list_changed_final_price": list_changed_final_price,
        "list_effect_message": list_effect_message,
        "excluded_from_pricing": excluded_from_pricing,
        "bend_percent": chosen_bend_percent,
        "bend_percent_used": chosen_bend_percent,
        "effective_markup_percent": markup_percent_used,
        "markup_percent_used": markup_percent_used,
        "mdc_markup_percent": markup_percent_used,
        "mdc_price": mdc,
        "competitor_candidate_price": competitor_candidate_price,
        "chosen_competitor_price": chosen_competitor,
        "selected_competitor_price": chosen_competitor,
        "chosen_competitor_original_price": (
            competitor_price_details.get(chosen_source, {}).get("original_price")
            if chosen_source
            else None
        ),
        "chosen_competitor_price_coefficient": (
            competitor_price_details.get(chosen_source, {}).get("price_coefficient")
            if chosen_source
            else None
        ),
        "chosen_competitor_adjusted_price": chosen_competitor,
        "chosen_competitor_source": chosen_source,
        "chosen_competitor_rank": chosen_competitor_rank,
        "rejected_competitors": rejected_competitors,
        "price_from_competitor": price_from_competitor,
        "final_price": price,
        "reason": reason,
        "log": calculation_log,
        "zone": zone,
        "zone_reference_price": zone_reference,
        "deviation_pct": deviation_pct,
    }

    price, debug = _apply_memorandum_cap(price=price, debug=debug, memorandum=memorandum)
    zone, zone_reference, deviation_pct = calculate_price_zone(
        price,
        chosen_competitor_price=chosen_competitor,
        lowest_competitor_price=zone_competitor_price_min,
    )
    debug.update({"zone": zone, "zone_reference_price": zone_reference, "deviation_pct": deviation_pct})

    return price, debug


def _pretty_source(source: str) -> str:
    if source.startswith("provisor:"):
        return f"Provisor {source.split(':', 1)[1]}"
    if source.startswith("phcenter:"):
        return f"Фармцентр {source.split(':', 1)[1]}"
    if source.startswith("manual:"):
        return source.split(":", 1)[1]
    if source.startswith("percentile:"):
        return source.split(":", 1)[1]
    return source


def _source_label(source: str, labels: dict[str, str] | None = None) -> str:
    if labels and labels.get(source):
        return labels[source]
    return _pretty_source(source)


def _build_calculation_log(
    *,
    reason: str,
    competitor_prices: list[tuple[Decimal, str]],
    chosen_source: str,
    chosen_competitor: Decimal | None,
    chosen_rank: int | None = None,
    no_competitor_markup_percent: Decimal,
    markup_percent: Decimal,
    source_labels: dict[str, str] | None = None,
    margin_overridden_by_list: bool = False,
) -> str:
    if not competitor_prices:
        if margin_overridden_by_list:
            return "Нет цен выбранных конкурентов. Применена шкала маржи для товаров без конкурентов с учетом Lists Management."
        return "Нет цен выбранных конкурентов. Применена глобальная шкала маржи для товаров без конкурентов."

    if reason in {"competitor_bend", "competitor_bend_c1"} and chosen_competitor is not None:
        idx = chosen_rank or next((i + 1 for i, (_, src) in enumerate(competitor_prices) if src == chosen_source), 1)
        return f"Цена рассчитана относительно {idx}-й цены конкурента {_source_label(chosen_source, source_labels)}."
        return f"Цена рассчитана относительно конкурента {_source_label(chosen_source, source_labels)} ({idx}-я по величине цена)."

    if reason == "all_competitors_failed_mdc":
        return "Ни одна цена конкурентов не прошла условие минимальной допустимой цены. Применена минимальная наценка."

    if reason == "no_competitor_markup_bumped_to_mdc":
        return "Нет цен выбранных конкурентов. Цена по шкале без конкурентов ниже МДЦ, применена МДЦ."

    if reason == "mdc_floor_after_rounding":
        return "Цена поднята до МДЦ после округления или ограничений."

    if reason == "mdc_floor_after_competitor":
        first_source = _source_label(competitor_prices[0][1], source_labels)
        return (
            f"Цена установлена по минимальной наценке, так как первая цена конкурента {first_source} "
            "не прошла условие минимальной допустимой цены."
        )

    if reason == "min_margin_floor":
        return "Цена поднята до минимальной наценки."
    if reason == "max_margin_cap":
        return "Цена ограничена максимальной наценкой."
    if reason == "fixed_price_list":
        return "Финальная цена установлена напрямую из фиксированной цены; выбор конкурента и прогиб не применялись."

    pct = (markup_percent * Decimal("100")).quantize(Decimal("0.01"))
    return f"Цена рассчитана по основной шкале наценки: {pct}%."


def calculate_prices(
    *,
    db: Session,
    price_format_code: str,
    price_list_number: str,
    as_of: date,
    activation_date: date | None,
    user: str,
    region_id: int | None = None,
    force_new_price_list: bool = False,
) -> int:
    pf = db.execute(select(PriceFormat).where(PriceFormat.code == price_format_code)).scalars().first()
    if not pf:
        # MVP: allow calculating on an empty DB by creating the price format from mock data.
        meta = next((x for x in data.PRICE_FORMATS if x.get("code") == price_format_code), None)
        pf = PriceFormat(
            code=price_format_code,
            name=(meta.get("name") if meta else None) or price_format_code,
            branch=(meta.get("branch") if meta else None),
        )

        defaults = data.PRICING_SETTINGS_BY_FORMAT.get(price_format_code) or data.PRICING_SETTINGS_BY_FORMAT.get(
            "ИПЛ_01_001"
        )
        if defaults and defaults.get("deflectionPercent") is not None:
            try:
                pf.progib = float(defaults["deflectionPercent"])
            except Exception:
                pass

        db.add(pf)
        db.flush()
        propagate_emit_assignments_to_new_price_format(db=db, price_format_id=int(pf.id))

    def _get_defaults() -> dict:
        return data.PRICING_SETTINGS_BY_FORMAT.get(price_format_code) or data.PRICING_SETTINGS_BY_FORMAT.get(
            "ИПЛ_01_001"
        ) or {}

    def _seed_markup_ranges(defaults: dict) -> None:
        rec = (defaults or {}).get("recommendedMarkups") or []
        for row in rec:
            try:
                cost_from = float(row.get("lowerBound"))
                cost_to = float(row.get("upperBound")) if row.get("upperBound") is not None else None
                mp = float(row.get("markupPercent")) / 100.0
            except Exception:
                continue

            db.add(
                MarkupRange(
                    price_format_id=pf.id,
                    cost_from=cost_from,
                    cost_to=cost_to,
                    markup_percent=mp,
                )
            )

    def _seed_bend_ranges(defaults: dict) -> None:
        bends = (defaults or {}).get("bendRanges") or []
        for row in bends:
            try:
                price_from = float(row.get("priceFrom"))
                bend_percent = float(row.get("bendPercent"))
            except Exception:
                continue

            db.add(
                BendRange(
                    price_format_id=pf.id,
                    price_from=price_from,
                    bend_percent=bend_percent,
                )
            )

    def _is_legacy_seeded_markup(ranges: list[MarkupRange]) -> bool:
        # Detect old placeholder defaults that were used earlier in the MVP.
        # If ranges match this pattern, we can safely migrate them to current defaults.
        expected = [
            (Decimal("0"), Decimal("49.99"), Decimal("0.1")),
            (Decimal("50"), Decimal("99.99"), Decimal("0.08")),
            (Decimal("100"), Decimal("499.99"), Decimal("0.05")),
            (Decimal("500"), Decimal("999.99"), Decimal("0.04")),
            (Decimal("1000"), Decimal("2999.99"), Decimal("0.03")),
            (Decimal("3000"), Decimal("99999999"), Decimal("0.01")),
        ]

        if len(ranges) != len(expected):
            return False

        for r, (cf, ct, mp) in zip(sorted(ranges, key=lambda x: float(x.cost_from)), expected):
            cost_from = _as_decimal(r.cost_from, Decimal("0")) or Decimal("0")
            cost_to = _as_decimal(r.cost_to, Decimal("0")) or Decimal("0")
            markup = _as_decimal(r.markup_percent, Decimal("0")) or Decimal("0")

            if cost_from != cf or cost_to != ct or markup != mp:
                return False

        return True

    defaults = _get_defaults()

    # Ensure markup ranges exist (seed from defaults if needed)
    ranges = db.execute(
        select(MarkupRange).where(MarkupRange.price_format_id == pf.id).order_by(MarkupRange.cost_from.asc())
    ).scalars().all()
    if not ranges:
        _seed_markup_ranges(defaults)
        db.flush()
    elif _is_legacy_seeded_markup(ranges):
        # Migrate legacy placeholder defaults to current defaults.
        db.execute(delete(MarkupRange).where(MarkupRange.price_format_id == pf.id))
        _seed_markup_ranges(defaults)
        db.flush()

        # Also migrate fallback progib (used only when bend table is empty).
        try:
            legacy_progib = _as_decimal(pf.progib, Decimal("0")) or Decimal("0")
            new_progib = _as_decimal((defaults or {}).get("deflectionPercent"), Decimal("0")) or Decimal("0")
            if legacy_progib == Decimal("5") and new_progib != Decimal("0"):
                pf.progib = float(new_progib)
                db.flush()
        except Exception:
            pass

    # Ensure bend ranges exist (seed from defaults if needed)
    bend_rows = db.execute(
        select(BendRange).where(BendRange.price_format_id == pf.id).order_by(BendRange.price_from.asc())
    ).scalars().all()
    if not bend_rows:
        _seed_bend_ranges(defaults)
        db.flush()

    # Safety: if still no ranges, fail with clear message.
    ranges = db.execute(select(MarkupRange).where(MarkupRange.price_format_id == pf.id)).scalars().all()
    if not ranges:
        raise ValueError("Markup ranges are required")

    competitor_price_mode = _competitor_price_mode(pf)
    percentile_mode = competitor_price_mode in {"percentile", "mixed"}
    physical_mode = competitor_price_mode in {"regular", "mixed"}
    if percentile_mode and not physical_mode:
        existing_percentile_rows = (
            db.execute(
                select(CompetitorPricePercentile.id)
                .where(CompetitorPricePercentile.price_format_id == pf.id)
                .where(CompetitorPricePercentile.percentile_scope == REGIONAL_SCOPE)
                .where(CompetitorPricePercentile.value.is_not(None))
                .limit(1)
            )
            .scalars()
            .first()
        )
        if existing_percentile_rows is None:
            raise ValueError("Percentile rows are required before price generation. Refresh/recalculate competitors first.")
    if physical_mode:
        existing_competitor_rows = (
            db.execute(
                select(CompetitorPrice.id)
                .where(CompetitorPrice.price_format_id == pf.id)
                .where(CompetitorPrice.product_id.is_not(None))
                .limit(1)
            )
            .scalars()
            .first()
        )
        if existing_competitor_rows is None:
            rebuild_competitor_prices_for_selected(db=db, price_format_id=pf.id)
            db.flush()
    if percentile_mode and physical_mode:
        existing_percentile_rows = (
            db.execute(
                select(CompetitorPricePercentile.id)
                .where(CompetitorPricePercentile.price_format_id == pf.id)
                .where(CompetitorPricePercentile.percentile_scope == REGIONAL_SCOPE)
                .where(CompetitorPricePercentile.value.is_not(None))
                .limit(1)
            )
            .scalars()
            .first()
        )

    pl = db.execute(select(PriceList).where(PriceList.number == price_list_number)).scalars().first()
    if pl is not None and force_new_price_list:
        raise ValueError(f"price list number already exists: {price_list_number}")
    if not pl:
        pl = PriceList(
            number=price_list_number,
            price_format_id=pf.id,
            activation_date=activation_date,
            user=user,
            status="Активен" if activation_date else "Черновик",
        )
        db.add(pl)
        db.flush()

    branch_source = pf.branch if str(pf.branch or "").strip() else region_id
    branch_id = canonical_branch_id(branch_source)
    stock_snapshot = _load_stock_generation_snapshot(db, branch_id)
    stock_product_ids = list(stock_snapshot.product_ids)
    products_before_filter = int(db.execute(select(func.count(Product.id))).scalar() or 0)
    stock_rows_found = int(db.execute(select(func.count(BranchStock.id)).where(BranchStock.branch_id == branch_id)).scalar() or 0)
    cost_rows_found = int(db.execute(select(func.count(BranchCost.id)).where(BranchCost.branch_id == branch_id)).scalar() or 0)
    stock_product_count = len(stock_snapshot.product_ids)
    cost_product_count = len(stock_snapshot.cost_by_product_id)
    all_stock_branch_ids = [str(value or "") for value in db.execute(select(BranchStock.branch_id).distinct()).scalars().all()]
    all_cost_branch_ids = [str(value or "") for value in db.execute(select(BranchCost.branch_id).distinct()).scalars().all()]
    missing_stock_for_branch = stock_rows_found == 0

    product_stmt = select(Product)
    if missing_stock_for_branch:
        logger.warning(
            "[REFERENCE_FILTER] missing_stock requested_region_id=%s price_format_id=%s price_format_code=%s "
            "price_format_branch=%s resolved_branch_id=%s stock_rows_found=%s cost_rows_found=%s "
            "stock_product_count=%s cost_product_count=%s products_before_filter=%s products_after_filter=%s "
            "available_stock_branch_ids=%s available_cost_branch_ids=%s",
            region_id,
            pf.id,
            pf.code,
            pf.branch,
            branch_id,
            stock_rows_found,
            cost_rows_found,
            stock_product_count,
            cost_product_count,
            products_before_filter,
            0,
            all_stock_branch_ids,
            all_cost_branch_ids,
        )
        raise ValueError(MISSING_STOCK_REFERENCE_ERROR)

    stale_result = db.execute(
        delete(CalculatedPrice)
        .where(CalculatedPrice.price_list_id == pl.id)
        .where(CalculatedPrice.product_id.not_in(stock_product_ids))
    )
    stock_snapshot.reconciliation["stale_calculated_rows_removed"] = int(stale_result.rowcount or 0)
    product_stmt = product_stmt.where(Product.id.in_(stock_product_ids))

    products = db.execute(product_stmt).scalars().all()
    logger.info(
        "[REFERENCE_FILTER] requested_region_id=%s price_format_id=%s price_format_code=%s price_format_branch=%s "
        "resolved_branch_id=%s stock_rows_found=%s cost_rows_found=%s stock_product_count=%s cost_product_count=%s "
        "products_before_filter=%s products_after_filter=%s",
        region_id,
        pf.id,
        pf.code,
        pf.branch,
        branch_id,
        stock_rows_found,
        cost_rows_found,
        stock_product_count,
        cost_product_count,
        products_before_filter,
        len(products),
    )
    if not products:
        # MVP: allow creating a price list before importing products.
        db.commit()
        return 0

    active_lists = _active_lists_for_format(db, pf.id, as_of)
    _validate_list_rule_conflicts(
        db,
        lists=active_lists,
        products=products,
        price_format=pf,
    )

    rule = db.get(PricingRule, pf.pricing_rule_id) if pf.pricing_rule_id else None
    applied_rule_name = (rule.name if rule else None) or pf.pricing_rule or pf.code
    applied_rule_version = ""
    if rule is not None and getattr(rule, "updated_at", None):
        applied_rule_version = local_iso(rule.updated_at)
    pricing_preload = _build_pricing_preload(
        db,
        price_format=pf,
        products=products,
        active_lists=active_lists,
        branch_id=str(region_id if region_id is not None else (pf.branch or "")),
    )
    percentile_price_cache = load_percentile_price_cache(db, pf.id) if percentile_mode else None
    existing_by_product_id = {
        int(row.product_id): row
        for row in db.execute(select(CalculatedPrice).where(CalculatedPrice.price_list_id == pl.id)).scalars().all()
    }

    # upsert calculated_prices
    count = 0
    excluded_by_universal_lists = 0
    for p in products:
        exclude_match = _cached_list_match(pricing_preload, int(p.id), LIST_TYPE_EXCLUDE_FROM_PRICING)
        if exclude_match is not None and exclude_match[0] != 0:
            excluded_by_universal_lists += 1
            db.execute(
                delete(CalculatedPrice)
                .where(CalculatedPrice.price_list_id == pl.id)
                .where(CalculatedPrice.product_id == p.id)
            )
            continue

        price, debug = calculate_price_for_product(
            db=db,
            product=p,
            price_format=pf,
            as_of=as_of,
            region_id=region_id,
            active_lists=active_lists,
            percentile_price_cache=percentile_price_cache,
            cost_override=stock_snapshot.cost_by_product_id.get(int(p.id), Decimal("0")),
            pricing_preload=pricing_preload,
        )

        existing = existing_by_product_id.get(int(p.id))

        cp = existing or CalculatedPrice(price_list_id=pl.id, product_id=p.id)
        cp.cost = float(debug["cost"])
        cp.base_price = float(debug["base_price"])
        cp.competitor_price = float(debug["competitor_price"]) if debug["competitor_price"] is not None else None
        cp.price_from_competitor = (
            float(debug["price_from_competitor"]) if debug["price_from_competitor"] is not None else None
        )
        cp.lowest_competitor_price = (
            float(debug["lowest_competitor_price"]) if debug["lowest_competitor_price"] is not None else None
        )
        cp.chosen_competitor_price = (
            float(debug["chosen_competitor_price"]) if debug["chosen_competitor_price"] is not None else None
        )
        cp.bend_percent_used = float(debug["bend_percent_used"]) if debug["bend_percent_used"] is not None else None
        cp.markup_percent_used = (
            float(debug["markup_percent_used"]) if debug["markup_percent_used"] is not None else None
        )
        if hasattr(cp, "mdc_markup_percent"):
            cp.mdc_markup_percent = (
                float(debug["mdc_markup_percent"]) if debug["mdc_markup_percent"] is not None else None
            )
        if hasattr(cp, "mdc_price"):
            cp.mdc_price = float(debug["mdc_price"]) if debug["mdc_price"] is not None else None
        if hasattr(cp, "competitor_candidate_price"):
            cp.competitor_candidate_price = (
                float(debug["competitor_candidate_price"]) if debug["competitor_candidate_price"] is not None else None
        )
        cp.final_price = float(price)
        if hasattr(cp, "memorandum_max_price"):
            value = debug.get("memorandum_max_price")
            cp.memorandum_max_price = float(value) if value is not None else None
        if hasattr(cp, "price_before_memorandum"):
            value = debug.get("price_before_memorandum")
            cp.price_before_memorandum = float(value) if value is not None else None
        if hasattr(cp, "memorandum_applied"):
            cp.memorandum_applied = bool(debug.get("memorandum_applied"))
        if hasattr(cp, "memorandum_below_mdc"):
            cp.memorandum_below_mdc = bool(debug.get("memorandum_below_mdc"))
        if hasattr(cp, "memorandum_list_id"):
            value = debug.get("memorandum_list_id")
            cp.memorandum_list_id = int(value) if value else None
        if hasattr(cp, "memorandum_list_name"):
            cp.memorandum_list_name = str(debug.get("memorandum_list_name") or "")
        if hasattr(cp, "memorandum_diagnostic_code"):
            cp.memorandum_diagnostic_code = str(debug.get("memorandum_diagnostic_code") or "")
        cp.applied_reason = str(debug.get("log") or debug["reason"])
        cp.applied_source_name = str(debug.get("applied_source_name") or "")
        cp.applied_source_type = str(debug.get("applied_source_type") or "")
        cp.applied_rule_name = applied_rule_name
        cp.applied_rule_version = applied_rule_version
        cp.applied_list_ids = json.dumps(debug.get("applied_list_ids") or [], ensure_ascii=False)
        if hasattr(cp, "applied_rule_type"):
            cp.applied_rule_type = str(debug.get("applied_rule_type") or "")
        if hasattr(cp, "applied_rule_value"):
            value = debug.get("applied_rule_value")
            cp.applied_rule_value = float(value) if value is not None else None
        if hasattr(cp, "applied_list_id"):
            value = debug.get("applied_list_id")
            cp.applied_list_id = int(value) if value is not None else None
        cp.used_substitute = bool(debug.get("used_substitute"))
        cp.used_percentile = bool(debug.get("used_percentile"))
        cp.rating_global = debug.get("rating_global")
        cp.rating_local = debug.get("rating_local")
        cp.zone = str(debug["zone"] or "")

        if existing is None:
            db.add(cp)

        count += 1

    stock_snapshot.reconciliation["excluded_by_universal_lists"] = excluded_by_universal_lists
    stock_snapshot.reconciliation["calculated_rows"] = count
    reference_versions = {}
    try:
        reference_versions = json.loads(pl.run_reference_versions_json or "{}")
    except Exception:
        reference_versions = {}
    if not isinstance(reference_versions, dict):
        reference_versions = {}
    reference_versions["generationReconciliation"] = stock_snapshot.reconciliation
    pl.run_reference_versions_json = json.dumps(reference_versions, ensure_ascii=False, default=str)

    snapshot = {}
    try:
        snapshot = json.loads(pl.run_snapshot_json or "{}")
    except Exception:
        snapshot = {}
    if isinstance(snapshot, dict) and snapshot:
        snapshot["generationReconciliation"] = stock_snapshot.reconciliation
        pl.run_snapshot_json = json.dumps(snapshot, ensure_ascii=False, default=str)

    db.commit()
    return count
