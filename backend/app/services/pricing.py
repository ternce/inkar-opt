from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from ..models import (
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
    PriceList,
    CalculatedPrice,
    ProductRating,
    PricingRule,
    RoundingRule,
)
from .. import data
from ..timezone import local_iso
from .competitor_matching import rebuild_competitor_prices_for_selected
from .competitor_percentiles import emit_percentile_group_keys, recalculate_competitor_percentiles_if_needed
from .competitor_percentiles import REGIONAL_SCOPE
from .competitor_assignments import get_assigned_competitor_price_lists
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


@dataclass(frozen=True)
class CompetitorResolved:
    competitor_price: Decimal | None
    applied_source: str


@dataclass(frozen=True)
class CompetitorResolvedMany:
    prices: list[tuple[Decimal, str]]  # (computed_price, source_name)


PercentilePriceCache = dict[int, dict[int, list[tuple[Decimal, str]]]]


@dataclass(frozen=True)
class SelectedSourceMeta:
    selected_sources: set[str]
    labels: dict[str, str]


def _as_decimal(value: object, default: Decimal | None = None) -> Decimal | None:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


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
) -> Decimal | None:
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
) -> Decimal | None:
    if (price_format.competitor_price_mode or "regular") == "percentile" and percentile_price_cache is not None:
        zone_percentile_number = int(price_format.percentile_number or 10)
        zone_resolved = resolve_percentile_prices_from_cache(
            percentile_price_cache,
            product_id,
            percentile_number=zone_percentile_number,
        )
        return zone_resolved.prices[0][0] if zone_resolved.prices else None
    return lowest_available_competitor_price(db, price_format.id, product_id)


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
) -> CompetitorResolvedMany:
    config_rows = db.execute(
        select(CompetitorPrice)
        .where(CompetitorPrice.price_format_id == price_format_id)
        .where(CompetitorPrice.product_id.is_(None))
    ).scalars().all()

    if not config_rows:
        return CompetitorResolvedMany([])

    out: list[tuple[Decimal, str]] = []
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

        out.append((source_price * coefficient, source_name))

    out.sort(key=lambda x: x[0])
    return CompetitorResolvedMany(out)


def resolve_percentile_prices(
    db: Session,
    price_format_id: int,
    product_id: int,
    *,
    percentile_number: int,
) -> CompetitorResolvedMany:
    active_groups = emit_percentile_group_keys(db=db, price_format_id=price_format_id)
    if not active_groups:
        return CompetitorResolvedMany([])
    rows = (
        db.execute(
            select(CompetitorPricePercentile)
            .where(CompetitorPricePercentile.price_format_id == price_format_id)
            .where(CompetitorPricePercentile.product_id == product_id)
            .where(CompetitorPricePercentile.percentile_scope == REGIONAL_SCOPE)
            .where(CompetitorPricePercentile.percentile == percentile_number)
            .where(CompetitorPricePercentile.value.is_not(None))
        )
        .scalars()
        .all()
    )
    out: list[tuple[Decimal, str]] = []
    for row in rows:
        if (str(row.branch_name or ""), str(row.competitor_name or "")) not in active_groups:
            continue
        value = _as_decimal(row.value)
        if value is None or value <= 0:
            continue
        src = f"percentile:{row.competitor_name}:{row.branch_name}:p{row.percentile}"
        out.append((value, src))
    out.sort(key=lambda x: x[0])
    return CompetitorResolvedMany(out)


