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

