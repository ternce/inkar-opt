"""Validated cache reads and atomic physical competitor-price materialization."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import time
from typing import Any, Sequence

from sqlalchemy import delete, func, insert, literal, select, update, union_all
from sqlalchemy.orm import Session

from ...models import (
    CompetitorMatchSnapshot,
    CompetitorMatchSnapshotItem,
    CompetitorPrice,
    CompetitorSourcePublication,
    CompetitorSourcePublicationItem,
    CompetitorSourceVersion,
    CompetitorSourceVersionItem,
    PriceFormatCompetitorMaterialization,
)
from ...timezone import now_kz_naive
from ..competitor_coefficients import effective_price_coefficient
from ..pricing_workflow.locks import (
    PricingWorkflowCapacityExceeded,
    PricingWorkflowConflict,
)
from .dependencies import build_dependency_vector
from .context import MatchingContextConflict, build_source_dependency_vector


class MatchingCacheConflict(PricingWorkflowConflict):
    """A present cache artifact is stale, corrupt, or raced publication."""

    def __init__(self, reason: str):
        self.key = None
        RuntimeError.__init__(self, f"matching cache conflict: {reason}")


class MatchingCacheUnavailable(PricingWorkflowCapacityExceeded):
    """A required cache artifact is safely absent or still being built."""


@dataclass(frozen=True)
class ResolvedSourceSnapshot:
    source_id: int
    source_version_id: int
    source_publication_id: int
    snapshot_id: int
    source_row_count: int
    matched_row_count: int
    source_name: str
    supplier: str
    price_date: object
    coefficient: float
    legacy_stats: dict[str, Any]


@dataclass(frozen=True)
class CacheMaterializationResult:
    used_cache: bool
    reason: str
    read_mode: str
    source_version_ids: tuple[int, ...] = ()
    snapshot_ids: tuple[int, ...] = ()
    materialization_id: int | None = None
    materialization_fingerprint: str = ""
    materialization_reused: bool = False
    row_count: int = 0
    cache_lookup_ms: float = 0.0
    materialization_ms: float = 0.0
    source_stats: tuple[tuple[str, dict[str, Any]], ...] = ()

    def metadata(self, *, full_rematch_invocations: int, raw_source_rows_read: int) -> dict[str, Any]:
        return {
            "mode": self.read_mode,
            "hit": self.used_cache,
            "reason": self.reason,
            "sourceVersionIds": list(self.source_version_ids),
            "matchingSnapshotIds": list(self.snapshot_ids),
            "materializationId": self.materialization_id,
            "materializationFingerprint": self.materialization_fingerprint,
            "materializationReused": self.materialization_reused,
            "materializedRows": self.row_count,
            "cacheLookupDurationMs": round(self.cache_lookup_ms, 3),
            "materializationDurationMs": round(self.materialization_ms, 3),
            "fullRematchInvocationCount": int(full_rematch_invocations),
            "rawSourceRowsRead": int(raw_source_rows_read),
        }


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        _canonical(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _conflict(reason: str) -> None:
    raise MatchingCacheConflict(reason)


def _resolve_source_snapshot(
    *, db: Session, assigned_item: Any, vector: Any, expected_source_ids: tuple[int, ...]
) -> ResolvedSourceSnapshot | None:
    source = assigned_item.price_list
    source_id = int(source.id)
    version_exists = db.scalar(
        select(CompetitorSourceVersion.id)
        .where(CompetitorSourceVersion.competitor_price_list_id == source_id)
        .limit(1)
    )
    if version_exists is None:
        return None
    published_count = int(
        db.scalar(
            select(func.count(CompetitorSourceVersion.id)).where(
                CompetitorSourceVersion.competitor_price_list_id == source_id,
                CompetitorSourceVersion.status == "published",
            )
        )
        or 0
    )
    if published_count != 1:
        _conflict(f"source {source_id} publication is incomplete")
    version = db.scalar(
        select(CompetitorSourceVersion)
        .where(
            CompetitorSourceVersion.competitor_price_list_id == source_id,
            CompetitorSourceVersion.status == "published",
        )
        .limit(1)
    )
    if version is None:  # Defensive: the count and row lookup share this transaction.
        _conflict(f"source {source_id} publication raced cache resolution")
    if version.published_at is None or version.error:
        _conflict(f"source {source_id} published version metadata is invalid")

    publication_count = int(
        db.scalar(
            select(func.count(CompetitorSourcePublication.id)).where(
                CompetitorSourcePublication.competitor_price_list_id == source_id,
                CompetitorSourcePublication.status == "published",
            )
        )
        or 0
    )
    if publication_count != 1:
        _conflict(f"source {source_id} physical publication is incomplete")
    publication = db.scalar(
        select(CompetitorSourcePublication)
        .where(
            CompetitorSourcePublication.competitor_price_list_id == source_id,
            CompetitorSourcePublication.status == "published",
        )
        .limit(1)
    )
    if (
        publication is None
        or int(publication.source_version_id) != int(version.id)
        or int(publication.row_count) != int(version.row_count)
        or publication.published_at is None
        or publication.error
    ):
        _conflict(f"source {source_id} physical publication metadata is invalid")
    declared_rows = int(getattr(source, "items_count", 0) or 0)
    if declared_rows and declared_rows != int(version.row_count):
        _conflict(
            f"source {source_id} declared rows {declared_rows} != version rows {version.row_count}"
        )
    exact = db.scalar(
        select(CompetitorMatchSnapshot)
        .where(
            CompetitorMatchSnapshot.source_version_id == int(version.id),
            CompetitorMatchSnapshot.dependency_fingerprint == vector.fingerprint,
            CompetitorMatchSnapshot.matcher_version == vector.matcher_version,
            CompetitorMatchSnapshot.normalization_version == vector.normalization_version,
        )
        .order_by(CompetitorMatchSnapshot.id.desc())
        .limit(1)
    )
    if exact is None:
        current_snapshot_id = db.scalar(
            select(CompetitorMatchSnapshot.id)
            .where(CompetitorMatchSnapshot.source_version_id == int(version.id))
            .limit(1)
        )
        if current_snapshot_id is not None:
            dependency_mismatch = db.scalar(
                select(CompetitorMatchSnapshot.id)
                .where(
                    CompetitorMatchSnapshot.source_version_id == int(version.id),
                    CompetitorMatchSnapshot.dependency_fingerprint != vector.fingerprint,
                )
                .limit(1)
            )
            if dependency_mismatch is not None:
                _conflict(f"source {source_id} dependency revision mismatch")
            _conflict(f"source {source_id} matcher or normalization version mismatch")
        # A snapshot for an older immutable publication is never eligible for
        # reuse, but it does not make the current publication corrupt.  The
        # current version simply has no snapshot yet, so fallback mode may run
        # the locked legacy matcher and required mode reports unavailable.
        return None
    if exact.status in {"pending", "running"}:
        raise MatchingCacheUnavailable(
            f"matching cache snapshot {exact.id} is not ready (status={exact.status})"
        )
    if exact.status not in {"ready", "published"} or exact.ready_at is None or exact.error:
        _conflict(f"source {source_id} snapshot {exact.id} is not publishable")
    try:
        stored_vector = json.loads(exact.dependency_vector_json or "{}")
    except (TypeError, json.JSONDecodeError):
        _conflict(f"source {source_id} snapshot dependency vector is corrupt")
    if stored_vector != vector.as_dict():
        _conflict(f"source {source_id} snapshot dependency vector mismatch")
    if (
        int(exact.input_row_count) != int(version.row_count)
        or int(exact.matched_row_count) + int(exact.unmatched_row_count)
        != int(version.row_count)
    ):
        _conflict(f"source {source_id} snapshot {exact.id} is incomplete")
    try:
        stored_stats = json.loads(exact.stats_json or "{}")
    except (TypeError, json.JSONDecodeError):
        _conflict(f"source {source_id} snapshot statistics are corrupt")
    if not isinstance(stored_stats, dict):
        _conflict(f"source {source_id} snapshot statistics are invalid")
    matching_context = stored_stats.get("legacyMatchingContext")
    if not isinstance(matching_context, dict):
        _conflict(f"source {source_id} snapshot matching context is missing")
    try:
        stored_source_ids = tuple(
            sorted(int(value) for value in matching_context.get("sourceIds", ()))
        )
    except (TypeError, ValueError):
        _conflict(f"source {source_id} snapshot matching context is invalid")
    if stored_source_ids != expected_source_ids:
        _conflict(f"source {source_id} snapshot matching cohort mismatch")
    legacy_stats = stored_stats.get("legacyStats") or {}
    if not isinstance(legacy_stats, dict):
        _conflict(f"source {source_id} snapshot legacy statistics are invalid")
    return ResolvedSourceSnapshot(
        source_id=source_id,
        source_version_id=int(version.id),
        source_publication_id=int(publication.id),
        snapshot_id=int(exact.id),
        source_row_count=int(version.row_count),
        matched_row_count=int(exact.matched_row_count),
        source_name=f"{source.source_type}:{source.source_key}",
        supplier=source.supplier or source.display_name,
        price_date=source.price_date,
        coefficient=effective_price_coefficient(source),
        legacy_stats=dict(legacy_stats),
    )


def _materialization_payload(
    *, price_format_id: int, assigned: Sequence[Any], resolved: Sequence[ResolvedSourceSnapshot], vector: Any
) -> dict[str, Any]:
    assignment_by_source = {
        int(item.price_list.id): item.assignment for item in assigned
    }
    return {
        "priceFormatId": int(price_format_id),
        "dependencyFingerprint": vector.fingerprint,
        "matcherVersion": vector.matcher_version,
        "normalizationVersion": vector.normalization_version,
        "sources": [
            {
                "sourceId": item.source_id,
                "sourceVersionId": item.source_version_id,
                "sourcePublicationId": item.source_publication_id,
                "snapshotId": item.snapshot_id,
                "sourceName": item.source_name,
                "supplier": item.supplier,
                "priceDate": item.price_date,
                "effectiveCoefficient": item.coefficient,
                "assignmentId": int(assignment_by_source[item.source_id].id),
                "assignmentCoefficient": assignment_by_source[item.source_id].coefficient,
                "percentileMode": assignment_by_source[item.source_id].percentile_mode,
                "sourceMode": assignment_by_source[item.source_id].source_mode,
            }
            for item in sorted(resolved, key=lambda row: row.source_id)
        ],
    }


def _validate_cold_item_counts(
    *, db: Session, resolved: Sequence[ResolvedSourceSnapshot]
) -> None:
    """Verify immutable row completeness before publishing a new output.

    Warm reuse already has atomically published business rows and validates
    their active count. Recounting every immutable input there would turn a
    cache hit into two full index scans.
    """

    for item in resolved:
        version_item_count = int(
            db.scalar(
                select(func.count(CompetitorSourceVersionItem.id)).where(
                    CompetitorSourceVersionItem.source_version_id
                    == item.source_version_id
                )
            )
            or 0
        )
        if version_item_count != item.source_row_count:
            _conflict(f"source {item.source_id} immutable version is incomplete")
        snapshot_item_count = int(
            db.scalar(
                select(func.count(CompetitorMatchSnapshotItem.id)).where(
                    CompetitorMatchSnapshotItem.match_snapshot_id == item.snapshot_id
                )
            )
            or 0
        )
        if snapshot_item_count != item.source_row_count:
            _conflict(
                f"source {item.source_id} snapshot {item.snapshot_id} is incomplete"
            )
        physical_lineage_count = int(
            db.scalar(
                select(func.count(CompetitorSourcePublicationItem.id))
                .select_from(CompetitorSourcePublicationItem)
                .join(
                    CompetitorSourceVersionItem,
                    CompetitorSourceVersionItem.id
                    == CompetitorSourcePublicationItem.source_version_item_id,
                )
                .where(
                    CompetitorSourcePublicationItem.source_publication_id
                    == item.source_publication_id,
                    CompetitorSourceVersionItem.source_version_id
                    == item.source_version_id,
                )
            )
            or 0
        )
        if physical_lineage_count != item.source_row_count:
            _conflict(
                f"source {item.source_id} physical publication lineage is invalid"
            )


def materialize_ready_snapshots(
    *, db: Session, price_format_id: int, assigned: Sequence[Any], read_mode: str
) -> CacheMaterializationResult:
    if db.get_bind().dialect.name != "postgresql":
        if read_mode == "required":
            raise MatchingCacheUnavailable("matching cache reads require PostgreSQL")
        return CacheMaterializationResult(False, "unsupported_database", read_mode)

    lookup_started = time.perf_counter()
    vector = build_dependency_vector(db=db)
    expected_source_ids = tuple(sorted(int(item.price_list.id) for item in assigned))
    resolved: list[ResolvedSourceSnapshot] = []
    for item in assigned:
        try:
            source_vector, _context = build_source_dependency_vector(
                db=db,
                competitor_price_list_id=int(item.price_list.id),
                base_vector=vector,
                expected_source_ids=expected_source_ids,
            )
        except MatchingContextConflict as exc:
            _conflict(str(exc))
        result = _resolve_source_snapshot(
            db=db,
            assigned_item=item,
            vector=source_vector,
            expected_source_ids=expected_source_ids,
        )
        if result is None:
            reason = f"snapshot_absent_for_source_{int(item.price_list.id)}"
            if read_mode == "required":
                raise MatchingCacheUnavailable(reason)
            return CacheMaterializationResult(
                False,
                reason,
                read_mode,
                cache_lookup_ms=(time.perf_counter() - lookup_started) * 1000,
            )
        resolved.append(result)

    payload = _materialization_payload(
        price_format_id=price_format_id,
        assigned=assigned,
        resolved=resolved,
        vector=vector,
    )
    assignment_fingerprint = _fingerprint({"sources": payload["sources"]})
    materialization_key = _fingerprint(payload)
    existing = db.scalar(
        select(PriceFormatCompetitorMaterialization)
        .where(PriceFormatCompetitorMaterialization.price_format_id == int(price_format_id))
        .where(PriceFormatCompetitorMaterialization.materialization_key == materialization_key)
        .with_for_update()
    )
    lookup_ms = (time.perf_counter() - lookup_started) * 1000
    if existing is not None and existing.status == "published" and existing.is_current:
        actual_rows = int(db.scalar(
            select(func.count(CompetitorPrice.id))
            .where(CompetitorPrice.price_format_id == int(price_format_id))
            .where(CompetitorPrice.product_id.is_not(None))
        ) or 0)
        if actual_rows != int(existing.row_count):
            _conflict(
                f"materialization {existing.id} row count {existing.row_count} != active rows {actual_rows}"
            )
        return CacheMaterializationResult(
            True,
            "materialization_unchanged",
            read_mode,
            tuple(item.source_version_id for item in resolved),
            tuple(item.snapshot_id for item in resolved),
            int(existing.id),
            materialization_key,
            True,
            actual_rows,
            lookup_ms,
            0.0,
            tuple((item.source_name, item.legacy_stats) for item in resolved),
        )

    _validate_cold_item_counts(db=db, resolved=resolved)
    materialize_started = time.perf_counter()
    row = existing
    if row is None:
        row = PriceFormatCompetitorMaterialization(
            price_format_id=int(price_format_id),
            materialization_key=materialization_key,
            assignment_fingerprint=assignment_fingerprint,
            source_snapshot_ids_json=json.dumps(
                [item.snapshot_id for item in resolved], separators=(",", ":")
            ),
            status="pending",
            is_current=False,
        )
        db.add(row)
        db.flush()
    row.status = "building"
    row.error = ""
    row.failed_at = None
    db.flush()

    db.execute(
        delete(CompetitorPrice)
        .where(CompetitorPrice.price_format_id == int(price_format_id))
        .where(CompetitorPrice.product_id.is_not(None))
    )
    source_selects = []
    for item in resolved:
        source_selects.append(
            select(
                literal(int(price_format_id), type_=CompetitorPrice.price_format_id.type),
                CompetitorMatchSnapshotItem.product_id,
                literal(item.source_name, type_=CompetitorPrice.source_name.type),
                literal(item.supplier, type_=CompetitorPrice.supplier.type),
                literal(item.price_date, type_=CompetitorPrice.price_date.type),
                literal(item.coefficient, type_=CompetitorPrice.coefficient.type),
                CompetitorSourceVersionItem.distributor_price,
                CompetitorMatchSnapshotItem.match_type,
                CompetitorSourcePublicationItem.physical_source_item_id,
                CompetitorSourceVersionItem.provisor_goods_id,
                CompetitorSourceVersionItem.distributor_goods_id,
                CompetitorSourceVersionItem.raw_manufacturer,
            )
            .select_from(CompetitorMatchSnapshotItem)
            .join(
                CompetitorSourceVersionItem,
                CompetitorSourceVersionItem.id
                == CompetitorMatchSnapshotItem.source_version_item_id,
            )
            .join(
                CompetitorSourcePublicationItem,
                CompetitorSourcePublicationItem.source_version_item_id
                == CompetitorSourceVersionItem.id,
            )
            .where(CompetitorMatchSnapshotItem.match_snapshot_id == item.snapshot_id)
            .where(
                CompetitorSourcePublicationItem.source_publication_id
                == item.source_publication_id
            )
            .where(CompetitorMatchSnapshotItem.product_id.is_not(None))
            .where(CompetitorSourceVersionItem.distributor_price.is_not(None))
        )
    if source_selects:
        combined = source_selects[0] if len(source_selects) == 1 else union_all(*source_selects)
        db.execute(
            insert(CompetitorPrice).from_select(
                (
                    "price_format_id",
                    "product_id",
                    "source_name",
                    "supplier",
                    "price_date",
                    "coefficient",
                    "source_price",
                    "match_type",
                    "source_item_id",
                    "source_goods_id",
                    "source_distributor_goods_id",
                    "source_manufacturer",
                ),
                combined,
            )
        )
    row_count = int(db.scalar(
        select(func.count(CompetitorPrice.id))
        .where(CompetitorPrice.price_format_id == int(price_format_id))
        .where(CompetitorPrice.product_id.is_not(None))
    ) or 0)
    db.execute(
        update(PriceFormatCompetitorMaterialization)
        .where(PriceFormatCompetitorMaterialization.price_format_id == int(price_format_id))
        .where(PriceFormatCompetitorMaterialization.is_current.is_(True))
        .where(PriceFormatCompetitorMaterialization.id != int(row.id))
        .values(is_current=False, status="superseded")
        .execution_options(synchronize_session=False)
    )
    now = now_kz_naive()
    row.assignment_fingerprint = assignment_fingerprint
    row.source_snapshot_ids_json = json.dumps(
        [item.snapshot_id for item in resolved], separators=(",", ":")
    )
    row.status = "published"
    row.is_current = True
    row.row_count = row_count
    row.ready_at = now
    row.published_at = now
    row.error = ""
    db.flush()
    return CacheMaterializationResult(
        True,
        "ready_snapshots_materialized",
        read_mode,
        tuple(item.source_version_id for item in resolved),
        tuple(item.snapshot_id for item in resolved),
        int(row.id),
        materialization_key,
        False,
        row_count,
        lookup_ms,
        (time.perf_counter() - materialize_started) * 1000,
        tuple((item.source_name, item.legacy_stats) for item in resolved),
    )