def load_percentile_price_cache(db: Session, price_format_id: int) -> PercentilePriceCache:
    active_groups = emit_percentile_group_keys(db=db, price_format_id=price_format_id)
    if not active_groups:
        return {}
    rows = (
        db.execute(
            select(
                CompetitorPricePercentile.product_id,
                CompetitorPricePercentile.percentile,
                CompetitorPricePercentile.value,
                CompetitorPricePercentile.competitor_name,
                CompetitorPricePercentile.branch_name,
            )
            .where(CompetitorPricePercentile.price_format_id == price_format_id)
            .where(CompetitorPricePercentile.percentile_scope == REGIONAL_SCOPE)
            .where(CompetitorPricePercentile.value.is_not(None))
            .order_by(
                CompetitorPricePercentile.product_id.asc(),
                CompetitorPricePercentile.percentile.asc(),
                CompetitorPricePercentile.value.asc(),
            )
        )
        .all()
    )
    cache: PercentilePriceCache = {}
    for product_id, percentile, value, competitor_name, branch_name in rows:
        if (str(branch_name or ""), str(competitor_name or "")) not in active_groups:
            continue
        price = _as_decimal(value)
        if price is None or price <= 0:
            continue
        src = f"percentile:{competitor_name}:{branch_name}:p{percentile}"
        cache.setdefault(int(product_id), {}).setdefault(int(percentile), []).append((price, src))
    return cache


