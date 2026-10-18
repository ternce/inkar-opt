from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Mapping

from ..sku import normalize_sku


def as_quantity(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def compare_stock_quantities(
    expected: Mapping[str, Decimal | None],
    actual: Mapping[str, Decimal | None],
    *,
    example_limit: int = 100,
) -> dict:
    common = sorted(set(expected) & set(actual))
    mismatches = [sku for sku in common if expected[sku] != actual[sku]]
    return {
        "expectedSkuCount": len(expected),
        "actualSkuCount": len(actual),
        "commonSkuCount": len(common),
        "matchingQuantityCount": len(common) - len(mismatches),
        "mismatchCount": len(mismatches),
        "missingFromActual": sorted(set(expected) - set(actual))[:example_limit],
        "missingFromExpected": sorted(set(actual) - set(expected))[:example_limit],
        "mismatches": [
            {
                "sku": sku,
                "expected": str(expected[sku]) if expected[sku] is not None else None,
                "actual": str(actual[sku]) if actual[sku] is not None else None,
            }
            for sku in mismatches[:example_limit]
        ],
    }


def normalized_quantity_map(rows: list[tuple[object, object]]) -> dict[str, Decimal | None]:
    result: dict[str, Decimal | None] = {}
    for raw_sku, raw_quantity in rows:
        sku = normalize_sku(raw_sku)
        if not sku:
            continue
        if sku in result:
            raise ValueError(f"ambiguous duplicate SKU in diagnostic input: {sku}")
        result[sku] = as_quantity(raw_quantity)
    return result
