from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Iterable

from sqlalchemy import delete, func, inspect, select
from sqlalchemy.orm import Session

from ..models import (
    InternalProductNormalized,
    Product,
    ProductExtra,
    VidmanCanonicalProduct,
    VidmanProductMatch,
    VidmanProductMatchCandidate,
)
from .vidman_normalization import ParsedVidmanItem, normalize_manufacturer, normalize_text, parse_vidman_product
from ..timezone import now_kz_naive


AUTO_MATCHED = "AUTO_MATCHED"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
UNMATCHED = "UNMATCHED"
MANUALLY_APPROVED = "MANUALLY_APPROVED"
MANUAL_UNMATCHED = "MANUAL_UNMATCHED"
REJECTED = "REJECTED"

MANUAL_STATUSES = {MANUALLY_APPROVED, MANUAL_UNMATCHED, REJECTED}
AUTO_STATUSES = {AUTO_MATCHED, REVIEW_REQUIRED, UNMATCHED}

_DOSAGE_LIKE_FORMS = {"tablet", "capsule", "suppository", "lozenge"}
_PRESENTATION_TOKENS_BY_FORM = {
    "tablet": {"таб", "табл", "таблетки", "п.п.о", "п.о", "жев", "chew", "zhev"},
    "capsule": {"капс", "капсул"},
    "suppository": {"рект", "ваг", "свечи", "супп"},
    "spray": {"наз", "назальный"},
    "nasal_drops": {"наз"},
    "eye_drops": {"гл", "глаз"},
}
_LEGAL_MANUFACTURER_SUFFIXES = {
    "ао",
    "оао",
    "зао",
    "тоо",
    "ооо",
    "ип",
    "ao",
    "jsc",
    "llc",
    "ltd",
    "limited",
    "inc",
    "corp",
    "corporation",
    "gmbh",
    "ag",
    "srl",
    "d.d",
    "dd",
}
_LOCATION_MANUFACTURER_SUFFIXES = {
    "россия",
    "рос",
    "казахстан",
    "каз",
    "украина",
    "словения",
    "novo",
    "mesto",
}


@dataclass(frozen=True)
class ProductIdentity:
    product_id: int
    code: str
    name: str
    manufacturer: str
    parsed: ParsedVidmanItem


@dataclass(frozen=True)
class MatchDecision:
    status: str
    product_id: int | None = None
    match_type: str = ""
    confidence: Decimal = Decimal("0")
    candidate_count: int = 0
    matched_by: str = "vidman_stage3"
    evidence: dict[str, object] = field(default_factory=dict)
    candidates: tuple[tuple[ProductIdentity, Decimal, str, dict[str, object]], ...] = ()


@dataclass
class VidmanMatchSummary:
    total_canonical: int = 0
    processed: int = 0
    stable_id_match: int = 0
    exact_structural_match: int = 0
    exact_name_structural_match: int = 0
    review_required: int = 0
    unmatched: int = 0
    manual_preserved: int = 0
    written_matches: int = 0
    written_candidates: int = 0
    hard_conflict_count: int = 0
    multiple_internal_candidates: int = 0
    no_candidate_count: int = 0
    elapsed_seconds: float = 0
    rows_per_sec: float = 0

    @property
    def auto_matchable(self) -> int:
        return self.stable_id_match + self.exact_structural_match + self.exact_name_structural_match

    def analytics(self) -> dict[str, object]:
        total = self.processed or self.total_canonical or 0

        def pct(value: int) -> float:
            return round((value / total) * 100, 2) if total else 0

        return {
            "TOTAL_CANONICAL": self.total_canonical,
            "PROCESSED": self.processed,
            "STABLE_ID_MATCH": self.stable_id_match,
            "EXACT_STRUCTURAL_MATCH": self.exact_structural_match,
            "EXACT_NAME_STRUCTURAL_MATCH": self.exact_name_structural_match,
            "REVIEW_REQUIRED": self.review_required,
            "UNMATCHED": self.unmatched,
            "MANUAL_PRESERVED": self.manual_preserved,
            "AUTO_MATCHABLE_PERCENT": pct(self.auto_matchable),
            "REVIEW_PERCENT": pct(self.review_required),
            "UNMATCHED_PERCENT": pct(self.unmatched),
            "WRITTEN_MATCHES": self.written_matches,
            "WRITTEN_CANDIDATES": self.written_candidates,
            "HARD_CONFLICT_COUNT": self.hard_conflict_count,
            "MULTIPLE_INTERNAL_CANDIDATES": self.multiple_internal_candidates,
            "NO_CANDIDATE_COUNT": self.no_candidate_count,
            "ROWS_PER_SEC": round(self.rows_per_sec, 2),
        }


