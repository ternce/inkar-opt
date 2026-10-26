from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import unicodedata
from typing import Any, Iterable, Mapping


FINGERPRINT_FORMAT_VERSION = "competitor-source-v2"

# Source identity must contain imported/source values only.  The legacy matcher
# writes its decisions back to the physical rows; hashing those outputs made an
# unchanged shared PLK look like new source content whenever another format ran.
SOURCE_CONTENT_FIELDS = (
    "provisor_id",
    "provisor_goods_id",
    "filial_id",
    "name",
    "reg_number",
    "distributor_goods_name",
    "distributor_goods_id",
    "distributor_price",
    "stock",
    "package_count",
    "expiry_date",
    "raw_name",
    "raw_manufacturer",
    "normalized_name",
    "normalized_manufacturer",
    "parsed_base_name",
    "parsed_form",
    "parsed_forms_json",
    "parsed_dosage",
    "parsed_dosage_volume",
    "parsed_quantity",
    "parsed_volume",
    "parsed_weight",
    "parsed_percent_strength",
    "parsed_concentration",
    "parsed_iu_dosage",
    "parsed_strength_signature",
    "parsed_dimensions_json",
    "parsed_critical_tokens_json",
    "raw_json",
)

# These fields are retained in immutable version rows for explicitly scoped
# existing-source backfills, but are outputs and never participate in source
# identity or duplicate identity.
MATCH_DECISION_FIELDS = (
    "authoritative_product_id",
    "authoritative_match_key",
    "authoritative_match_type",
    "authoritative_match_score",
    "authoritative_matched_sku",
)
PERSISTED_SOURCE_FIELDS = SOURCE_CONTENT_FIELDS + MATCH_DECISION_FIELDS

INTEGER_FIELDS = {
    "authoritative_product_id",
    "provisor_id",
    "provisor_goods_id",
    "filial_id",
    "parsed_quantity",
    "legacy_source_item_id",
}
NUMERIC_FIELDS = {
    "distributor_price",
    "stock",
    "package_count",
    "authoritative_match_score",
    "parsed_dosage",
    "parsed_dosage_volume",
    "parsed_volume",
    "parsed_weight",
    "parsed_percent_strength",
    "parsed_concentration",
    "parsed_iu_dosage",
}
JSON_FIELDS = {
    "parsed_forms_json",
    "parsed_dimensions_json",
    "parsed_critical_tokens_json",
    "raw_json",
}

