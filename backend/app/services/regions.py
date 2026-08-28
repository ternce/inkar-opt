from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class City:
    id: int
    name: str


# City IDs come from Помощник.md (ph.center region & Provisor cityId).
# Keep this minimal and explicit: only cities we actively use in mapping.
CITIES: list[City] = [
    City(1, "Алматы"),
    City(2, "Нур-Султан"),
    City(3, "Актау"),
    City(4, "Актобе"),
    City(5, "Атырау"),
    City(6, "Караганда"),
    City(8, "Костанай"),
    City(9, "Кызылорда"),
    City(10, "Павлодар"),
    City(11, "Петропавловск"),
    City(13, "Талдыкорган"),
    City(15, "Уральск"),
    City(16, "Усть-Каменогорск"),
    City(17, "Шымкент"),
]

SUPPORTED_USER_CITY_NAMES: tuple[str, ...] = (
    "Алматы",
    "Астана",
    "Шымкент",
    "Атырау",
    "Есик",
    "Караганда",
    "Костанай",
    "Семей",
    "Усть-Каменогорск",
    "Павлодар",
    "Актау",
    "Актобе",
)

SUPPORTED_USER_CITIES: tuple[City, ...] = tuple(
    City(index, name) for index, name in enumerate(SUPPORTED_USER_CITY_NAMES, start=1)
)


def _region_lookup_key(value: object) -> str:
    text = str(value or "").replace("\u00a0", " ").strip().casefold()
    text = re.sub(r"[.,]+", " ", text)
    text = re.sub(r"^(?:г|город)\s+", "", text)
    text = re.sub(r"\s+(?:г|город)$", "", text)
    return " ".join(text.split())


SUPPORTED_USER_CITY_ALIASES: dict[str, str] = {}
for _name in SUPPORTED_USER_CITY_NAMES:
    SUPPORTED_USER_CITY_ALIASES[_region_lookup_key(_name)] = _name
SUPPORTED_USER_CITY_ALIASES.update(
    {
        "almaty": "Алматы",
        "alma aty": "Алматы",
        "astana": "Астана",
        "нур султан": "Астана",
        "нурсултан": "Астана",
        "nur sultan": "Астана",
        "nur-sultan": "Астана",
        "nursultan": "Астана",
        "shymkent": "Шымкент",
        "atyrau": "Атырау",
        "esik": "Есик",
        "issyk": "Есик",
        "karaganda": "Караганда",
        "kostanay": "Костанай",
        "semey": "Семей",
        "усть каменогорск": "Усть-Каменогорск",
        "ust kamenogorsk": "Усть-Каменогорск",
        "ust-kamenogorsk": "Усть-Каменогорск",
        "oskemen": "Усть-Каменогорск",
        "pavlodar": "Павлодар",
        "aktau": "Актау",
        "aktobe": "Актобе",
    }
)


def canonical_supported_city_name(value: object) -> str:
    return SUPPORTED_USER_CITY_ALIASES.get(_region_lookup_key(value), "")


def is_supported_user_city_name(value: object) -> bool:
    return bool(canonical_supported_city_name(value))

CITY_ID_BY_NAME: dict[str, int] = {
    "Алматы": 1,
    "Нур-Султан": 2,
    "Нурсултан": 2,
    "Астана": 2,
    "Актау": 3,
    "Актобе": 4,
    "Атырау": 5,
    "Караганда": 6,
    "Костанай": 8,
    "Кызылорда": 9,
    "Павлодар": 10,
    "Петропавловск": 11,
    "Талдыкорган": 13,
    "Уральск": 15,
    "Орал": 15,
    "Усть-Каменогорск": 16,
    "Шымкент": 17,
}


# Provisor filial IDs by city (from Помощник.md).
# NOTE: Not all cities have known filial IDs in the doc.
PROVISOR_FILIAL_IDS_BY_CITY_ID: dict[int, list[int]] = {
    1: [128, 1052, 1075, 1397, 8322],
    2: [149, 1076],
    3: [159, 1106],
    4: [162],
    5: [1392],
    6: [148],
    8: [154, 1108],
    9: [158],
    10: [155, 1111],
    11: [1149],
    13: [151],
    15: [153, 1114],
    16: [152],
    17: [1107, 1145],
}


def city_id_from_branch(branch: str | None) -> int | None:
    b = (branch or "").strip()
    if not b:
        return None
    return CITY_ID_BY_NAME.get(canonical_supported_city_name(b) or b)


def allowed_provisor_source_names_for_city_id(city_id: int | None) -> set[str] | None:
    if city_id is None:
        return None

    ids = PROVISOR_FILIAL_IDS_BY_CITY_ID.get(int(city_id)) or []
    return {f"provisor:{i}" for i in ids}