@dataclass
class ProductIndexes:
    products: list[ProductIdentity]
    by_signature: dict[str, list[ProductIdentity]]
    by_base_name: dict[str, list[ProductIdentity]]
    by_token: dict[str, list[ProductIdentity]]
    by_goods_id: dict[int, list[ProductIdentity]]


def _decimal_token(value: object | None) -> str:
    if value is None:
        return ""
    decimal = Decimal(str(value)).normalize()
    if decimal == decimal.to_integral_value():
        return format(decimal.quantize(Decimal("1")), "f")
    return format(decimal, "f").rstrip("0").rstrip(".")


def _same_decimal(left: object | None, right: object | None) -> bool:
    return _decimal_token(left) == _decimal_token(right)


def _tokens(value: str) -> set[str]:
    return {token for token in normalize_text(value).replace("|", " ").split() if len(token) >= 3}


def _manufacturer_core(value: str | None) -> str:
    text = normalize_manufacturer(value)
    text = re.sub(r"[()\"'`]+", " ", text)
    text = re.sub(r"[./,-]+", " ", text)
    tokens = [token for token in text.split() if token]
    changed = True
    while changed:
        changed = False
        while tokens and tokens[-1] in _LEGAL_MANUFACTURER_SUFFIXES | _LOCATION_MANUFACTURER_SUFFIXES:
            tokens.pop()
            changed = True
        if len(tokens) >= 2 and tokens[-2:] == ["d", "d"]:
            tokens = tokens[:-2]
            changed = True
    return " ".join(tokens)


def _manufacturer_alias_exact(left: str, right: str) -> bool:
    normalized_left = normalize_manufacturer(left)
    normalized_right = normalize_manufacturer(right)
    if not normalized_left or not normalized_right or normalized_left == normalized_right:
        return False
    left_core = _manufacturer_core(normalized_left)
    right_core = _manufacturer_core(normalized_right)
    return bool(left_core and right_core and left_core == right_core)


def _signature_without_manufacturer(parsed: ParsedVidmanItem) -> str:
    parts = parsed.normalized_signature.split("|") if parsed.normalized_signature else []
    if parsed.normalized_manufacturer and parts and parts[-1] == parsed.normalized_manufacturer:
        parts = parts[:-1]
    return "|".join(parts)


def _canonical_signature_without_manufacturer(canonical: VidmanCanonicalProduct) -> str:
    parts = canonical.canonical_signature.split("|") if canonical.canonical_signature else []
    if canonical.canonical_manufacturer and parts and parts[-1] == canonical.canonical_manufacturer:
        parts = parts[:-1]
    return "|".join(parts)


def _manufacturer_compatible(left: str, right: str) -> bool:
    normalized_left = normalize_manufacturer(left)
    normalized_right = normalize_manufacturer(right)
    return (
        not normalized_left
        or not normalized_right
        or normalized_left == normalized_right
        or _manufacturer_alias_exact(normalized_left, normalized_right)
    )


def _manufacturer_exact(left: str, right: str) -> bool:
    normalized_left = normalize_manufacturer(left)
    normalized_right = normalize_manufacturer(right)
    return bool(normalized_left and normalized_right and normalized_left == normalized_right)


def _unit_compatible(left_unit: str, right_unit: str) -> bool:
    return not left_unit or not right_unit or left_unit == right_unit


def _effective_dosage(value: object | None, unit: str, weight: object | None, weight_unit: str, form: str) -> tuple[object | None, str, str]:
    if value is not None:
        return value, unit or "", "dosage"
    if form in _DOSAGE_LIKE_FORMS and weight is not None and (not weight_unit or weight_unit == "g"):
        return weight, weight_unit or "", "weight_as_dosage"
    return None, "", ""


def _presentation_clean_name(name: str, evidence: dict[str, object]) -> str:
    text = normalize_text(name).replace("#", " ")
    shared = set(evidence.get("shared_structural_fields") or [])
    if "dosage_form" not in shared:
        return re.sub(r"\s+", " ", text).strip(" .")
    form = str(evidence.get("shared_dosage_form") or "")
    governed = set(_PRESENTATION_TOKENS_BY_FORM.get(form, set()))
    if not governed:
        return re.sub(r"\s+", " ", text).strip(" .")
    tokens = []
    for token in re.sub(r"[./]+", " ", text).split():
        cleaned = token.strip(" .")
        if cleaned in governed:
            continue
        tokens.append(cleaned)
    return re.sub(r"\s+", " ", " ".join(tokens)).strip(" .")


