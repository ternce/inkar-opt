from __future__ import annotations


REFERENCE_TYPES = [
    {"code": "stock", "name": "Остатки"},
    {"code": "cost", "name": "Себестоимость"},
    {"code": "rating_global", "name": "Рейтинг общий"},
    {"code": "rating_local", "name": "Рейтинг локальный"},
    {"code": "products", "name": "Номенклатура"},
    {"code": "counterparties", "name": "Контрагенты"},
    {"code": "holdings", "name": "Холдинги"},
    {"code": "delivery_points", "name": "Точки доставки"},
]

REFERENCE_TYPE_BY_CODE = {row["code"]: row for row in REFERENCE_TYPES}

BRANCHES = [
    {"id": "1", "name": "Алматы"},
    {"id": "2", "name": "Астана"},
    {"id": "17", "name": "Шымкент"},
    {"id": "3", "name": "Актау"},
    {"id": "4", "name": "Актобе"},
    {"id": "5", "name": "Атырау"},
    {"id": "6", "name": "Караганда"},
    {"id": "8", "name": "Костанай"},
    {"id": "9", "name": "Кызылорда"},
    {"id": "10", "name": "Павлодар"},
    {"id": "11", "name": "Петропавловск"},
    {"id": "13", "name": "Талдыкорган"},
    {"id": "15", "name": "Уральск"},
    {"id": "16", "name": "Усть-Каменогорск"},
]

BRANCH_BY_ID = {row["id"]: row for row in BRANCHES}

BRANCH_ALIASES = {
    "almaty": "1",
    "astana": "2",
    "nur-sultan": "2",
    "nursultan": "2",
    "aktau": "3",
    "aktobe": "4",
    "atyrau": "5",
    "karaganda": "6",
    "kostanay": "8",
    "kyzylorda": "9",
    "pavlodar": "10",
    "petropavlovsk": "11",
    "taldykorgan": "13",
    "uralsk": "15",
    "oral": "15",
    "ust-kamenogorsk": "16",
    "shymkent": "17",
}


def _branch_lookup_key(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def canonical_branch_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text in BRANCH_BY_ID:
        return text
    if text.isdigit():
        normalized = str(int(text))
        if normalized in BRANCH_BY_ID:
            return normalized
    lookup = _branch_lookup_key(text)
    alias = BRANCH_ALIASES.get(lookup)
    if alias:
        return alias
    for branch in BRANCHES:
        if _branch_lookup_key(branch.get("name")) == lookup:
            return str(branch["id"])
    return text


def branch_display_name(branch_id: object) -> str:
    canonical = canonical_branch_id(branch_id)
    return BRANCH_BY_ID.get(canonical, {}).get("name", str(branch_id or "").strip())
