from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterable

from sqlalchemy import delete, distinct, func, select
from sqlalchemy.orm import Session

from backend.app.models import (
    VidmanCanonicalProduct,
    VidmanNormalizedItem,
    VidmanRawCanonicalLink,
    VidmanRawItem,
)
from backend.app.timezone import now_kz_naive


DEFAULT_BATCH_SIZE = 1000

_SPACE_RE = re.compile(r"\s+")
_PACK_RE = re.compile(r"(?:[n№]\s*|\bno\s*)(\d{1,5})(?=\b|[a-zа-я])", re.IGNORECASE)
_PACK_BY_FORM_RE = re.compile(
    r"(?<![\w.,])(\d{1,5})\s*(табл?|таблетк[аи]?|капс(?:ул[аы]?)?|амп(?:ул[аы]?)?|фл(?:акон[аы]?)?|саше|стик(?:ов)?)\b",
    re.IGNORECASE,
)
_NUMBER_RE = r"(?P<value>\d+(?:[\.,]\d+)?)"
_PLAIN_NUMBER_RE = r"\d+(?:[\.,]\d+)?"
_UNIT_TOKEN_RE = r"мкг|mcg|µg|μg|мг|mg|миллиграмм(?:а|ов)?|ме|me|iu|ед|гр|г|g|мл|ml"
_UNIT_RE = re.compile(
    rf"{_NUMBER_RE}\s*(?P<unit>{_UNIT_TOKEN_RE})(?=\b|[n№])",
    re.IGNORECASE,
)
_RATIO_RE = re.compile(
    rf"(?P<num>{_PLAIN_NUMBER_RE})\s*(?P<num_unit>{_UNIT_TOKEN_RE})?\s*[/-]\s*(?P<den>{_PLAIN_NUMBER_RE})?\s*(?P<den_unit>{_UNIT_TOKEN_RE}|доз[ау]?)\b",
    re.IGNORECASE,
)
_MULTI_COMPONENT_RE = re.compile(
    rf"{_PLAIN_NUMBER_RE}\s*(?:мкг|mcg|µg|μg|мг|mg|ме|me|iu|ед)\s*(?:\+\s*{_PLAIN_NUMBER_RE}\s*(?:мкг|mcg|µg|μg|мг|mg|ме|me|iu|ед))+",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(rf"{_NUMBER_RE}\s*%", re.IGNORECASE)
_STANDALONE_NUMBER_RE = re.compile(r"(?<![\w.,])(\d+(?:[\.,]\d+)?)(?![\w.,])")
_SAFE_UNITLESS_STRENGTH_RE = re.compile(r"\bl[\s-]*тироксин\s+(\d+(?:[\.,]\d+)?)\b", re.IGNORECASE)

_FORM_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("eye_drops", ("глазные капли", "капли глазные", "гл. капли", "гл.капли", "гл капли", "гл кап")),
    ("ear_drops", ("ушные капли", "капли ушные")),
    ("nasal_drops", ("назальные капли", "капли назальные", "капли наз")),
    ("drops", ("капли", "кап.")),
    ("tablet", ("таблетки", "табл.", "табл", "таб.", "таб")),
    ("capsule", ("капсулы", "капсул", "капс.", "капс")),
    ("ampoule", ("ампулы", "ампул", "амп.", "амп")),
    ("vial", ("флакон", "флак", "фл.", "фл")),
    ("lozenge", ("пастилки", "паст.", "паст")),
    ("aerosol", ("аэрозоль",)),
    ("ointment", ("мазь",)),
    ("cream", ("крем",)),
    ("gel", ("гель",)),
    ("syrup", ("сироп",)),
    ("suspension", ("суспензия", "сусп.")),
    ("suppository", ("суппозитории", "супп.", "супп", "свечи")),
    ("solution", ("раствор", "р-р", "р/р")),
    ("spray", ("спрей",)),
    ("lyophilizate", ("лиофилизат", "лиоф." , "лиоф")),
    ("granules", ("гранулы", "гран.", "гран")),
    ("powder", ("порошок",)),
)

