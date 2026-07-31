from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CompetitorPricePercentile, RegularCompetitorPricePercentile
from .competitors.identity import canonical_regular_competitor_identity
from .competitors.percentiles.sources import (
    PERCENTILE_SOURCE_COMPETITOR,
    PERCENTILE_SOURCE_EMIT,
    percentile_source_id,
)

logger = logging.getLogger(__name__)

REGULAR_COMPETITOR_SCOPE = "regular_competitor"


@dataclass(frozen=True)
class RegularPercentileExportConfig:
    source_name: str
    canonical_identity: str
    percentile: int


def percentile_export_source_names(row: CompetitorPricePercentile) -> tuple[str, str]:
    """Return the selectable export identities represented by a percentile row."""

    emit_source = percentile_source_id(
        percentile_source=PERCENTILE_SOURCE_EMIT,
        price_format_id=row.price_format_id,
        scope=row.percentile_scope,
        source_key=row.source_key,
        region=row.branch_name,
        competitor=row.competitor_name,
        percentile=row.percentile,
    )
    competitor_source = percentile_source_id(
        percentile_source=PERCENTILE_SOURCE_COMPETITOR,
        price_format_id=row.price_format_id,
        scope="global",
        source_key=row.source_key,
        region="",
        competitor=row.competitor_name,
        percentile=row.percentile,
    )
    return f"percentile:{emit_source}", f"percentile:{competitor_source}"


