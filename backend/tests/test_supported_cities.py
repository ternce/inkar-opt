from backend.app.models import PriceFormat
from backend.app.services.references.types import BRANCHES, USER_SELECTABLE_BRANCHES
from backend.app.services.regions import CITIES, SUPPORTED_USER_CITY_NAMES


def test_supported_user_city_list_is_exact_business_list():
    assert list(SUPPORTED_USER_CITY_NAMES) == [
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
    ]
    assert len(SUPPORTED_USER_CITY_NAMES) == 12


def test_unsupported_legacy_cities_are_not_user_selectable():
    hidden = {"Кызылорда", "Петропавловск", "Талдыкорган", "Уральск", "Орал", "Нур-Султан"}
    assert hidden.isdisjoint(set(SUPPORTED_USER_CITY_NAMES))
    assert hidden.isdisjoint({row["name"] for row in USER_SELECTABLE_BRANCHES})


def test_historical_city_mapping_rows_are_preserved_for_compatibility():
    legacy_names = {city.name for city in CITIES}
    assert {"Кызылорда", "Петропавловск", "Талдыкорган", "Уральск", "Нур-Султан"}.issubset(legacy_names)
    assert {"Кызылорда", "Петропавловск", "Талдыкорган", "Уральск"}.issubset(
        {row["name"] for row in BRANCHES}
    )

    historical = PriceFormat(code="LEGACY", name="Legacy", branch="Кызылорда")
    assert historical.branch == "Кызылорда"