def _structural_conflicts(canonical: VidmanCanonicalProduct, product: ProductIdentity) -> list[str]:
    parsed = product.parsed
    conflicts: list[str] = []
    canonical_dosage, canonical_dosage_unit, _canonical_dosage_source = _effective_dosage(
        canonical.dosage_value,
        canonical.dosage_unit or "",
        canonical.weight_value,
        canonical.weight_unit or "",
        canonical.dosage_form or parsed.dosage_form or "",
    )
    product_dosage, product_dosage_unit, _product_dosage_source = _effective_dosage(
        parsed.dosage_value,
        parsed.dosage_unit or "",
        parsed.weight_value,
        parsed.weight_unit or "",
        parsed.dosage_form or canonical.dosage_form or "",
    )
    checks = (
        ("dosage", canonical_dosage, product_dosage),
        ("concentration", canonical.concentration_value, parsed.concentration_value),
        ("volume", canonical.volume_value, parsed.volume_value),
    )
    for label, left, right in checks:
        if left is not None and right is not None and not _same_decimal(left, right):
            conflicts.append(label)
    canonical_weight_as_dosage = canonical_dosage is not None and canonical.dosage_value is None and canonical.weight_value is not None
    product_weight_as_dosage = product_dosage is not None and parsed.dosage_value is None and parsed.weight_value is not None
    if (
        canonical.weight_value is not None
        and parsed.weight_value is not None
        and not canonical_weight_as_dosage
        and not product_weight_as_dosage
        and not _same_decimal(canonical.weight_value, parsed.weight_value)
    ):
        conflicts.append("weight")

    unit_checks = (
        ("dosage_unit", canonical_dosage_unit, product_dosage_unit),
        ("concentration_unit", canonical.concentration_unit, parsed.concentration_unit),
        ("volume_unit", canonical.volume_unit, parsed.volume_unit),
        ("dosage_form", canonical.dosage_form, parsed.dosage_form),
    )
    for label, left, right in unit_checks:
        if left and right and not _unit_compatible(left, right):
            conflicts.append(label)
    if (
        canonical.weight_value is not None
        and parsed.weight_value is not None
        and not canonical_weight_as_dosage
        and not product_weight_as_dosage
        and canonical.weight_unit
        and parsed.weight_unit
        and canonical.weight_unit != parsed.weight_unit
    ):
        conflicts.append("weight_unit")

    if canonical.pack_count is not None and parsed.pack_count is not None and canonical.pack_count != parsed.pack_count:
        conflicts.append("pack_count")

    canonical_tokens = _canonical_identity_tokens(canonical)
    product_tokens = set(parsed.identity_tokens)
    for prefix in ("strengths:", "ratio:", "package_volume:", "package_weight:"):
        left = sorted(token for token in canonical_tokens if token.startswith(prefix))
        right = sorted(token for token in product_tokens if token.startswith(prefix))
        if left and right and left != right:
            conflicts.append(prefix.rstrip(":"))

    if canonical.canonical_manufacturer and parsed.normalized_manufacturer and not _manufacturer_compatible(
        canonical.canonical_manufacturer, parsed.normalized_manufacturer
    ):
        conflicts.append("manufacturer")
    return sorted(set(conflicts))


def _signature_tokens(value: str) -> set[str]:
    return {part for part in (value or "").split("|") if part}


def _canonical_identity_tokens(canonical: VidmanCanonicalProduct) -> set[str]:
    ordinary_parts = {
        canonical.base_name,
        canonical.dosage_form,
        canonical.canonical_manufacturer,
        _decimal_token(canonical.dosage_value) + (canonical.dosage_unit or "") if canonical.dosage_value is not None else "",
        _decimal_token(canonical.concentration_value) + (canonical.concentration_unit or "")
        if canonical.concentration_value is not None
        else "",
        _decimal_token(canonical.volume_value) + (canonical.volume_unit or "") if canonical.volume_value is not None else "",
        _decimal_token(canonical.weight_value) + (canonical.weight_unit or "") if canonical.weight_value is not None else "",
        f"pack{canonical.pack_count}" if canonical.pack_count is not None else "",
    }
    return {part for part in canonical.canonical_signature.split("|") if part and part not in ordinary_parts}