def resolve_percentile_prices_from_cache(
    cache: PercentilePriceCache,
    product_id: int,
    *,
    percentile_number: int,
) -> CompetitorResolvedMany:
    prices = list(cache.get(int(product_id), {}).get(int(percentile_number), []))
    prices.sort(key=lambda x: x[0])
    return CompetitorResolvedMany(prices)


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

    rows = db.execute(
        select(ListItem)
        .where(ListItem.universal_list_id.in_(list_ids))
        .where(ListItem.product_id == product_id)
        .order_by(ListItem.universal_list_id.asc(), ListItem.id.asc())
        .limit(2)
    ).scalars().all()

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
) -> Decimal:
    mdc = price_from_margin(cost, markup_percent)
    if not competitor_prices:
        price = price_from_margin(cost, no_competitor_markup_percent)
        if price < mdc:
            price = mdc
    else:
        price = mdc
        for competitor_price, _competitor_source in competitor_prices:
            bend_percent = get_bend_percent_by_price_range(
                db,
                price_format.id,
                competitor_price,
                fallback_percent=fallback_bend_percent,
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
) -> tuple[Decimal, dict]:
    cost = _as_decimal(product.cost)
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
            "rating_global": _rating_value(db, product.id, "global"),
            "rating_local": _rating_value(db, product.id, "local", branch_id),
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

    markup_percent = get_markup_percent_by_range(db, price_format.id, cost)
    no_competitor_markup_percent = get_no_competitor_markup_percent_by_range(
        db,
        price_format.id,
        cost,
        fallback=markup_percent,
    )
    global_markup_percent = markup_percent
    global_no_competitor_markup_percent = no_competitor_markup_percent
    rounding_rule = db.get(RoundingRule, price_format.rounding_rule_id) if price_format.rounding_rule_id else None
    active_lists = active_lists if active_lists is not None else _active_lists_for_format(db, price_format.id, as_of)
    applied_list_effects: list[dict] = []

    fixed_price_match = _find_item_match(db, active_lists, product.id, LIST_TYPE_FIXED_PRICE)
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
            "rating_global": _rating_value(db, product.id, "global"),
            "rating_local": _rating_value(db, product.id, "local", branch_id),
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
        return fixed_price, debug

    fixed_markup_match = _find_item_match(db, active_lists, product.id, LIST_TYPE_FIXED_MARKUP)
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
            "rating_global": _rating_value(db, product.id, "global"),
            "rating_local": _rating_value(db, product.id, "local", branch_id),
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
        return fixed_markup_mdc, debug

    effective_city_id = region_id if region_id is not None else city_id_from_branch(price_format.branch)
    allowed_provisor_sources = allowed_provisor_source_names_for_city_id(effective_city_id)
    selected_meta = _selected_source_meta(db, price_format.id)

    percentile_number = int(price_format.percentile_number or 10)
    percentile_match = _find_item_match(db, active_lists, product.id, LIST_TYPE_PERCENTILE_OVERRIDE)
    percentile_effect: dict | None = None
    if percentile_match is not None:
        percentile_value, list_id = percentile_match
        percentile_number = max(1, min(99, int(percentile_value)))
        percentile_effect = _list_effect(list_id, LIST_TYPE_PERCENTILE_OVERRIDE, percentile_value, "percentile_override")

    if (price_format.competitor_price_mode or "regular") == "percentile":
        if percentile_price_cache is not None:
            resolved_many = resolve_percentile_prices_from_cache(
                percentile_price_cache,
                product.id,
                percentile_number=percentile_number,
            )
        else:
            resolved_many = resolve_percentile_prices(
                db,
                price_format.id,
                product.id,
                percentile_number=percentile_number,
            )
    else:
        resolved_many = resolve_competitor_prices(
            db,
            price_format.id,
            product.id,
            allowed_provisor_sources=allowed_provisor_sources,
        )

    critical_markup_match = _find_item_match(db, active_lists, product.id, LIST_TYPE_CRITICAL_MARKUP)
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

    min_markup_match = _find_item_match(db, active_lists, product.id, LIST_TYPE_MIN_MARKUP)
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

    max_markup_match = _find_item_match(db, active_lists, product.id, LIST_TYPE_MAX_MARKUP)
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

    no_bend_match = _find_item_match(db, active_lists, product.id, LIST_TYPE_NO_BEND)
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
                bend_percent = get_bend_percent_by_price_range(
                    db,
                    price_format.id,
                    competitor_price,
                    fallback_percent=fallback_bend_percent,
                )
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

    exclude_match = _find_item_match(db, active_lists, product.id, LIST_TYPE_EXCLUDE_FROM_PRICING)
    if exclude_match is not None:
        exclude_value, list_id = exclude_match
        if exclude_value != 0:
            price = cost
            reason = "exclude_from_pricing_list"
            excluded_from_pricing = True
            skip_rounding_floor = True
            applied_list_effects.append(_list_effect(list_id, LIST_TYPE_EXCLUDE_FROM_PRICING, exclude_value, reason))

    if not excluded_from_pricing:
        fixed_price_match = _find_item_match(db, active_lists, product.id, LIST_TYPE_FIXED_PRICE)
        if fixed_price_match is not None:
            fixed_price, list_id = fixed_price_match
            price = fixed_price
            reason = "fixed_price_list"
            skip_rounding_floor = True
            applied_list_effects.append(_list_effect(list_id, LIST_TYPE_FIXED_PRICE, fixed_price, reason))
        else:
            min_price_match = _find_item_match(db, active_lists, product.id, LIST_TYPE_MIN_PRICE)
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

            max_price_match = _find_item_match(db, active_lists, product.id, LIST_TYPE_MAX_PRICE)
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
        zone_competitor_price_min = lowest_available_competitor_price(db, price_format.id, product.id)

    zone, zone_reference, deviation_pct = calculate_price_zone(
        price,
        chosen_competitor_price=chosen_competitor,
        lowest_competitor_price=zone_competitor_price_min,
    )

    applied_source = chosen_source or competitor_source_min
    source_match_type = _source_match_type(db, price_format.id, product.id, applied_source)
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
        "rating_global": _rating_value(db, product.id, "global"),
        "rating_local": _rating_value(db, product.id, "local", branch_id),
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

    percentile_mode = (pf.competitor_price_mode or "regular") == "percentile"
    if percentile_mode:
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
    else:
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
            recalculate_competitor_percentiles_if_needed(db=db, price_format_id=pf.id)
            db.flush()

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

    products = db.execute(select(Product)).scalars().all()
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
    percentile_price_cache = load_percentile_price_cache(db, pf.id) if percentile_mode else None

    # upsert calculated_prices
    count = 0
    for p in products:
        exclude_match = _find_item_match(db, active_lists, p.id, LIST_TYPE_EXCLUDE_FROM_PRICING)
        if exclude_match is not None and exclude_match[0] != 0:
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
        )

        existing = db.execute(
            select(CalculatedPrice)
            .where(CalculatedPrice.price_list_id == pl.id)
            .where(CalculatedPrice.product_id == p.id)
        ).scalars().first()

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

    db.commit()
    return count