_MANUFACTURER_ALIASES = {
    "berlin chemie": "берлин хеми",
    "berlin-chemie": "берлин хеми",
    "берлин-хеми": "берлин хеми",
    "берлин хеми": "берлин хеми",
}


@dataclass(frozen=True)
class ParsedVidmanItem:
    normalized_name: str
    normalized_manufacturer: str
    base_name: str
    dosage_value: Decimal | None = None
    dosage_unit: str = ""
    concentration_value: Decimal | None = None
    concentration_unit: str = ""
    volume_value: Decimal | None = None
    volume_unit: str = ""
    weight_value: Decimal | None = None
    weight_unit: str = ""
    pack_count: int | None = None
    dosage_form: str = ""
    variant_text: str = ""
    identity_tokens: tuple[str, ...] = ()
    normalized_signature: str = ""
    parse_confidence: Decimal = Decimal("0")
    parse_warnings: list[str] = field(default_factory=list)


@dataclass
class VidmanNormalizationSummary:
    raw_rows_total: int = 0
    processed: int = 0
    normalized: int = 0
    auto_linked: int = 0
    new_canonical_products: int = 0
    existing_canonical_reused: int = 0
    ambiguous: int = 0
    warnings: int = 0
    errors: int = 0
    unresolved: int = 0
    elapsed_seconds: float = 0
    rows_per_sec: float = 0
    analytics: dict[str, object] = field(default_factory=dict)


def normalize_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").lower().strip()
    text = text.replace("ё", "е").replace("№", " № ")
    text = re.sub(r"[‐‑‒–—―]+", "-", text)
    text = re.sub(r"(\d),(?=\d)", r"\1.", text)
    text = re.sub(r"[,;]+", " ", text)
    text = re.sub(r"\s*([%/()+-])\s*", r"\1", text)
    text = re.sub(
        rf"({_PLAIN_NUMBER_RE})\s*(РјРєРі|mcg|Вµg|Ојg|РјРі|mg|РјРµ|me|iu|РµРґ)\s*-?\s*({_PLAIN_NUMBER_RE})\s*(РјР»|ml)\b",
        r"\1 \2/\3 \4",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(\d)\s*(мкг|mcg|µg|μg|мг|mg|миллиграмм(?:а|ов)?|гр|г|g|мл|ml|%)\b",
        r"\1 \2",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^([a-zа-я])\s+([\wа-я])", r"\1-\2", text)
    return _SPACE_RE.sub(" ", text).strip()


def _normalize_identity_text(value: str | None) -> str:
    text = normalize_text(value)
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"\s*\+\s*", "+", text)
    text = re.sub(r"(\d)\s+(mg|mcg|iu|g|ml)\b", r"\1\2", text)
    text = re.sub(r"\s+-\s+", "-", text)
    text = re.sub(r"/{2,}", "/", text)
    text = re.sub(r"-{2,}", "-", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r",{2,}", ",", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[\s/.,;:]+", "", text)
    text = re.sub(r"[\s/.,;:-]+$", "", text)
    return text.strip()


def normalize_manufacturer(value: str | None) -> str:
    text = _normalize_identity_text(value)
    text = text.replace("-", " ")
    text = _SPACE_RE.sub(" ", text).strip()
    return _MANUFACTURER_ALIASES.get(text, text)


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(",", "."))
    except (InvalidOperation, AttributeError):
        return None


def _decimal_token(value: Decimal | None, unit: str = "") -> str:
    if value is None:
        return ""
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        formatted = format(normalized.quantize(Decimal("1")), "f")
    else:
        formatted = format(normalized, "f").rstrip("0").rstrip(".")
    return f"{formatted}{unit}"


def _unit_alias(unit: str) -> str:
    token = normalize_text(unit)
    if token in {"мг", "mg"} or token.startswith("миллиграмм"):
        return "mg"
    if token in {"мкг", "mcg", "µg", "μg"}:
        return "mcg"
    if token in {"ме", "me", "iu", "ед"}:
        return "iu"
    if token in {"г", "гр", "g"}:
        return "g"
    if token in {"мл", "ml"}:
        return "ml"
    return token