def _parse_regular_percentile_source_name(
    source_name: str,
    *,
    price_format_id: int,
) -> RegularPercentileExportConfig | None:
    prefix = f"percentile:{PERCENTILE_SOURCE_COMPETITOR}:{price_format_id}:{REGULAR_COMPETITOR_SCOPE}:"
    if not source_name.startswith(prefix):
        return None
    rest = source_name.removeprefix(prefix)
    marker = ":p"
    if marker not in rest:
        return None
    before_percentile, percentile_text = rest.rsplit(marker, 1)
    try:
        percentile = int(percentile_text)
    except Exception:
        return None
    identity_text = before_percentile.split("::", 1)[0].strip()
    canonical_identity = canonical_regular_competitor_identity(identity_text)
    if not canonical_identity:
        return None
    return RegularPercentileExportConfig(
        source_name=source_name,
        canonical_identity=canonical_identity,
        percentile=percentile,
    )


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def load_percentile_export_price_cells(
    *,
    db: Session,
    price_format_id: int,
    product_ids: list[int],
    selected_source_names: set[str],
) -> dict[int, dict[str, dict]]:
    if not product_ids or not selected_source_names:
        return {}

    product_id_set = {int(product_id) for product_id in product_ids if product_id}
    selected_names = {str(source_name or "").strip() for source_name in selected_source_names if source_name}
    if not product_id_set or not selected_names:
        return {}

    out: dict[int, dict[str, dict]] = {}
    rows_loaded = 0
    values_written = 0

    rows = (
        db.execute(
            select(CompetitorPricePercentile)
            .where(CompetitorPricePercentile.price_format_id == price_format_id)
            .where(CompetitorPricePercentile.product_id.in_(sorted(product_id_set)))
            .where(CompetitorPricePercentile.value.is_not(None))
            .order_by(
                CompetitorPricePercentile.product_id.asc(),
                CompetitorPricePercentile.source_key.asc(),
                CompetitorPricePercentile.percentile_scope.asc(),
                CompetitorPricePercentile.percentile.asc(),
                CompetitorPricePercentile.id.asc(),
            )
        )
        .scalars()
        .all()
    )
    rows_loaded += len(rows)

    for row in rows:
        value = _decimal_or_none(row.value)
        if value is None:
            continue
        for source_name in percentile_export_source_names(row):
            if source_name not in selected_names:
                continue
            out.setdefault(int(row.product_id), {})[source_name] = {
                "value": value,
                "sourcePrice": value,
                "originalPrice": value,
                "sourceType": "percentile",
                "percentileSourceType": "emit",
                "percentile": int(row.percentile),
                "sourceCount": int(row.source_count or 0),
                "priceCount": int(row.price_count or 0),
            }
            values_written += 1

    regular_configs: dict[tuple[str, int], list[RegularPercentileExportConfig]] = {}
    for source_name in sorted(selected_names):
        config = _parse_regular_percentile_source_name(source_name, price_format_id=price_format_id)
        if config is None:
            continue
        regular_configs.setdefault((config.canonical_identity, config.percentile), []).append(config)
        logger.info(
            "[PERCENTILE_PRICING_CONFIG] config_id=%s price_format_id=%s source_scope=%s raw_source_name=%s "
            "canonical_identity=%s percentile_level=%s column_key=%s",
            source_name,
            price_format_id,
            REGULAR_COMPETITOR_SCOPE,
            source_name,
            config.canonical_identity,
            config.percentile,
            source_name,
        )

    regular_rows_loaded = 0
    regular_values_written = 0
    lookups_attempted = len(product_id_set) * sum(len(configs) for configs in regular_configs.values())
    missing_by_identity: dict[str, int] = {}
    if regular_configs:
        identities = sorted({identity for identity, _pct in regular_configs})
        percentiles = sorted({pct for _identity, pct in regular_configs})
        regular_rows = (
            db.execute(
                select(RegularCompetitorPricePercentile)
                .where(RegularCompetitorPricePercentile.competitor_identity.in_(identities))
                .where(RegularCompetitorPricePercentile.product_id.in_(sorted(product_id_set)))
                .where(RegularCompetitorPricePercentile.percentile.in_(percentiles))
                .where(RegularCompetitorPricePercentile.value.is_not(None))
                .order_by(
                    RegularCompetitorPricePercentile.competitor_identity.asc(),
                    RegularCompetitorPricePercentile.percentile.asc(),
                    RegularCompetitorPricePercentile.product_id.asc(),
                    RegularCompetitorPricePercentile.id.asc(),
                )
            )
            .scalars()
            .all()
        )
        regular_rows_loaded = len(regular_rows)
        products_loaded_by_key: dict[tuple[str, int], set[int]] = {key: set() for key in regular_configs}
        for row in regular_rows:
            identity = canonical_regular_competitor_identity(row.competitor_identity)
            percentile = int(row.percentile)
            configs = regular_configs.get((identity, percentile))
            if not configs:
                continue
            value = _decimal_or_none(row.value)
            if value is None:
                continue
            product_id = int(row.product_id)
            products_loaded_by_key.setdefault((identity, percentile), set()).add(product_id)
            for config in configs:
                logger.debug(
                    "[PERCENTILE_PRODUCT_LOOKUP] product_id=%s sku= canonical_identity=%s percentile_level=%s "
                    "found=true value=%s sample_count=%s column_key=%s",
                    product_id,
                    identity,
                    percentile,
                    value,
                    int(row.sample_count or 0),
                    config.source_name,
                )
                out.setdefault(product_id, {})[config.source_name] = {
                    "value": value,
                    "sourcePrice": value,
                    "originalPrice": value,
                    "sourceType": "percentile",
                    "percentileSourceType": "regular_competitor",
                    "canonicalIdentity": identity,
                    "percentile": percentile,
                    "sampleCount": int(row.sample_count or 0),
                    "sourceCount": int(row.source_count or 0),
                    "minPrice": _decimal_or_none(row.min_price),
                    "maxPrice": _decimal_or_none(row.max_price),
                }
                regular_values_written += 1

        for key, configs in regular_configs.items():
            identity, percentile = key
            products_loaded = products_loaded_by_key.get(key, set())
            missing_count = max(0, len(product_id_set) * len(configs) - len(products_loaded) * len(configs))
            if missing_count:
                missing_by_identity[identity] = missing_by_identity.get(identity, 0) + missing_count
            logger.info(
                "[PERCENTILE_PRICING_CACHE] canonical_identity=%s percentile_level=%s rows_loaded=%s products_loaded=%s",
                identity,
                percentile,
                len(products_loaded),
                len(products_loaded),
            )

    if regular_configs:
        logger.info(
            "[PERCENTILE_PRICING_SUMMARY] configs_selected=%s cache_rows_loaded=%s products_processed=%s "
            "lookups_attempted=%s lookups_found=%s lookups_missing=%s column_values_written=%s candidates_used=%s "
            "missing_by_identity=%s",
            sum(len(configs) for configs in regular_configs.values()),
            regular_rows_loaded,
            len(product_id_set),
            lookups_attempted,
            regular_values_written,
            max(0, lookups_attempted - regular_values_written),
            regular_values_written,
            regular_values_written,
            missing_by_identity,
        )

    logger.debug(
        "[PERCENTILE_PRICING_SUMMARY] configs_selected=%s cache_rows_loaded=%s products_processed=%s "
        "column_values_written=%s",
        len(selected_names),
        rows_loaded + regular_rows_loaded,
        len(product_id_set),
        values_written + regular_values_written,
    )
    return out


def load_percentile_export_prices(
    *,
    db: Session,
    price_format_id: int,
    product_ids: list[int],
    selected_source_names: set[str],
) -> dict[int, dict[str, Decimal]]:
    cells = load_percentile_export_price_cells(
        db=db,
        price_format_id=price_format_id,
        product_ids=product_ids,
        selected_source_names=selected_source_names,
    )
    out: dict[int, dict[str, Decimal]] = {}
    for product_id, values_by_source in cells.items():
        for source_name, cell in values_by_source.items():
            value = cell.get("value")
            if isinstance(value, Decimal):
                out.setdefault(product_id, {})[source_name] = value
    return out
