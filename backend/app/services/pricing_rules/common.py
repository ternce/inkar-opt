from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from ...timezone import now_kz_naive


def as_decimal(value: object, field: str) -> Decimal:
    try:
        dec = Decimal(str(value))
    except Exception:
        raise ValueError(f"{field} must be numeric")
    return dec


def validate_ranges(rows: Iterable[dict], percent_key: str) -> list[dict]:
    normalized: list[dict] = []
    for idx, row in enumerate(rows):
        cost_from = as_decimal(row.get("costFrom", row.get("cost_from", row.get("lowerBound"))), "cost_from")
        raw_to = row.get("costTo", row.get("cost_to", row.get("upperBound")))
        cost_to = None if raw_to in (None, "") else as_decimal(raw_to, "cost_to")
        percent = as_decimal(row.get(percent_key), percent_key)
        if cost_to is not None and cost_from >= cost_to:
            raise ValueError("cost_from must be less than cost_to")
        if percent < 0:
            raise ValueError(f"{percent_key} must be >= 0")
        normalized.append(
            {
                "cost_from": cost_from,
                "cost_to": cost_to,
                percent_key: percent,
                "sort_order": int(row.get("sortOrder", row.get("sort_order", idx))),
            }
        )

    ordered = sorted(normalized, key=lambda x: (x["cost_from"], Decimal("999999999") if x["cost_to"] is None else x["cost_to"]))
    previous_to: Decimal | None = None
    for row in ordered:
        if previous_to is not None and row["cost_from"] < previous_to:
            raise ValueError("ranges must not overlap")
        if row["cost_to"] is None:
            previous_to = None
        else:
            previous_to = row["cost_to"]
    return ordered


def touch(row) -> None:
    row.updated_at = now_kz_naive()
