from __future__ import annotations

from ..models import CompetitorPriceList


MULTI_PRICE_PERCENTILE_MODE = "multi_price_per_sku"
EMIT_SOURCE_MARKERS = (
    "emit",
    "emiti",
    "emity",
    "amity",
    "эмит",
    "эмити",
    "СЌРјРёС‚",
    "Р­РјРёС‚",
)


def _text(value: object) -> str:
    return str(value or "").strip().casefold()


def default_percentile_mode_for_source(row: CompetitorPriceList) -> str:
    """Default source behavior kept outside percentile calculation logic."""

    source_type = _text(row.source_type)
    names = " ".join(
        _text(value)
        for value in (
            row.source_key,
            row.display_name,
            row.supplier,
            row.competitor_name,
            row.account_login,
        )
    )
    if source_type == "emit" or any(marker.casefold() in names for marker in EMIT_SOURCE_MARKERS):
        return MULTI_PRICE_PERCENTILE_MODE
    return ""


def effective_percentile_mode(row: CompetitorPriceList, configured_mode: object = "") -> str:
    mode = str(configured_mode or "").strip()
    return mode or default_percentile_mode_for_source(row)