def _product_identity_tokens(product: ProductIdentity) -> set[str]:
    return set(product.parsed.identity_tokens)


def _meaningful_structural_presence(canonical: VidmanCanonicalProduct, product: ProductIdentity) -> dict[str, tuple[bool, bool, bool]]:
    parsed = product.parsed
    canonical_tokens = _canonical_identity_tokens(canonical)
    product_tokens = _product_identity_tokens(product)
    shared_form = canonical.dosage_form if canonical.dosage_form and canonical.dosage_form == parsed.dosage_form else ""
    canonical_dosage, canonical_dosage_unit, canonical_dosage_source = _effective_dosage(
        canonical.dosage_value,
        canonical.dosage_unit or "",
        canonical.weight_value,
        canonical.weight_unit or "",
        canonical.dosage_form or parsed.dosage_form or "",
    )
    product_dosage, product_dosage_unit, product_dosage_source = _effective_dosage(
        parsed.dosage_value,
        parsed.dosage_unit or "",
        parsed.weight_value,
        parsed.weight_unit or "",
        parsed.dosage_form or canonical.dosage_form or "",
    )

    def decimal_field(left_value: object | None, left_unit: str, right_value: object | None, right_unit: str) -> tuple[bool, bool, bool]:
        left_known = left_value is not None
        right_known = right_value is not None
        matches = bool(left_known and right_known and _same_decimal(left_value, right_value) and _unit_compatible(left_unit, right_unit))
        return left_known, right_known, matches

    def token_field(prefix: str) -> tuple[bool, bool, bool]:
        left = {token for token in canonical_tokens if token.startswith(prefix)}
        right = {token for token in product_tokens if token.startswith(prefix)}
        return bool(left), bool(right), bool(left and right and left == right)

    fields = {
        "dosage": decimal_field(canonical_dosage, canonical_dosage_unit, product_dosage, product_dosage_unit),
        "pack_count": (
            canonical.pack_count is not None,
            parsed.pack_count is not None,
            bool(canonical.pack_count is not None and parsed.pack_count is not None and canonical.pack_count == parsed.pack_count),
        ),
        "concentration": decimal_field(
            canonical.concentration_value,
            canonical.concentration_unit or "",
            parsed.concentration_value,
            parsed.concentration_unit or "",
        ),
        "volume": decimal_field(canonical.volume_value, canonical.volume_unit or "", parsed.volume_value, parsed.volume_unit or ""),
        "weight": (
            canonical.weight_value is not None and canonical_dosage_source != "weight_as_dosage",
            parsed.weight_value is not None and product_dosage_source != "weight_as_dosage",
            bool(
                canonical.weight_value is not None
                and parsed.weight_value is not None
                and canonical_dosage_source != "weight_as_dosage"
                and product_dosage_source != "weight_as_dosage"
                and _same_decimal(canonical.weight_value, parsed.weight_value)
                and _unit_compatible(canonical.weight_unit or "", parsed.weight_unit or "")
            ),
        ),
        "dosage_form": (
            bool(canonical.dosage_form),
            bool(parsed.dosage_form),
            bool(canonical.dosage_form and parsed.dosage_form and canonical.dosage_form == parsed.dosage_form),
        ),
        "strengths": token_field("strengths:"),
        "package_volume": token_field("package_volume:"),
        "package_weight": token_field("package_weight:"),
        "ratio": token_field("ratio:"),
    }
    if shared_form:
        fields["shared_dosage_form"] = (True, True, True)
    return fields


def _structural_evidence(canonical: VidmanCanonicalProduct, product: ProductIdentity, conflicts: list[str]) -> dict[str, object]:
    presence = _meaningful_structural_presence(canonical, product)
    shared = sorted(name for name, (left, right, matches) in presence.items() if name != "shared_dosage_form" and left and right and matches)
    missing_on_internal = sorted(name for name, (left, right, _matches) in presence.items() if name != "shared_dosage_form" and left and not right)
    missing_on_vidman = sorted(name for name, (left, right, _matches) in presence.items() if name != "shared_dosage_form" and right and not left)
    return {
        "shared_structural_fields": shared,
        "shared_dosage_form": canonical.dosage_form if canonical.dosage_form and canonical.dosage_form == product.parsed.dosage_form else "",
        "missing_on_vidman": missing_on_vidman,
        "missing_on_internal": missing_on_internal,
        "hard_conflicts": conflicts,
    }