def _normalize_ratio_unit(value: Decimal | None, numerator_unit: str, denominator: Decimal | None, denominator_unit: str) -> str:
    left = _decimal_token(value, _unit_alias(numerator_unit)) if numerator_unit else _decimal_token(value, "")
    denominator_alias = normalize_text(denominator_unit)
    if denominator_alias in {"доза", "дозу"}:
        right = "dose"
    else:
        right = _decimal_token(denominator, _unit_alias(denominator_alias))
    return f"{left}/{right}".strip("/")


def _component_token(value: Decimal | None, unit: str) -> str:
    return _decimal_token(value, _unit_alias(unit))


def _normalize_strength_components(expression: str) -> str:
    components = [
        _component_token(_decimal(match.group("value")), match.group("unit"))
        for match in _UNIT_RE.finditer(normalize_text(expression))
        if _unit_alias(match.group("unit")) in {"mg", "mcg", "iu"}
    ]
    components = [component for component in components if component]
    return f"strengths:{'+'.join(components)}" if len(components) >= 2 else ""


def _signature_has_token_prefix(parsed: ParsedVidmanItem, prefix: str) -> bool:
    return any(token.startswith(prefix) for token in parsed.identity_tokens)


def _find_governed_unitless_strength(normalized_name: str, pack_start: int | None) -> tuple[Decimal | None, tuple[int, int] | None]:
    match = _SAFE_UNITLESS_STRENGTH_RE.search(normalized_name)
    if match:
        return _decimal(match.group(1)), match.span(1)
    if pack_start is None:
        return None, None
    before_pack = normalized_name[:pack_start]
    matches = list(_STANDALONE_NUMBER_RE.finditer(before_pack))
    if not matches:
        return None, None
    candidate = matches[-1]
    tail = before_pack[candidate.end() :]
    if tail.strip(" -+/\\"):
        return None, None
    return _decimal(candidate.group(1)), candidate.span(1)


def _extract_form(text: str) -> tuple[str, list[tuple[int, int]]]:
    matches: list[tuple[int, int]] = []
    for form, aliases in _FORM_ALIASES:
        for alias in aliases:
            pattern = re.compile(rf"(?<![a-zа-я]){re.escape(alias)}(?![a-zа-я])", re.IGNORECASE)
            found = pattern.search(text)
            if found:
                matches.append(found.span())
                return form, matches
    return "", matches


def _remove_spans(text: str, spans: Iterable[tuple[int, int]]) -> str:
    pieces: list[str] = []
    cursor = 0
    for start, end in sorted(_merge_spans(spans)):
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return _SPACE_RE.sub(" ", "".join(pieces)).strip(" ,.;")


