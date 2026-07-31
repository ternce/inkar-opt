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


def canonical_provisor_source_key(account_id: object, external_price_list_id: object) -> str:
    account = str(account_id or "").strip()
    external = str(external_price_list_id or "").strip()
    if account and external:
        return f"account:{account}:plk:{external}"
    if external:
        return f"plk:{external}"
    return ""


def canonical_competitor_source_key(row: CompetitorPriceList) -> str:
    """Return the stable business identity used for assignment/percentile joins."""

    source_key = str(row.source_key or "").strip()
    if source_key:
        return source_key

    source_type = _text(row.source_type)
    external_id = str(row.external_price_list_id or "").strip()
    account_id = str(row.account_id or "").strip()
    branch_id = str(row.branch_id or row.branch_code or "").strip()

    if source_type == "emit" and external_id:
        return f"emit:{external_id}"
    if default_percentile_mode_for_source(row) == MULTI_PRICE_PERCENTILE_MODE and external_id:
        return f"emit:{external_id}"
    if source_type == "provisor" and account_id and external_id:
        return canonical_provisor_source_key(account_id, external_id)
    if source_type == "provisor" and external_id:
        return f"plk:{external_id}"
    if source_type == "vidman" and account_id and external_id:
        return f"{account_id}:{external_id}"
    if source_type == "manual" and external_id:
        return f"manual:{external_id}"
    if source_type and account_id and branch_id:
        return f"{source_type}:{account_id}:{branch_id}"
    return ""


def ensure_canonical_source_key(row: CompetitorPriceList) -> str:
    key = canonical_competitor_source_key(row)
    if key and not str(row.source_key or "").strip():
        row.source_key = key
    return key
