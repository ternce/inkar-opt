from __future__ import annotations


REQUIRED_COLUMNS = {
    "stock": ("sku", "stock"),
    "cost": ("sku", "cost"),
    "rating_global": ("sku", "rating_global"),
    "rating_local": ("sku", "rating_local"),
    "products": ("sku", "name"),
    "counterparties": ("name",),
    "holdings": ("name",),
    "delivery_points": ("name",),
}


def required_columns_for(data_type: str) -> tuple[str, ...]:
    return REQUIRED_COLUMNS.get(data_type, ())

