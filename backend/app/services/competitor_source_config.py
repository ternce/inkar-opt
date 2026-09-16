from __future__ import annotations

import re

from ..models import CompetitorPriceList


MULTI_PRICE_PERCENTILE_MODE = "multi_price_per_sku"
EMIT_DISPLAY_NAMES_BY_FILIAL_ID = {
    "1052": "Эмити Интернешнл Алматы",
    "1076": "Эмити Интернешнл Астана",
    "1106": "Эмити Интернешнл Актау",
    "1107": "Эмити Интернешнл Шымкент",
    "1108": "Эмити Интернешнл Костанай",
    "1111": "Эмити Интернешнл Павлодар",
    "1114": "Эмити Интернешнл Уральск",
    "1140": "Эмити Интернешнл Талдыкорган",
    "1149": "Эмити Интернешнл Петропавловск",
}
_EMIT_SOURCE_KEY_RE = re.compile(r"^emit:(\d+)$", flags=re.IGNORECASE)
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


def _filial_id_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(int(text))
    except Exception:
        return text


def emit_display_name(filial_id: int | str | None, fallback: str | None = None) -> str:
    filial_text = _filial_id_text(filial_id)
    if filial_text in EMIT_DISPLAY_NAMES_BY_FILIAL_ID:
        return EMIT_DISPLAY_NAMES_BY_FILIAL_ID[filial_text]
    fallback_text = str(fallback or "").strip()
    if fallback_text:
        return fallback_text
    return f"Emit International {filial_text}" if filial_text else "Emit International"


def emit_filial_id_from_source_key(source_key: object) -> str:
    match = _EMIT_SOURCE_KEY_RE.match(str(source_key or "").strip())
    return _filial_id_text(match.group(1)) if match else ""


def emit_display_name_from_source_key(source_key: object, fallback: str | None = None) -> str:
    filial_id = emit_filial_id_from_source_key(source_key)
    return emit_display_name(filial_id, fallback) if filial_id else str(fallback or "").strip()


def emit_display_aliases(filial_id: int | str | None, fallback: str | None = None) -> set[str]:
    filial_text = _filial_id_text(filial_id)
    aliases: set[str] = set()
    display_name = emit_display_name(filial_text, fallback)
    if display_name:
        aliases.add(display_name)
    if filial_text:
        aliases.add(f"Emit International {filial_text}")
        aliases.add(f"Emit {filial_text}")
    fallback_text = str(fallback or "").strip()
    if fallback_text:
        aliases.add(fallback_text)
    return {item for item in aliases if item}


def emit_display_aliases_from_source_key(source_key: object, fallback: str | None = None) -> set[str]:
    filial_id = emit_filial_id_from_source_key(source_key)
    return emit_display_aliases(filial_id, fallback) if filial_id else {str(fallback or "").strip()} - {""}


def normalize_emit_display_value(source_key: object, value: object = "") -> str:
    mapped = emit_display_name_from_source_key(source_key)
    return mapped or str(value or "").strip()


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
        return f"account:{account_id}:main:{external_id}"
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
