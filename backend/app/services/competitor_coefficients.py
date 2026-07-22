from __future__ import annotations

from ..models import CompetitorPriceList


def validate_price_coefficient(value: object) -> float:
    try:
        parsed = float(value)
    except Exception as exc:
        raise ValueError("priceCoefficient must be a number") from exc
    if not (0.01 <= parsed <= 100):
        raise ValueError("priceCoefficient must be between 0.01 and 100")
    return parsed


def effective_price_coefficient(row: CompetitorPriceList) -> float:
    try:
        return validate_price_coefficient(row.price_coefficient)
    except Exception:
        return 1.0