def _merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if start >= end:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _span_overlaps(span: tuple[int, int], spans: Iterable[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < other_end and end > other_start for other_start, other_end in spans)


def _build_signature(parsed: ParsedVidmanItem) -> str:
    if not parsed.base_name:
        return ""
    parts = [_normalize_identity_text(parsed.base_name)]
    structured_values = (
        (parsed.dosage_value, parsed.dosage_unit, "strengths:"),
        (parsed.concentration_value, parsed.concentration_unit, ""),
        (parsed.volume_value, parsed.volume_unit, "package_volume:"),
        (parsed.weight_value, parsed.weight_unit, "package_weight:"),
    )
    for value, unit, covered_by_prefix in structured_values:
        if covered_by_prefix and _signature_has_token_prefix(parsed, covered_by_prefix):
            continue
        token = _decimal_token(value, unit)
        if token:
            parts.append(token)
    parts.extend(_normalize_identity_text(token) for token in parsed.identity_tokens)
    if parsed.dosage_form:
        parts.append(parsed.dosage_form)
    if parsed.pack_count is not None:
        parts.append(f"pack{parsed.pack_count}")
    if parsed.normalized_manufacturer:
        parts.append(_normalize_identity_text(parsed.normalized_manufacturer))
    return "|".join(parts)


def parse_vidman_product(raw_name: str | None, raw_manufacturer: str | None = "") -> ParsedVidmanItem:
    normalized_name = normalize_text(raw_name)
    normalized_manufacturer = normalize_manufacturer(raw_manufacturer)
    warnings: list[str] = []
    remove_spans: list[tuple[int, int]] = []
    identity_tokens: list[str] = []

    pack_count: int | None = None
    pack_match = _PACK_RE.search(normalized_name)
    if pack_match:
        pack_count = int(pack_match.group(1))
        remove_spans.append(pack_match.span())
    else:
        pack_form_match = _PACK_BY_FORM_RE.search(normalized_name)
        if pack_form_match:
            pack_count = int(pack_form_match.group(1))
            remove_spans.append(pack_form_match.span(1))

    dosage_value: Decimal | None = None
    dosage_unit = ""
    concentration_value: Decimal | None = None
    concentration_unit = ""
    volume_value: Decimal | None = None
    volume_unit = ""
    weight_value: Decimal | None = None
    weight_unit = ""

    for match in _MULTI_COMPONENT_RE.finditer(normalized_name):
        expression = _normalize_strength_components(match.group(0))
        if expression:
            identity_tokens.append(expression)
        remove_spans.append(match.span())

    ratio_matches = list(_RATIO_RE.finditer(normalized_name))
    for match in ratio_matches:
        value = _decimal(match.group("num"))
        denominator = _decimal(match.group("den")) if match.group("den") else None
        numerator_unit = match.group("num_unit") or ""
        denominator_unit = match.group("den_unit") or ""
        if value is None:
            warnings.append("invalid_numeric_value")
            continue
        ratio_token = _normalize_ratio_unit(value, numerator_unit, denominator, denominator_unit)
        if ratio_token:
            if concentration_value is None:
                concentration_value = value
                concentration_unit = f"per_{ratio_token.split('/', 1)[1]}" if "/" in ratio_token else ratio_token
            identity_tokens.append(f"ratio:{ratio_token}")
        remove_spans.append(match.span())

    percent_match = _PERCENT_RE.search(normalized_name)
    if percent_match and not _span_overlaps(percent_match.span(), remove_spans):
        concentration_value = _decimal(percent_match.group("value"))
        concentration_unit = "percent"
        remove_spans.append(percent_match.span())

    unit_matches: list[tuple[re.Match[str], Decimal, str]] = []
    for match in _UNIT_RE.finditer(normalized_name):
        if _span_overlaps(match.span(), remove_spans):
            continue
        value = _decimal(match.group("value"))
        unit = _unit_alias(match.group("unit"))
        if value is None:
            warnings.append("invalid_numeric_value")
            continue
        unit_matches.append((match, value, unit))

    by_unit: dict[str, list[tuple[re.Match[str], Decimal]]] = {}
    for match, value, unit in unit_matches:
        by_unit.setdefault(unit, []).append((match, value))

    strength_components = [
        (match, value, unit)
        for match, value, unit in unit_matches
        if unit in {"mg", "mcg", "iu"}
    ]
    distinct_strength_tokens = [
        token
        for token in dict.fromkeys(_decimal_token(value, unit) for _, value, unit in strength_components)
        if token
    ]
    if len(distinct_strength_tokens) >= 2:
        first_match, first_value, first_unit = strength_components[0]
        dosage_value = first_value
        dosage_unit = first_unit
        identity_tokens.append(f"strengths:{'+'.join(distinct_strength_tokens)}")
        remove_spans.extend(match.span() for match, _, _ in strength_components)
    elif strength_components and dosage_value is None:
        first_match, first_value, first_unit = strength_components[0]
        dosage_value = first_value
        dosage_unit = first_unit
        remove_spans.append(first_match.span())

    for unit, target_name in (("ml", "package_volume"), ("g", "package_weight")):
        matches = by_unit.get(unit, [])
        if not matches:
            continue
        distinct_tokens = [
            token
            for token in dict.fromkeys(_decimal_token(value, unit) for _, value in matches)
            if token
        ]
        first_match, first_value = matches[0]
        if unit == "ml" and volume_value is None:
            volume_value = first_value
            volume_unit = unit
        elif unit == "g" and weight_value is None:
            weight_value = first_value
            weight_unit = unit
        if ratio_matches or len(distinct_tokens) >= 2:
            identity_tokens.append(f"{target_name}:{'+'.join(distinct_tokens)}")
        remove_spans.extend(match.span() for match, _ in matches)

    dosage_form, form_spans = _extract_form(normalized_name)
    remove_spans.extend(form_spans)

    unitless_value, unitless_span = _find_governed_unitless_strength(
        normalized_name,
        pack_match.start() if pack_match else None,
    )
    if dosage_value is None and unitless_value is not None and dosage_form in {"tablet", "capsule", "suppository", "lozenge"}:
        dosage_value = unitless_value
        dosage_unit = ""
        if unitless_span is not None:
            remove_spans.append(unitless_span)

    working = _remove_spans(normalized_name, remove_spans)
    working = re.sub(r"[()]", " ", working)
    if normalized_manufacturer:
        without_manufacturer = re.sub(rf"(?<!\w){re.escape(normalized_manufacturer)}(?!\w)", " ", working)
        if normalize_text(without_manufacturer):
            working = without_manufacturer
    working = normalize_text(working)

    standalone_numbers = [m for m in _STANDALONE_NUMBER_RE.finditer(working)]
    if standalone_numbers:
        warnings.append("unparsed_numeric_token")

    base_name = _normalize_identity_text(re.sub(r"[()]", " ", working))
    if not base_name:
        warnings.append("missing_base_name")

    variant_text = _normalize_identity_text(_remove_spans(normalized_name, remove_spans))
    confidence = Decimal("1.0")
    if "missing_base_name" in warnings:
        confidence -= Decimal("0.5")
    if any(w.startswith("ambiguous") for w in warnings):
        confidence -= Decimal("0.2")
    if any(w.startswith("duplicate") or w.startswith("unparsed") for w in warnings):
        confidence -= Decimal("0.1")
    if confidence < 0:
        confidence = Decimal("0")

    parsed = ParsedVidmanItem(
        normalized_name=normalized_name,
        normalized_manufacturer=normalized_manufacturer,
        base_name=base_name,
        dosage_value=dosage_value,
        dosage_unit=dosage_unit,
        concentration_value=concentration_value,
        concentration_unit=concentration_unit,
        volume_value=volume_value,
        volume_unit=volume_unit,
        weight_value=weight_value,
        weight_unit=weight_unit,
        pack_count=pack_count,
        dosage_form=dosage_form,
        variant_text=variant_text,
        identity_tokens=tuple(dict.fromkeys(identity_tokens)),
        parse_confidence=confidence,
        parse_warnings=warnings,
    )
    return ParsedVidmanItem(**{**parsed.__dict__, "normalized_signature": _build_signature(parsed)})


def _apply_normalized_fields(item: VidmanNormalizedItem, parsed: ParsedVidmanItem) -> None:
    item.normalized_name = parsed.normalized_name
    item.normalized_manufacturer = parsed.normalized_manufacturer
    item.base_name = parsed.base_name
    item.dosage_value = parsed.dosage_value
    item.dosage_unit = parsed.dosage_unit
    item.concentration_value = parsed.concentration_value
    item.concentration_unit = parsed.concentration_unit
    item.volume_value = parsed.volume_value
    item.volume_unit = parsed.volume_unit
    item.weight_value = parsed.weight_value
    item.weight_unit = parsed.weight_unit
    item.pack_count = parsed.pack_count
    item.dosage_form = parsed.dosage_form
    item.variant_text = parsed.variant_text
    item.normalized_signature = parsed.normalized_signature
    item.parse_confidence = parsed.parse_confidence
    item.parse_warnings_json = json.dumps(parsed.parse_warnings, ensure_ascii=False)
    item.updated_at = now_kz_naive()


def _canonical_from_parsed(parsed: ParsedVidmanItem) -> VidmanCanonicalProduct:
    return VidmanCanonicalProduct(
        canonical_name=parsed.base_name,
        canonical_manufacturer=parsed.normalized_manufacturer,
        base_name=parsed.base_name,
        dosage_value=parsed.dosage_value,
        dosage_unit=parsed.dosage_unit,
        concentration_value=parsed.concentration_value,
        concentration_unit=parsed.concentration_unit,
        volume_value=parsed.volume_value,
        volume_unit=parsed.volume_unit,
        weight_value=parsed.weight_value,
        weight_unit=parsed.weight_unit,
        pack_count=parsed.pack_count,
        dosage_form=parsed.dosage_form,
        canonical_signature=parsed.normalized_signature,
        status="active",
    )


def rebuild_vidman_stage2(
    db: Session,
    *,
    account_id: int | None = None,
    main_id: int | None = None,
) -> None:
    raw_ids_query = select(VidmanRawItem.id)
    if account_id is not None:
        raw_ids_query = raw_ids_query.where(VidmanRawItem.account_id == account_id)
    if main_id is not None:
        raw_ids_query = raw_ids_query.where(VidmanRawItem.main_id == main_id)
    raw_ids = list(db.scalars(raw_ids_query))
    if not raw_ids:
        return

    db.execute(delete(VidmanRawCanonicalLink).where(VidmanRawCanonicalLink.raw_item_id.in_(raw_ids)))
    db.execute(delete(VidmanNormalizedItem).where(VidmanNormalizedItem.raw_item_id.in_(raw_ids)))
    db.flush()
    linked_canonical_ids = select(VidmanRawCanonicalLink.canonical_product_id)
    db.execute(delete(VidmanCanonicalProduct).where(VidmanCanonicalProduct.id.not_in(linked_canonical_ids)))
    db.flush()


def process_vidman_stage2(
    db: Session,
    *,
    account_id: int | None = None,
    main_id: int | None = None,
    limit: int | None = None,
    only_unprocessed: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    rebuild: bool = False,
    verbose: bool = False,
) -> VidmanNormalizationSummary:
    started = time.monotonic()
    summary = VidmanNormalizationSummary()
    if rebuild:
        rebuild_vidman_stage2(db, account_id=account_id, main_id=main_id)
        only_unprocessed = False

    base_query = select(VidmanRawItem.id)
    if account_id is not None:
        base_query = base_query.where(VidmanRawItem.account_id == account_id)
    if main_id is not None:
        base_query = base_query.where(VidmanRawItem.main_id == main_id)
    if only_unprocessed:
        base_query = base_query.outerjoin(
            VidmanNormalizedItem,
            VidmanNormalizedItem.raw_item_id == VidmanRawItem.id,
        ).where(VidmanNormalizedItem.id.is_(None))

    total_query = select(func.count()).select_from(base_query.subquery())
    summary.raw_rows_total = int(db.scalar(total_query) or 0)

    remaining = limit
    last_id = 0
    while remaining is None or remaining > 0:
        current_batch_size = batch_size if remaining is None else min(batch_size, remaining)
        ids_query = base_query.where(VidmanRawItem.id > last_id).order_by(VidmanRawItem.id).limit(current_batch_size)
        ids = list(db.scalars(ids_query))
        if not ids:
            break
        rows = list(db.scalars(select(VidmanRawItem).where(VidmanRawItem.id.in_(ids)).order_by(VidmanRawItem.id)))
        existing_normalized = {
            item.raw_item_id: item
            for item in db.scalars(select(VidmanNormalizedItem).where(VidmanNormalizedItem.raw_item_id.in_(ids)))
        }
        existing_links = {
            link.raw_item_id: link
            for link in db.scalars(select(VidmanRawCanonicalLink).where(VidmanRawCanonicalLink.raw_item_id.in_(ids)))
        }
        for raw in rows:
            try:
                parsed = parse_vidman_product(raw.raw_name, raw.raw_manufacturer)
                normalized = existing_normalized.get(raw.id)
                if normalized is None:
                    normalized = VidmanNormalizedItem(raw_item_id=raw.id)
                    db.add(normalized)
                    summary.normalized += 1
                _apply_normalized_fields(normalized, parsed)
                db.flush()

                if parsed.parse_warnings:
                    summary.warnings += len(parsed.parse_warnings)
                if not parsed.normalized_signature:
                    summary.ambiguous += 1
                    summary.unresolved += 1
                    continue

                canonical = db.scalar(
                    select(VidmanCanonicalProduct).where(
                        VidmanCanonicalProduct.canonical_signature == parsed.normalized_signature
                    )
                )
                if canonical is None:
                    canonical = _canonical_from_parsed(parsed)
                    db.add(canonical)
                    db.flush()
                    summary.new_canonical_products += 1
                    match_type = "exact_signature"
                else:
                    summary.existing_canonical_reused += 1
                    match_type = "exact_signature"

                link = existing_links.get(raw.id)
                if link is None:
                    link = VidmanRawCanonicalLink(raw_item_id=raw.id)
                    db.add(link)
                link.normalized_item_id = normalized.id
                link.canonical_product_id = canonical.id
                link.match_type = match_type
                link.confidence = parsed.parse_confidence
                link.is_auto_linked = True
                link.updated_at = now_kz_naive()
                summary.auto_linked += 1
                if verbose:
                    print(f"linked raw_id={raw.id} signature={parsed.normalized_signature}")
            except Exception as exc:  # pragma: no cover - defensive batch accounting
                summary.errors += 1
                print(f"ERROR raw_id={raw.id}: {exc}")
            finally:
                summary.processed += 1

        db.flush()
        if not dry_run:
            db.commit()
        last_id = max(ids)
        if remaining is not None:
            remaining -= len(ids)

    refresh_canonical_counts(db)
    if dry_run:
        db.rollback()
    else:
        db.commit()

    summary.analytics = calculate_vidman_stage2_analytics(db)
    summary.elapsed_seconds = time.monotonic() - started
    summary.rows_per_sec = summary.processed / summary.elapsed_seconds if summary.elapsed_seconds else 0
    return summary


def refresh_canonical_counts(db: Session) -> None:
    db.query(VidmanCanonicalProduct).update(
        {
            VidmanCanonicalProduct.raw_variants_count: 0,
            VidmanCanonicalProduct.accounts_count: 0,
            VidmanCanonicalProduct.plks_count: 0,
            VidmanCanonicalProduct.updated_at: now_kz_naive(),
        },
        synchronize_session=False,
    )
    rows = db.execute(
        select(
            VidmanRawCanonicalLink.canonical_product_id,
            func.count(VidmanRawItem.id),
            func.count(distinct(VidmanRawItem.account_id)),
            func.count(distinct(VidmanRawItem.main_id)),
        )
        .join(VidmanRawItem, VidmanRawItem.id == VidmanRawCanonicalLink.raw_item_id)
        .group_by(VidmanRawCanonicalLink.canonical_product_id)
    ).all()
    now = now_kz_naive()
    for canonical_id, raw_count, accounts_count, plks_count in rows:
        canonical = db.get(VidmanCanonicalProduct, canonical_id)
        if canonical is None:
            continue
        canonical.raw_variants_count = int(raw_count or 0)
        canonical.accounts_count = int(accounts_count or 0)
        canonical.plks_count = int(plks_count or 0)
        canonical.updated_at = now
    db.flush()


def calculate_vidman_stage2_analytics(db: Session) -> dict[str, object]:
    raw_count = int(db.scalar(select(func.count(VidmanRawItem.id))) or 0)
    normalized_count = int(db.scalar(select(func.count(VidmanNormalizedItem.id))) or 0)
    canonical_count = int(db.scalar(select(func.count(VidmanCanonicalProduct.id))) or 0)
    auto_linked = int(db.scalar(select(func.count(VidmanRawCanonicalLink.id))) or 0)
    unresolved = max(normalized_count - auto_linked, 0)
    warnings_count = int(
        db.scalar(
            select(func.count(VidmanNormalizedItem.id)).where(VidmanNormalizedItem.parse_warnings_json != "[]")
        )
        or 0
    )
    multi_account = int(
        db.scalar(select(func.count(VidmanCanonicalProduct.id)).where(VidmanCanonicalProduct.accounts_count >= 2)) or 0
    )
    one_account = int(
        db.scalar(select(func.count(VidmanCanonicalProduct.id)).where(VidmanCanonicalProduct.accounts_count == 1)) or 0
    )
    multi_plk = int(
        db.scalar(select(func.count(VidmanCanonicalProduct.id)).where(VidmanCanonicalProduct.plks_count >= 2)) or 0
    )
    conflicting_signatures = int(
        db.scalar(
            select(func.count()).select_from(
                select(VidmanNormalizedItem.normalized_signature)
                .where(VidmanNormalizedItem.normalized_signature != "")
                .group_by(VidmanNormalizedItem.normalized_signature)
                .having(func.count(distinct(VidmanNormalizedItem.base_name)) > 1)
                .subquery()
            )
        )
        or 0
    )
    top_repeated = [
        {
            "canonical_product_id": row.id,
            "signature": row.canonical_signature,
            "raw_variants_count": row.raw_variants_count,
            "accounts_count": row.accounts_count,
            "plks_count": row.plks_count,
        }
        for row in db.scalars(
            select(VidmanCanonicalProduct)
            .order_by(VidmanCanonicalProduct.raw_variants_count.desc(), VidmanCanonicalProduct.id)
            .limit(50)
        )
    ]
    unresolved_variants = [
        {"normalized_name": name, "count": count}
        for name, count in db.execute(
            select(VidmanNormalizedItem.normalized_name, func.count(VidmanNormalizedItem.id))
            .outerjoin(VidmanRawCanonicalLink, VidmanRawCanonicalLink.normalized_item_id == VidmanNormalizedItem.id)
            .where(VidmanRawCanonicalLink.id.is_(None))
            .group_by(VidmanNormalizedItem.normalized_name)
            .order_by(func.count(VidmanNormalizedItem.id).desc())
            .limit(50)
        )
    ]

    def coverage(column) -> dict[str, int | float]:
        parsed = int(db.scalar(select(func.count(VidmanNormalizedItem.id)).where(column.is_not(None))) or 0)
        return {"parsed": parsed, "total": normalized_count, "percent": round((parsed / normalized_count) * 100, 2) if normalized_count else 0}

    form_parsed = int(
        db.scalar(select(func.count(VidmanNormalizedItem.id)).where(VidmanNormalizedItem.dosage_form != "")) or 0
    )
    manufacturer_parsed = int(
        db.scalar(select(func.count(VidmanNormalizedItem.id)).where(VidmanNormalizedItem.normalized_manufacturer != ""))
        or 0
    )
    pack_parsed = int(
        db.scalar(select(func.count(VidmanNormalizedItem.id)).where(VidmanNormalizedItem.pack_count.is_not(None))) or 0
    )
    return {
        "raw_rows_count": raw_count,
        "normalized_rows_count": normalized_count,
        "canonical_products_count": canonical_count,
        "compression_ratio": round(raw_count / canonical_count, 4) if canonical_count else None,
        "canonical_products_one_account": one_account,
        "canonical_products_two_plus_accounts": multi_account,
        "products_multiple_plks": multi_plk,
        "conflicting_signatures": conflicting_signatures,
        "top_50_repeated_canonical_products": top_repeated,
        "top_50_unresolved_ambiguous_raw_variants": unresolved_variants,
        "parsing_coverage": {
            "dosage": coverage(VidmanNormalizedItem.dosage_value),
            "form": {"parsed": form_parsed, "total": normalized_count, "percent": round((form_parsed / normalized_count) * 100, 2) if normalized_count else 0},
            "pack": {"parsed": pack_parsed, "total": normalized_count, "percent": round((pack_parsed / normalized_count) * 100, 2) if normalized_count else 0},
            "manufacturer": {
                "parsed": manufacturer_parsed,
                "total": normalized_count,
                "percent": round((manufacturer_parsed / normalized_count) * 100, 2) if normalized_count else 0,
            },
        },
        "auto_linked": auto_linked,
        "unresolved": unresolved,
        "warnings": warnings_count,
    }
