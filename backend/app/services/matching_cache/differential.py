"""Reusable, read-only differential validation for legacy and cache artifacts.

The comparator intentionally has no "close enough" business normalization.  It
only canonicalizes representation (Decimal/date/JSON ordering) and reports all
missing, extra and changed values under caller-supplied logical identities.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
import json
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import (
    CompetitorMatchSnapshotItem,
    CompetitorPriceListItem,
    CompetitorSourceVersion,
    CompetitorSourceVersionItem,
    Product,
)
from .capture import iter_legacy_source_rows


@dataclass(frozen=True)
class DifferentialMismatch:
    artifact: str
    identity: tuple[Any, ...]
    field: str
    legacy_value: Any
    shadow_value: Any


@dataclass(frozen=True)
class DifferentialReport:
    artifact: str
    legacy_count: int
    shadow_count: int
    mismatches: tuple[DifferentialMismatch, ...]

    @property
    def exact(self) -> bool:
        return not self.mismatches

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact": self.artifact,
            "legacyCount": self.legacy_count,
            "shadowCount": self.shadow_count,
            "exact": self.exact,
            "mismatches": [asdict(row) for row in self.mismatches],
        }

    def assert_exact(self) -> None:
        if self.exact:
            return
        detail = json.dumps(self.as_dict(), ensure_ascii=False, default=str, indent=2)
        raise AssertionError(f"{self.artifact} differential mismatch:\n{detail}")


def _canonical(value: Any) -> Any:
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        # NUMERIC values can be Decimal after a PostgreSQL reload but remain
        # float/int in an ORM identity map immediately after matching.  Use
        # the decimal text representation so only storage representation is
        # ignored; any actual numeric value change remains a mismatch.
        return format(Decimal(str(value)).normalize(), "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _canonical(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_canonical(item) for item in value)
    return value


def compare_records(
    *,
    artifact: str,
    legacy: Iterable[Mapping[str, Any]],
    shadow: Iterable[Mapping[str, Any]],
    identity_fields: Sequence[str],
    compare_fields: Sequence[str] | None = None,
) -> DifferentialReport:
    """Compare multisets without relying on database surrogate IDs.

    Repeated identities are paired in deterministic canonical record order, so
    duplicate multiplicity differences are explicit and never collapsed.
    """

    legacy_rows = [dict(row) for row in legacy]
    shadow_rows = [dict(row) for row in shadow]
    fields = tuple(compare_fields or sorted(
        (set().union(*(row.keys() for row in legacy_rows + shadow_rows)))
        - set(identity_fields)
    ))

    def identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
        return tuple(_canonical(row.get(field)) for field in identity_fields)

    def record_key(row: Mapping[str, Any]) -> str:
        payload = {field: _canonical(row.get(field)) for field in fields}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    left: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    right: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in legacy_rows:
        left[identity(row)].append(row)
    for row in shadow_rows:
        right[identity(row)].append(row)

    mismatches: list[DifferentialMismatch] = []
    for key in sorted(set(left) | set(right), key=repr):
        legacy_group = sorted(left.get(key, []), key=record_key)
        shadow_group = sorted(right.get(key, []), key=record_key)
        if len(legacy_group) != len(shadow_group):
            mismatches.append(DifferentialMismatch(
                artifact, key, "__multiplicity__", len(legacy_group), len(shadow_group)
            ))
        for occurrence, (legacy_row, shadow_row) in enumerate(
            zip(legacy_group, shadow_group)
        ):
            occurrence_key = key + (occurrence,)
            for field in fields:
                legacy_value = _canonical(legacy_row.get(field))
                shadow_value = _canonical(shadow_row.get(field))
                if legacy_value != shadow_value:
                    mismatches.append(DifferentialMismatch(
                        artifact, occurrence_key, field, legacy_value, shadow_value
                    ))
    return DifferentialReport(
        artifact=artifact,
        legacy_count=len(legacy_rows),
        shadow_count=len(shadow_rows),
        mismatches=tuple(mismatches),
    )

def compare_legacy_and_shadow_matches(
    *, db: Session, competitor_price_list_id: int, snapshot_id: int
) -> DifferentialReport:
    """Compare current legacy decisions with a snapshot by logical source row."""

    product_codes = dict(db.execute(select(Product.id, Product.code)).all())
    legacy_items = {
        int(item.id): item
        for item in db.scalars(
            select(CompetitorPriceListItem).where(
                CompetitorPriceListItem.price_list_id == int(competitor_price_list_id)
            )
        )
    }
    legacy: list[dict[str, Any]] = []
    for row in iter_legacy_source_rows(
        db=db, competitor_price_list_id=int(competitor_price_list_id)
    ):
        item = legacy_items[int(row.values["legacy_source_item_id"])]
        legacy.append({
            "sourceRowKey": row.source_row_key,
            "duplicateOrdinal": row.duplicate_ordinal,
            "productCode": product_codes.get(item.product_id),
            "matchType": str(item.match_type or "unmatched"),
            "matchScore": item.match_score,
            "matchKey": str(item.match_key or ""),
            "matchedSku": str(item.matched_sku or ""),
        })

    shadow = [
        {
            "sourceRowKey": row.source_row_key,
            "duplicateOrdinal": row.duplicate_ordinal,
            "productCode": product_codes.get(decision.product_id),
            "matchType": decision.match_type,
            "matchScore": decision.match_score,
            "matchKey": decision.match_key,
            "matchedSku": decision.matched_sku,
        }
        for decision, row in db.execute(
            select(CompetitorMatchSnapshotItem, CompetitorSourceVersionItem)
            .join(
                CompetitorSourceVersionItem,
                CompetitorSourceVersionItem.id
                == CompetitorMatchSnapshotItem.source_version_item_id,
            )
            .join(
                CompetitorSourceVersion,
                CompetitorSourceVersion.id == CompetitorSourceVersionItem.source_version_id,
            )
            .where(CompetitorMatchSnapshotItem.match_snapshot_id == int(snapshot_id))
            .where(
                CompetitorSourceVersion.competitor_price_list_id
                == int(competitor_price_list_id)
            )
        )
    ]
    return compare_records(
        artifact="matching_decisions",
        legacy=legacy,
        shadow=shadow,
        identity_fields=("sourceRowKey", "duplicateOrdinal"),
        compare_fields=("productCode", "matchType", "matchScore", "matchKey", "matchedSku"),
    )