def _has_unshared_variant_identity(canonical: VidmanCanonicalProduct, product: ProductIdentity) -> bool:
    canonical_tokens = _canonical_identity_tokens(canonical)
    product_tokens = _product_identity_tokens(product)
    governed_prefixes = ("strengths:", "ratio:", "package_volume:", "package_weight:")
    canonical_variant_tokens = {token for token in canonical_tokens if not token.startswith(governed_prefixes)}
    product_variant_tokens = {token for token in product_tokens if not token.startswith(governed_prefixes)}
    return bool(canonical_variant_tokens ^ product_variant_tokens)


def _evidence(canonical: VidmanCanonicalProduct, product: ProductIdentity, conflicts: list[str]) -> dict[str, object]:
    parsed = product.parsed
    structural = _structural_evidence(canonical, product, conflicts)
    manufacturer_exact = _manufacturer_exact(canonical.canonical_manufacturer, parsed.normalized_manufacturer)
    manufacturer_alias_exact = _manufacturer_alias_exact(canonical.canonical_manufacturer, parsed.normalized_manufacturer)
    clean_canonical_name = _presentation_clean_name(canonical.base_name or "", structural)
    clean_product_name = _presentation_clean_name(parsed.base_name or "", structural)
    evidence = {
        "canonical_product_id": canonical.id,
        "product_id": product.product_id,
        "sku": product.code,
        "canonical_signature": canonical.canonical_signature,
        "internal_signature": parsed.normalized_signature,
        "exact_name": canonical.base_name == parsed.base_name,
        "comparison_name_exact": bool(clean_canonical_name and clean_product_name and clean_canonical_name == clean_product_name),
        "comparison_canonical_name": clean_canonical_name,
        "comparison_internal_name": clean_product_name,
        "exact_signature": canonical.canonical_signature == parsed.normalized_signature,
        "name_exact": canonical.base_name == parsed.base_name,
        "signature_exact": canonical.canonical_signature == parsed.normalized_signature,
        "signature_without_manufacturer_exact": _canonical_signature_without_manufacturer(canonical)
        == _signature_without_manufacturer(parsed),
        "manufacturer_exact": manufacturer_exact,
        "manufacturer_alias_exact": manufacturer_alias_exact,
        "manufacturer_conflict": bool(
            canonical.canonical_manufacturer
            and parsed.normalized_manufacturer
            and not _manufacturer_compatible(canonical.canonical_manufacturer, parsed.normalized_manufacturer)
        ),
        "manufacturer_match": manufacturer_exact,
        "manufacturer_compatible": _manufacturer_compatible(canonical.canonical_manufacturer, parsed.normalized_manufacturer),
        "dosage_match": "dosage" in structural["shared_structural_fields"] or canonical.dosage_value is None or parsed.dosage_value is None,
        "pack_match": canonical.pack_count is None or parsed.pack_count is None or canonical.pack_count == parsed.pack_count,
        "form_match": not canonical.dosage_form or not parsed.dosage_form or canonical.dosage_form == parsed.dosage_form,
        "volume_match": canonical.volume_value is None or parsed.volume_value is None or _same_decimal(canonical.volume_value, parsed.volume_value),
        "conflicts": conflicts,
        "unshared_variant_identity": _has_unshared_variant_identity(canonical, product),
    }
    evidence.update(structural)
    return evidence


def _has_strong_identity(canonical: VidmanCanonicalProduct, product: ProductIdentity) -> bool:
    conflicts = _structural_conflicts(canonical, product)
    evidence = _evidence(canonical, product, conflicts)
    return _exact_name_auto_confidence(evidence) is not None


def _exact_name_auto_confidence(evidence: dict[str, object]) -> Decimal | None:
    if evidence.get("hard_conflicts") or evidence.get("conflicts"):
        return None
    if not evidence.get("exact_name"):
        return None
    if not evidence.get("manufacturer_match"):
        return None
    if evidence.get("missing_on_internal"):
        return None
    if evidence.get("missing_on_vidman"):
        return None
    if evidence.get("unshared_variant_identity"):
        return None
    shared_count = len(evidence.get("shared_structural_fields") or [])
    if shared_count >= 3:
        return Decimal("98")
    if shared_count == 2:
        return Decimal("97")
    return None


