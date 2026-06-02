from __future__ import annotations

import re


def normalize_external_sku(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value).strip())


def normalize_composite_sku(value: object) -> str | None:
    raw = normalize_external_sku(value)
    if not raw or "_" not in raw:
        return None
    parts = [p for p in raw.split("_") if p != ""]
    if len(parts) < 3:
        return None
    return "_".join(parts)


def normalize_sku(value: object, *, pad_to: int = 18) -> str | None:
    """Normalizes SKU across Excel/ph.center/Provisor.

    Rules:
    - accept int/float/str
    - if contains '_' keep part after last underscore
    - keep digits only
    - left-pad with zeros to `pad_to` (default: 18)
    - if longer than pad_to, keep last `pad_to`
    """

    if value is None:
        return None

    if isinstance(value, float):
        # Excel may store codes as floats; avoid scientific notation pitfalls
        if value != value:  # NaN
            return None
        s = str(int(value)) if value.is_integer() else str(value)
    else:
        s = str(value).strip()

    if not s:
        return None

    if "_" in s:
        s = s.split("_")[-1].strip()

    digits = re.sub(r"\D+", "", s)
    if not digits:
        return None

    if pad_to and len(digits) < pad_to:
        digits = digits.zfill(pad_to)
    elif pad_to and len(digits) > pad_to:
        digits = digits[-pad_to:]

    return digits


def normalize_sku_variants(value: object, *, pad_to: int = 18) -> list[str]:
    variants: list[str] = []

    def add(v: str | None) -> None:
        if v and v not in variants:
            variants.append(v)

    add(normalize_composite_sku(value))
    raw = normalize_external_sku(value)
    if raw:
        add(raw)
    add(normalize_sku(value, pad_to=pad_to))
    if value is None:
        return variants

    if "_" in raw:
        digits_all = re.sub(r"\D+", "", raw)
        if digits_all:
            if pad_to and len(digits_all) < pad_to:
                digits_all = digits_all.zfill(pad_to)
            elif pad_to and len(digits_all) > pad_to:
                digits_all = digits_all[-pad_to:]
            add(digits_all)

    return variants