# The snapshot schema names these fields explicitly as authoritative, while
# legacy competitor_price_list_items uses the shorter names. Accept both at
# the boundary so freezing a legacy row cannot silently omit a match decision.
LEGACY_FIELD_ALIASES = {
    "authoritative_product_id": "product_id",
    "authoritative_match_key": "match_key",
    "authoritative_match_type": "match_type",
    "authoritative_match_score": "match_score",
    "authoritative_matched_sku": "matched_sku",
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFC", str(value)).replace("\r\n", "\n").replace("\r", "\n")


def _integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid integer fingerprint value: {value!r}") from exc
    if not dec.is_finite() or dec != dec.to_integral_value():
        raise ValueError(f"invalid integer fingerprint value: {value!r}")
    return int(dec)


def _number(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numbers cannot be fingerprinted")
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid numeric fingerprint value: {value!r}") from exc
    if not dec.is_finite():
        raise ValueError("non-finite numbers cannot be fingerprinted")
    if dec == 0:
        return "0"
    normalized = format(dec.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def _canonical_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, Decimal):
        return {"$decimal": _number(value)}
    if isinstance(value, float):
        return {"$decimal": _number(value)}
    if isinstance(value, str):
        candidate = _text(value)
        try:
            return _canonical_json_value(json.loads(candidate))
        except (json.JSONDecodeError, TypeError):
            return candidate
    if isinstance(value, Mapping):
        return {
            _text(key): _canonical_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: _text(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    return _text(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(_canonical_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _field_value(field: str, value: Any) -> Any:
    if field in INTEGER_FIELDS:
        return _integer(value)
    if field in NUMERIC_FIELDS:
        return _number(value)
    if field in JSON_FIELDS:
        return _canonical_json_value(value)
    return _text(value)


def _source_value(source: Mapping[str, Any], field: str) -> Any:
    if field in source:
        return source.get(field)
    alias = LEGACY_FIELD_ALIASES.get(field)
    return source.get(alias) if alias else None


@dataclass(frozen=True)
class CanonicalSourceRow:
    source_row_key: str
    source_ordinal: int
    duplicate_ordinal: int
    row_sha256: str
    matching_input_sha256: str
    values: dict[str, Any]

    def persistence_mapping(self, source_version_id: int) -> dict[str, Any]:
        values = dict(self.values)
        for field in JSON_FIELDS:
            values[field] = _canonical_json(values.get(field))
        return {
            "source_version_id": source_version_id,
            "source_row_key": self.source_row_key,
            "source_ordinal": self.source_ordinal,
            "duplicate_ordinal": self.duplicate_ordinal,
            "row_sha256": self.row_sha256,
            "matching_input_sha256": self.matching_input_sha256,
            **values,
        }


@dataclass(frozen=True)
class CanonicalSourceContent:
    content_sha256: str
    matching_input_sha256: str
    rows: tuple[CanonicalSourceRow, ...]

    @property
    def row_count(self) -> int:
        return len(self.rows)


class CanonicalSourceHasher:
    """Incremental source digest for database-sized source generations."""

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._digest.update(
            (_canonical_json({"version": FINGERPRINT_FORMAT_VERSION}) + "\n").encode("utf-8")
        )
        self._row_count = 0

    def add(self, row: CanonicalSourceRow) -> None:
        envelope = {
            "sourceOrdinal": row.source_ordinal,
            "sourceRowKey": row.source_row_key,
            "duplicateOrdinal": row.duplicate_ordinal,
            "rowSha256": row.row_sha256,
        }
        self._digest.update((_canonical_json(envelope) + "\n").encode("utf-8"))
        self._row_count += 1

    def finish(self) -> tuple[str, int]:
        final = self._digest.copy()
        final.update((_canonical_json({"rowCount": self._row_count}) + "\n").encode("utf-8"))
        return final.hexdigest(), self._row_count


def _derived_source_row_key(values: Mapping[str, Any], row_sha256: str) -> str:
    identity = [
        values.get("filial_id"),
        values.get("provisor_id"),
        values.get("provisor_goods_id"),
        values.get("distributor_goods_id"),
        values.get("expiry_date"),
    ]
    if any(value not in (None, "") for value in identity):
        return "identity:" + _sha256(identity)
    return f"content:{row_sha256}"


def canonical_source_row(
    source: Mapping[str, Any],
    *,
    input_ordinal: int,
    occurrences: dict[str, int],
    include_match_decisions: bool = False,
) -> CanonicalSourceRow:
    identity_fields = SOURCE_CONTENT_FIELDS + (
        MATCH_DECISION_FIELDS if include_match_decisions else ()
    )
    identity_values = {
        field: _field_value(field, _source_value(source, field))
        for field in identity_fields
    }
    # Persist the old row id only as lineage. It is a database surrogate,
    # so including it in content identity would break identical-import reuse.
    row_sha256 = _sha256({"version": FINGERPRINT_FORMAT_VERSION, "values": identity_values})
    values = {
        field: _field_value(field, _source_value(source, field))
        for field in PERSISTED_SOURCE_FIELDS
    }
    values["legacy_source_item_id"] = _field_value(
        "legacy_source_item_id", source.get("legacy_source_item_id")
    )
    source_row_key = _text(source.get("source_row_key")) or _derived_source_row_key(values, row_sha256)
    source_ordinal = _integer(source.get("source_ordinal"))
    ordinal = input_ordinal if source_ordinal is None else source_ordinal
    requested_duplicate = _integer(source.get("duplicate_ordinal"))
    if requested_duplicate is None:
        duplicate_ordinal = occurrences.get(source_row_key, 0)
    else:
        duplicate_ordinal = requested_duplicate
    occurrences[source_row_key] = max(occurrences.get(source_row_key, 0), duplicate_ordinal + 1)
    return CanonicalSourceRow(
        source_row_key=source_row_key,
        source_ordinal=ordinal,
        duplicate_ordinal=duplicate_ordinal,
        row_sha256=row_sha256,
        matching_input_sha256=row_sha256,
        values=values,
    )


def canonical_source_content(
    rows: Iterable[Mapping[str, Any]], *, include_match_decisions: bool = False
) -> CanonicalSourceContent:
    prepared: list[tuple[int, str, str, dict[str, Any], int | None]] = []
    for input_ordinal, source in enumerate(rows):
        identity_fields = SOURCE_CONTENT_FIELDS + (
            MATCH_DECISION_FIELDS if include_match_decisions else ()
        )
        identity_values = {
            field: _field_value(field, _source_value(source, field))
            for field in identity_fields
        }
        row_sha256 = _sha256({"version": FINGERPRINT_FORMAT_VERSION, "values": identity_values})
        values = {
            field: _field_value(field, _source_value(source, field))
            for field in PERSISTED_SOURCE_FIELDS
        }
        values["legacy_source_item_id"] = _field_value(
            "legacy_source_item_id", source.get("legacy_source_item_id")
        )
        source_row_key = _text(source.get("source_row_key")) or _derived_source_row_key(values, row_sha256)
        source_ordinal = _integer(source.get("source_ordinal"))
        ordinal = input_ordinal if source_ordinal is None else source_ordinal
        requested_duplicate = _integer(source.get("duplicate_ordinal"))
        prepared.append((ordinal, source_row_key, row_sha256, values, requested_duplicate))

    prepared.sort(key=lambda item: (item[0], item[1], item[2]))
    ordinals = [item[0] for item in prepared]
    if len(ordinals) != len(set(ordinals)):
        raise ValueError("source_ordinal values must be unique")

    occurrences: dict[str, int] = {}
    canonical_rows: list[CanonicalSourceRow] = []
    used_identities: set[tuple[str, int]] = set()
    for ordinal, source_row_key, row_sha256, values, requested_duplicate in prepared:
        if requested_duplicate is None:
            duplicate_ordinal = occurrences.get(source_row_key, 0)
        else:
            duplicate_ordinal = requested_duplicate
        if duplicate_ordinal < 0:
            raise ValueError("duplicate_ordinal must not be negative")
        occurrences[source_row_key] = max(occurrences.get(source_row_key, 0), duplicate_ordinal + 1)
        identity = (source_row_key, duplicate_ordinal)
        if identity in used_identities:
            raise ValueError("source_row_key and duplicate_ordinal must be unique within a version")
        used_identities.add(identity)
        canonical_rows.append(
            CanonicalSourceRow(
                source_row_key=source_row_key,
                source_ordinal=ordinal,
                duplicate_ordinal=duplicate_ordinal,
                row_sha256=row_sha256,
                matching_input_sha256=row_sha256,
                values=values,
            )
        )

    hasher = CanonicalSourceHasher()
    for row in canonical_rows:
        hasher.add(row)
    content_sha256, _ = hasher.finish()
    # Deliberately conservative for Phase 2B.1.  Price and every persisted
    # normalized field remain matching dependencies.
    matching_input_sha256 = content_sha256
    return CanonicalSourceContent(
        content_sha256=content_sha256,
        matching_input_sha256=matching_input_sha256,
        rows=tuple(canonical_rows),
    )