def _parsed_from_internal(row: InternalProductNormalized) -> ParsedVidmanItem:
    try:
        identity_tokens = tuple(json.loads(row.identity_tokens_json or "[]"))
    except json.JSONDecodeError:
        identity_tokens = ()
    try:
        parse_warnings = list(json.loads(row.parse_warnings_json or "[]"))
    except json.JSONDecodeError:
        parse_warnings = ["invalid_internal_parse_warnings_json"]
    return ParsedVidmanItem(
        normalized_name=row.normalized_name or "",
        normalized_manufacturer=row.normalized_manufacturer or "",
        base_name=row.base_name or "",
        dosage_value=row.dosage_value,
        dosage_unit=row.dosage_unit or "",
        concentration_value=row.concentration_value,
        concentration_unit=row.concentration_unit or "",
        volume_value=row.volume_value,
        volume_unit=row.volume_unit or "",
        weight_value=row.weight_value,
        weight_unit=row.weight_unit or "",
        pack_count=row.pack_count,
        dosage_form=row.dosage_form or "",
        variant_text=row.variant_text or "",
        identity_tokens=identity_tokens,
        normalized_signature=row.normalized_signature or "",
        parse_confidence=row.parse_confidence,
        parse_warnings=parse_warnings,
    )


def _add_product_identity(indexes: ProductIndexes, identity: ProductIdentity, provisor_goods_id: int | None) -> None:
    indexes.products.append(identity)
    parsed = identity.parsed
    if parsed.normalized_signature:
        indexes.by_signature.setdefault(parsed.normalized_signature, []).append(identity)
    if parsed.base_name:
        indexes.by_base_name.setdefault(parsed.base_name, []).append(identity)
    if provisor_goods_id is not None:
        indexes.by_goods_id.setdefault(int(provisor_goods_id), []).append(identity)
    for token in _tokens(parsed.base_name):
        indexes.by_token.setdefault(token, []).append(identity)


def build_product_indexes(db: Session) -> ProductIndexes:
    indexes = ProductIndexes(products=[], by_signature={}, by_base_name={}, by_token={}, by_goods_id={})

    internal_normalized_count = 0
    if db.bind is not None and (
        db.bind.dialect.name == "sqlite" or inspect(db.bind).has_table("internal_product_normalized")
    ):
        internal_normalized_count = int(db.scalar(select(func.count(InternalProductNormalized.id))) or 0)
    if internal_normalized_count > 0:
        rows = db.execute(
            select(Product, InternalProductNormalized)
            .join(InternalProductNormalized, InternalProductNormalized.product_id == Product.id)
            .order_by(Product.id)
        ).all()
        for product, normalized in rows:
            parsed = _parsed_from_internal(normalized)
            identity = ProductIdentity(
                product_id=product.id,
                code=product.code or "",
                name=product.name or "",
                manufacturer=normalized.raw_manufacturer or "",
                parsed=parsed,
            )
            _add_product_identity(indexes, identity, product.provisor_goods_id)
        return indexes

    extras = {row.product_id: row.manufacturer for row in db.scalars(select(ProductExtra))}
    for product in db.scalars(select(Product).order_by(Product.id)):
        manufacturer = extras.get(product.id, "")
        parsed = parse_vidman_product(product.name, manufacturer)
        identity = ProductIdentity(
            product_id=product.id,
            code=product.code or "",
            name=product.name or "",
            manufacturer=manufacturer,
            parsed=parsed,
        )
        _add_product_identity(indexes, identity, product.provisor_goods_id)
    return indexes


def _candidate_products(canonical: VidmanCanonicalProduct, indexes: ProductIndexes, *, limit: int = 12) -> list[ProductIdentity]:
    seen: dict[int, ProductIdentity] = {}
    for product in indexes.by_base_name.get(canonical.base_name, []):
        seen[product.product_id] = product
    for token in _tokens(canonical.base_name):
        for product in indexes.by_token.get(token, [])[:100]:
            seen.setdefault(product.product_id, product)
    scored = [
        (
            SequenceMatcher(None, canonical.base_name, product.parsed.base_name).ratio()
            + (0.1 if _manufacturer_compatible(canonical.canonical_manufacturer, product.parsed.normalized_manufacturer) else 0),
            product,
        )
        for product in seen.values()
    ]
    scored.sort(key=lambda item: (-item[0], item[1].product_id))
    return [product for _score, product in scored[:limit]]


def decide_match(canonical: VidmanCanonicalProduct, indexes: ProductIndexes) -> MatchDecision:
    exact = indexes.by_signature.get(canonical.canonical_signature, [])
    if len(exact) == 1:
        product = exact[0]
        conflicts = _structural_conflicts(canonical, product)
        evidence = _evidence(canonical, product, conflicts)
        if not conflicts:
            return MatchDecision(
                status=AUTO_MATCHED,
                product_id=product.product_id,
                match_type="exact_structural_signature",
                confidence=Decimal("99"),
                candidate_count=1,
                evidence=evidence,
            )
    if len(exact) > 1:
        candidates = tuple((p, Decimal("96"), "duplicate_exact_structural_signature", _evidence(canonical, p, [])) for p in exact)
        return MatchDecision(
            status=REVIEW_REQUIRED,
            match_type="duplicate_exact_structural_signature",
            confidence=Decimal("96"),
            candidate_count=len(candidates),
            evidence={"reason": "multiple internal products share exact normalized identity"},
            candidates=candidates,
        )

    same_base = indexes.by_base_name.get(canonical.base_name, [])
    compatible: list[tuple[ProductIdentity, dict[str, object], Decimal]] = []
    exact_name_review_candidates: list[tuple[ProductIdentity, Decimal, str, dict[str, object]]] = []
    for product in same_base:
        conflicts = _structural_conflicts(canonical, product)
        evidence = _evidence(canonical, product, conflicts)
        confidence = _exact_name_auto_confidence(evidence)
        if confidence is not None:
            compatible.append((product, evidence, confidence))
        else:
            exact_name_review_candidates.append((product, Decimal("96"), "exact_name_insufficient_evidence", evidence))
    if len(compatible) == 1:
        product, evidence, confidence = compatible[0]
        return MatchDecision(
            status=AUTO_MATCHED,
            product_id=product.product_id,
            match_type="exact_name_structural_validation",
            confidence=confidence,
            candidate_count=1,
            evidence=evidence,
        )
    if len(compatible) > 1:
        candidates = tuple((p, Decimal("95"), "duplicate_exact_name_structural_validation", e) for p, e, _confidence in compatible)
        return MatchDecision(
            status=REVIEW_REQUIRED,
            match_type="duplicate_exact_name_structural_validation",
            confidence=Decimal("95"),
            candidate_count=len(candidates),
            evidence={"reason": "multiple compatible internal products share normalized name"},
            candidates=candidates,
        )
    if exact_name_review_candidates:
        return MatchDecision(
            status=REVIEW_REQUIRED,
            match_type="exact_name_insufficient_evidence",
            confidence=Decimal("96"),
            candidate_count=len(exact_name_review_candidates),
            evidence={"reason": "exact normalized name without enough shared structural evidence"},
            candidates=tuple(exact_name_review_candidates),
        )

    review_candidates: list[tuple[ProductIdentity, Decimal, str, dict[str, object]]] = []
    for product in _candidate_products(canonical, indexes):
        conflicts = _structural_conflicts(canonical, product)
        evidence = _evidence(canonical, product, conflicts)
        name_score = Decimal(str(round(SequenceMatcher(None, canonical.base_name, product.parsed.base_name).ratio() * 90, 2)))
        bonus = Decimal("5") if evidence["manufacturer_compatible"] else Decimal("0")
        score = min(Decimal("96"), name_score + bonus)
        review_candidates.append((product, score, "candidate_generation", evidence))
    review_candidates.sort(key=lambda item: (-item[1], item[0].product_id))
    if review_candidates:
        return MatchDecision(
            status=REVIEW_REQUIRED,
            match_type="candidate_generation",
            confidence=review_candidates[0][1],
            candidate_count=len(review_candidates),
            evidence={"reason": "no unique precision-safe auto match"},
            candidates=tuple(review_candidates),
        )
    return MatchDecision(
        status=UNMATCHED,
        match_type="no_candidate",
        confidence=Decimal("0"),
        candidate_count=0,
        evidence={"reason": "no candidate passed blocking keys"},
    )


def _json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _apply_decision(db: Session, canonical: VidmanCanonicalProduct, decision: MatchDecision, *, rebuild_auto: bool) -> tuple[int, int]:
    existing = db.scalar(
        select(VidmanProductMatch).where(VidmanProductMatch.canonical_product_id == canonical.id)
    )
    if existing is not None and existing.status in MANUAL_STATUSES:
        return 0, 0
    if existing is not None and existing.status == AUTO_MATCHED and not rebuild_auto:
        return 0, 0
    if existing is None:
        existing = VidmanProductMatch(canonical_product_id=canonical.id)
        db.add(existing)
    existing.product_id = decision.product_id
    existing.status = decision.status
    existing.match_type = decision.match_type
    existing.confidence = decision.confidence
    existing.candidate_count = decision.candidate_count
    existing.matched_by = decision.matched_by
    existing.evidence_json = _json(decision.evidence)
    existing.updated_at = now_kz_naive()

    db.execute(delete(VidmanProductMatchCandidate).where(VidmanProductMatchCandidate.canonical_product_id == canonical.id))
    candidates_written = 0
    for rank, (product, score, match_type, evidence) in enumerate(decision.candidates, start=1):
        db.add(
            VidmanProductMatchCandidate(
                canonical_product_id=canonical.id,
                product_id=product.product_id,
                rank=rank,
                score=score,
                match_type=match_type,
                evidence_json=_json(evidence),
            )
        )
        candidates_written += 1
    return 1, candidates_written


def process_vidman_product_matches(
    db: Session,
    *,
    limit: int | None = None,
    canonical_id: int | None = None,
    only_unmatched: bool = False,
    apply: bool = False,
    rebuild_auto: bool = False,
    verbose: bool = False,
) -> VidmanMatchSummary:
    started = time.monotonic()
    summary = VidmanMatchSummary()
    indexes = build_product_indexes(db)
    has_match_tables = bool(db.bind is not None and inspect(db.bind).has_table("vidman_product_matches"))

    query = select(VidmanCanonicalProduct).order_by(VidmanCanonicalProduct.id)
    if canonical_id is not None:
        query = query.where(VidmanCanonicalProduct.id == canonical_id)
    if only_unmatched:
        if has_match_tables:
            manual_or_present = select(VidmanProductMatch.canonical_product_id).where(
                VidmanProductMatch.status.in_([AUTO_MATCHED, REVIEW_REQUIRED, MANUALLY_APPROVED, REJECTED])
            )
            query = query.where(VidmanCanonicalProduct.id.not_in(manual_or_present))
    canonicals = list(db.scalars(query.limit(limit) if limit is not None else query))
    total_query = select(func.count(VidmanCanonicalProduct.id))
    if canonical_id is not None:
        total_query = total_query.where(VidmanCanonicalProduct.id == canonical_id)
    summary.total_canonical = int(db.scalar(total_query) or 0)

    existing_matches: dict[int, VidmanProductMatch] = {}
    if has_match_tables:
        existing_matches = {
            row.canonical_product_id: row
            for row in db.scalars(
                select(VidmanProductMatch).where(
                    VidmanProductMatch.canonical_product_id.in_([canonical.id for canonical in canonicals] or [-1])
                )
            )
        }

    for canonical in canonicals:
        existing = existing_matches.get(canonical.id)
        if existing is not None and existing.status in MANUAL_STATUSES:
            summary.manual_preserved += 1
            summary.processed += 1
            continue
        decision = decide_match(canonical, indexes)
        if decision.status == AUTO_MATCHED:
            if decision.match_type == "stable_identifier":
                summary.stable_id_match += 1
            elif decision.match_type == "exact_structural_signature":
                summary.exact_structural_match += 1
            else:
                summary.exact_name_structural_match += 1
        elif decision.status == REVIEW_REQUIRED:
            summary.review_required += 1
            if decision.match_type.startswith("duplicate_"):
                summary.multiple_internal_candidates += 1
        else:
            summary.unmatched += 1
            if decision.match_type == "no_candidate":
                summary.no_candidate_count += 1
        if decision.evidence.get("conflicts"):
            summary.hard_conflict_count += 1
        for _product, _score, _match_type, evidence in decision.candidates:
            if evidence.get("conflicts"):
                summary.hard_conflict_count += 1
                break
        if apply:
            written, candidates_written = _apply_decision(db, canonical, decision, rebuild_auto=rebuild_auto)
            summary.written_matches += written
            summary.written_candidates += candidates_written
        if verbose:
            print(
                f"canonical_id={canonical.id} status={decision.status} "
                f"product_id={decision.product_id} type={decision.match_type} confidence={decision.confidence}"
            )
        summary.processed += 1

    if apply:
        db.commit()
    else:
        db.rollback()
    summary.elapsed_seconds = time.monotonic() - started
    summary.rows_per_sec = summary.processed / summary.elapsed_seconds if summary.elapsed_seconds else 0
    return summary
