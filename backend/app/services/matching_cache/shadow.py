from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
from itertools import zip_longest
import logging
import os
import sqlite3
import tempfile
import time

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...models import (
    CompetitorMatchSnapshot,
    CompetitorMatchSnapshotItem,
    CompetitorPriceList,
    CompetitorPriceListItem,
    CompetitorSourceVersion,
    CompetitorSourceVersionItem,
)
from ...timezone import now_kz_naive
from ..competitor_matching import (
    _product_indexes,
    _sync_provisor_reference_mapping_from_items,
    rematch_price_list_items_by_product,
)
from ..pricing_workflow.locks import acquire_transaction_write_locks
from .capture import (
    _postgres_io_sample,
    _wal_bytes_between,
    fingerprint_legacy_source,
    iter_legacy_source_rows,
)
from .dependencies import (
    MANUAL_CODE_MAPPINGS,
    MATCHER_VERSION,
    NORMALIZATION_VERSION,
    PRODUCT_CATALOG,
    PROVISOR_REFERENCES,
    PROVISOR_SUBSTITUTES,
    VIDMAN_AUTHORITATIVE,
    DependencyVector,
    build_dependency_vector,
    matching_context_key,
)
from .context import (
    MatchingContextConflict,
    build_source_dependency_vector,
    resolve_matching_context,
    resolve_matching_contexts,
)


logger = logging.getLogger(__name__)


def _authoritative_decisions_match_context(
    *, source_metadata: dict, context_price_format_ids: tuple[int, ...], context_count: int
) -> bool:
    if source_metadata.get("matchingDecisionMode") != "authoritative_legacy_output":
        return False
    value = source_metadata.get(
        "authoritativePriceFormatId", source_metadata.get("priceFormatId")
    )
    try:
        authoritative_price_format_id = int(value)
    except (TypeError, ValueError):
        authoritative_price_format_id = None
    if authoritative_price_format_id is not None:
        return authoritative_price_format_id in context_price_format_ids
    # Compatibility for old single-context backfills.  Unscoped decisions are
    # never safe once a physical source participates in multiple contexts.
    return context_count == 1


class ShadowSnapshotConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ShadowSnapshotResult:
    snapshot_id: int
    source_version_id: int
    row_count: int
    matched_count: int
    dependency_fingerprint: str
    duration_seconds: float


def _revision(vector: DependencyVector, scope: str) -> int:
    return int(vector.revisions.get(f"{scope}:global", 0))


class _DecisionSpool:
    """Disk-backed transfer across the matcher savepoint rollback.

    SQLite is deliberately used instead of a Python list/dict: a large source
    may contain hundreds of thousands of rows, while the matcher decisions
    have to survive rolling back its legacy side effects.  The temporary file
    is local to the worker and is removed on success, failure or cancellation.
    """

    def __init__(self) -> None:
        cleanup_stale_shadow_spools()
        handle = tempfile.NamedTemporaryFile(
            prefix=f"match-shadow-{os.getpid()}-", suffix=".sqlite3", delete=False
        )
        self.path = handle.name
        handle.close()
        self.connection = sqlite3.connect(self.path)
        self.connection.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            CREATE TABLE identity_map (
                legacy_id INTEGER PRIMARY KEY,
                source_version_item_id INTEGER NOT NULL
            );
            CREATE TABLE decisions (
                legacy_id INTEGER PRIMARY KEY,
                product_id INTEGER,
                match_key TEXT NOT NULL,
                match_type TEXT NOT NULL,
                match_score TEXT,
                matched_sku TEXT NOT NULL,
                is_manual INTEGER NOT NULL,
                is_rejected INTEGER NOT NULL,
                is_authoritative INTEGER NOT NULL
            );
            """
        )

    def close(self) -> None:
        try:
            self.connection.close()
        finally:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


def cleanup_stale_shadow_spools(
    *, directory: str | os.PathLike[str] | None = None, older_than_seconds: float = 3600
) -> list[str]:
    """Remove abandoned local spools without touching active recent workers."""

    root = Path(directory or tempfile.gettempdir())
    cutoff = time.time() - max(0.0, float(older_than_seconds))
    removed: list[str] = []
    for path in root.glob("match-shadow-*-*.sqlite3"):
        try:
            if path.stat().st_mtime > cutoff:
                continue
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("[MATCH_CACHE_SPOOL_CLEANUP_FAILED] path=%s", path, exc_info=True)
    if removed:
        logger.warning("[MATCH_CACHE_SPOOL_CLEANUP] removed=%s", len(removed))
    return removed


def get_or_create_shadow_snapshot(
    *, db: Session, source_version_id: int, vector: DependencyVector | None = None
) -> tuple[CompetitorMatchSnapshot, bool]:
    source_version = db.get(CompetitorSourceVersion, int(source_version_id))
    if source_version is None:
        raise ShadowSnapshotConflict(f"source version not found: {source_version_id}")
    base_vector = vector or build_dependency_vector(db=db)
    if base_vector.matching_context is None:
        vector, _context = build_source_dependency_vector(
            db=db,
            competitor_price_list_id=int(source_version.competitor_price_list_id),
            base_vector=base_vector,
        )
    else:
        vector = base_vector
    existing = db.scalar(
        select(CompetitorMatchSnapshot)
        .where(CompetitorMatchSnapshot.source_version_id == int(source_version_id))
        .where(CompetitorMatchSnapshot.dependency_fingerprint == vector.fingerprint)
        .where(CompetitorMatchSnapshot.matcher_version == vector.matcher_version)
        .where(CompetitorMatchSnapshot.normalization_version == vector.normalization_version)
    )
    if existing is not None:
        expected_context_key = (
            matching_context_key(vector.matching_context)
            if vector.matching_context is not None else ""
        )
        if expected_context_key and not existing.matching_context_key:
            existing.matching_context_key = expected_context_key
            db.flush()
        elif (
            expected_context_key
            and existing.matching_context_key != expected_context_key
        ):
            raise ShadowSnapshotConflict("snapshot context identity mismatch")
        return existing, True
    try:
        with db.begin_nested():
            row = CompetitorMatchSnapshot(
                source_version_id=int(source_version_id),
                dependency_fingerprint=vector.fingerprint,
                matching_context_key=(
                    matching_context_key(vector.matching_context)
                    if vector.matching_context is not None else ""
                ),
                dependency_vector_json=json.dumps(
                    vector.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                product_catalog_revision=_revision(vector, PRODUCT_CATALOG),
                manual_code_mapping_revision=_revision(vector, MANUAL_CODE_MAPPINGS),
                provisor_reference_revision=_revision(vector, PROVISOR_REFERENCES),
                provisor_substitute_revision=_revision(vector, PROVISOR_SUBSTITUTES),
                vidman_authoritative_revision=_revision(vector, VIDMAN_AUTHORITATIVE),
                matcher_version=MATCHER_VERSION,
                normalization_version=NORMALIZATION_VERSION,
                status="pending",
            )
            db.add(row)
            db.flush()
        return row, False
    except IntegrityError:
        row = db.scalar(
            select(CompetitorMatchSnapshot)
            .where(CompetitorMatchSnapshot.source_version_id == int(source_version_id))
            .where(CompetitorMatchSnapshot.dependency_fingerprint == vector.fingerprint)
            .where(CompetitorMatchSnapshot.matcher_version == vector.matcher_version)
            .where(CompetitorMatchSnapshot.normalization_version == vector.normalization_version)
        )
        if row is None:
            raise
        expected_context_key = (
            matching_context_key(vector.matching_context)
            if vector.matching_context is not None else ""
        )
        if expected_context_key and not row.matching_context_key:
            row.matching_context_key = expected_context_key
            db.flush()
        elif expected_context_key and row.matching_context_key != expected_context_key:
            raise ShadowSnapshotConflict("snapshot context identity mismatch")
        return row, True


def build_shadow_snapshot(
    *, db: Session, snapshot_id: int, expected_dependency_fingerprint: str = ""
) -> ShadowSnapshotResult:
    """Validate with the matcher, then cache the immutable legacy decisions.

    Existing-source backfills explicitly freeze the production legacy
    workflow's final authoritative decision fields. Reinterpreting those rows
    can change precedence after the legacy run has persisted SourceGoodsMatch
    rows, so their rolled-back matcher is diagnostic only. Normal import
    captures continue to use the shadow rematch result.
    """

    started_at = time.perf_counter()
    io_before = _postgres_io_sample(db)
    lock_target = db.execute(
        select(
            CompetitorMatchSnapshot.source_version_id,
            CompetitorMatchSnapshot.matching_context_key,
            CompetitorSourceVersion.competitor_price_list_id,
        )
        .join(
            CompetitorSourceVersion,
            CompetitorSourceVersion.id == CompetitorMatchSnapshot.source_version_id,
        )
        .where(CompetitorMatchSnapshot.id == int(snapshot_id))
    ).one_or_none()
    if lock_target is None:
        raise ShadowSnapshotConflict(f"snapshot not found: {snapshot_id}")
    try:
        context = resolve_matching_context(
            db=db,
            competitor_price_list_id=int(lock_target.competitor_price_list_id),
            context_key=str(lock_target.matching_context_key or "") or None,
        )
    except MatchingContextConflict as exc:
        raise ShadowSnapshotConflict(str(exc)) from exc
    acquire_transaction_write_locks(
        db,
        price_format_ids=context.price_format_ids,
        source_ids=context.source_ids,
        retry_timeout_seconds=5.0,
    )
    try:
        locked_context = resolve_matching_context(
            db=db,
            competitor_price_list_id=int(lock_target.competitor_price_list_id),
            context_key=str(lock_target.matching_context_key or "") or None,
        )
    except MatchingContextConflict as exc:
        raise ShadowSnapshotConflict(str(exc)) from exc
    if locked_context != context:
        raise ShadowSnapshotConflict("legacy matching cohort changed while acquiring locks")
    snapshot = db.scalar(
        select(CompetitorMatchSnapshot)
        .where(CompetitorMatchSnapshot.id == int(snapshot_id))
        .with_for_update()
    )
    if snapshot is None:
        raise ShadowSnapshotConflict(f"snapshot not found: {snapshot_id}")
    if snapshot.status == "ready":
        return ShadowSnapshotResult(
            snapshot_id=int(snapshot.id),
            source_version_id=int(snapshot.source_version_id),
            row_count=int(snapshot.input_row_count),
            matched_count=int(snapshot.matched_row_count),
            dependency_fingerprint=snapshot.dependency_fingerprint,
            duration_seconds=0.0,
        )
    if snapshot.status not in {"pending", "failed"}:
        raise ShadowSnapshotConflict(f"snapshot {snapshot_id} is {snapshot.status}")

    source_version = db.scalar(
        select(CompetitorSourceVersion)
        .where(CompetitorSourceVersion.id == snapshot.source_version_id)
        .with_for_update()
    )
    if source_version is None or source_version.status != "published":
        raise ShadowSnapshotConflict("shadow matching requires a published source version")
    try:
        source_metadata = json.loads(source_version.source_metadata_json or "{}")
    except (TypeError, json.JSONDecodeError):
        source_metadata = {}
    preserve_authoritative_decisions = _authoritative_decisions_match_context(
        source_metadata=source_metadata if isinstance(source_metadata, dict) else {},
        context_price_format_ids=context.price_format_ids,
        context_count=len(resolve_matching_contexts(
            db=db,
            competitor_price_list_id=int(source_version.competitor_price_list_id),
        )),
    )
    base_vector_before = build_dependency_vector(db=db)
    vector_before, _ = build_source_dependency_vector(
        db=db,
        competitor_price_list_id=int(source_version.competitor_price_list_id),
        base_vector=base_vector_before,
        expected_source_ids=context.source_ids,
        context_key=context.context_key,
    )
    required_fingerprint = expected_dependency_fingerprint or snapshot.dependency_fingerprint
    if required_fingerprint not in {
        vector_before.fingerprint,
        base_vector_before.fingerprint,
    }:
        raise ShadowSnapshotConflict("stale dependency fingerprint before shadow matching")
    if snapshot.dependency_fingerprint != vector_before.fingerprint:
        raise ShadowSnapshotConflict("snapshot matching context fingerprint is stale")
    current_source = fingerprint_legacy_source(
        db=db, competitor_price_list_id=int(source_version.competitor_price_list_id)
    )
    if (
        current_source.content_sha256 != source_version.content_sha256
        or current_source.row_count != int(source_version.row_count)
    ):
        raise ShadowSnapshotConflict("active source content no longer matches the immutable version")

    version_items = db.execute(
        select(
            CompetitorSourceVersionItem.id,
            CompetitorSourceVersionItem.source_ordinal,
            CompetitorSourceVersionItem.source_row_key,
            CompetitorSourceVersionItem.duplicate_ordinal,
            CompetitorSourceVersionItem.authoritative_product_id,
            CompetitorSourceVersionItem.authoritative_match_key,
            CompetitorSourceVersionItem.authoritative_match_type,
            CompetitorSourceVersionItem.authoritative_match_score,
            CompetitorSourceVersionItem.authoritative_matched_sku,
        )
        .where(CompetitorSourceVersionItem.source_version_id == source_version.id)
        .order_by(CompetitorSourceVersionItem.source_ordinal.asc())
        .execution_options(yield_per=2000, stream_results=True)
    )
    spool = _DecisionSpool()
    try:
        identity_count = 0
        active_rows = iter_legacy_source_rows(
            db=db,
            competitor_price_list_id=int(source_version.competitor_price_list_id),
        )
        missing_row = object()
        for version_item, active_row in zip_longest(
            version_items, active_rows, fillvalue=missing_row
        ):
            if version_item is missing_row or active_row is missing_row:
                raise ShadowSnapshotConflict("active source identity mapping is incomplete")
            (
                version_item_id,
                ordinal,
                row_key,
                duplicate_ordinal,
                product_id,
                match_key,
                match_type_value,
                match_score,
                matched_sku,
            ) = version_item
            expected = (int(ordinal), str(row_key), int(duplicate_ordinal))
            actual = (
                active_row.source_ordinal,
                active_row.source_row_key,
                active_row.duplicate_ordinal,
            )
            legacy_id = active_row.values.get("legacy_source_item_id")
            if expected != actual or legacy_id is None:
                raise ShadowSnapshotConflict("active rows do not match immutable duplicate identities")
            spool.connection.execute(
                "INSERT INTO identity_map (legacy_id, source_version_item_id) VALUES (?, ?)",
                (int(legacy_id), int(version_item_id)),
            )
            if preserve_authoritative_decisions:
                match_type = str(match_type_value or "unmatched")
                folded = match_type.casefold()
                spool.connection.execute(
                    "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(legacy_id),
                        int(product_id) if product_id is not None else None,
                        str(match_key or ""),
                        match_type,
                        str(match_score) if match_score is not None else None,
                        str(matched_sku or ""),
                        int("manual" in folded),
                        int("reject" in folded),
                        int(
                            "manual" in folded
                            or match_type.startswith("vidman_")
                            or match_type.startswith("provisor_goods")
                        ),
                    ),
                )
            identity_count += 1
        spool.connection.commit()
        if identity_count != int(source_version.row_count):
            raise ShadowSnapshotConflict("source version item count is incomplete")

        price_list = db.get(CompetitorPriceList, int(source_version.competitor_price_list_id))
        if price_list is None:
            raise ShadowSnapshotConflict("active source is missing")
        snapshot.status = "running"
        snapshot.error = ""
        db.flush()

        stats: dict = {}
        decision_count = identity_count if preserve_authoritative_decisions else 0
        nested = db.begin_nested()
        try:
            _sync_provisor_reference_mapping_from_items(
                db,
                price_list_ids=list(context.source_ids),
            )
            product_indexes = _product_indexes(db)
            stats = rematch_price_list_items_by_product(
                db=db,
                price_list=price_list,
                product_indexes=product_indexes,
                flush=True,
                collect_matched_rows=False,
                persist_materialized_fields=False,
            )
            if not preserve_authoritative_decisions:
                active_items = db.scalars(
                    select(CompetitorPriceListItem)
                    .where(CompetitorPriceListItem.price_list_id == price_list.id)
                    .order_by(CompetitorPriceListItem.id.asc())
                    .execution_options(yield_per=2000, stream_results=True)
                )
                for item in active_items:
                    match_type = str(item.match_type or "unmatched")
                    folded = match_type.casefold()
                    spool.connection.execute(
                        "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            int(item.id),
                            int(item.product_id) if item.product_id is not None else None,
                            str(item.match_key or ""),
                            match_type,
                            str(item.match_score) if item.match_score is not None else None,
                            str(item.matched_sku or ""),
                            int("manual" in folded),
                            int("reject" in folded),
                            int(
                                "manual" in folded
                                or match_type.startswith("vidman_")
                                or match_type.startswith("provisor_goods")
                            ),
                        ),
                    )
                    decision_count += 1
                spool.connection.commit()
        finally:
            nested.rollback()
            db.expire_all()

        if decision_count != identity_count:
            raise ShadowSnapshotConflict(
                f"shadow row count mismatch: decisions={decision_count} version_items={identity_count}"
            )
        missing = spool.connection.execute(
            """SELECT 1 FROM identity_map i LEFT JOIN decisions d ON d.legacy_id = i.legacy_id
               WHERE d.legacy_id IS NULL LIMIT 1"""
        ).fetchone()
        extra = spool.connection.execute(
            """SELECT 1 FROM decisions d LEFT JOIN identity_map i ON i.legacy_id = d.legacy_id
               WHERE i.legacy_id IS NULL LIMIT 1"""
        ).fetchone()
        if missing is not None or extra is not None:
            raise ShadowSnapshotConflict("shadow rows do not match immutable source row identities")

        # Retried builds replace only unpublished snapshot rows.
        db.execute(
            delete(CompetitorMatchSnapshotItem).where(
                CompetitorMatchSnapshotItem.match_snapshot_id == int(snapshot_id)
            )
        )
        batch: list[dict] = []
        matched_count = 0
        cursor = spool.connection.execute(
            """SELECT i.source_version_item_id, d.product_id, d.match_key, d.match_type,
                      d.match_score, d.matched_sku, d.is_manual, d.is_rejected, d.is_authoritative
               FROM decisions d JOIN identity_map i ON i.legacy_id = d.legacy_id
               ORDER BY d.legacy_id"""
        )
        for decision in cursor:
            if decision[1] is not None:
                matched_count += 1
            batch.append({
                "match_snapshot_id": int(snapshot_id),
                "source_version_item_id": decision[0],
                "product_id": decision[1],
                "match_key": decision[2],
                "match_type": decision[3],
                "match_score": decision[4],
                "matched_sku": decision[5],
                "is_manual": bool(decision[6]),
                "is_rejected": bool(decision[7]),
                "is_authoritative": bool(decision[8]),
                "audit_json": "{}",
            })
            if len(batch) >= 2000:
                db.bulk_insert_mappings(CompetitorMatchSnapshotItem, batch)
                batch.clear()
        if batch:
            db.bulk_insert_mappings(CompetitorMatchSnapshotItem, batch)
        db.flush()

        persisted_count = int(
            db.scalar(
                select(func.count(CompetitorMatchSnapshotItem.id)).where(
                    CompetitorMatchSnapshotItem.match_snapshot_id == int(snapshot_id)
                )
            )
            or 0
        )
        base_vector_after = build_dependency_vector(db=db)
        vector_after, _ = build_source_dependency_vector(
            db=db,
            competitor_price_list_id=int(source_version.competitor_price_list_id),
            base_vector=base_vector_after,
            expected_source_ids=context.source_ids,
            context_key=context.context_key,
        )
        final_source = fingerprint_legacy_source(
            db=db, competitor_price_list_id=int(source_version.competitor_price_list_id)
        )
        if vector_after.fingerprint != vector_before.fingerprint:
            raise ShadowSnapshotConflict("dependencies changed during shadow matching")
        if final_source.content_sha256 != source_version.content_sha256:
            raise ShadowSnapshotConflict("source changed during shadow matching")
        if persisted_count != int(source_version.row_count):
            raise ShadowSnapshotConflict("snapshot item persistence is incomplete")

        elapsed = time.perf_counter() - started_at
        snapshot = db.get(CompetitorMatchSnapshot, int(snapshot_id))
        snapshot.status = "ready"
        snapshot.input_row_count = persisted_count
        snapshot.matched_row_count = matched_count
        snapshot.unmatched_row_count = persisted_count - matched_count
        snapshot.stats_json = json.dumps(
            {
                "durationSeconds": round(elapsed, 6),
                "legacyStats": {
                    key: value for key, value in stats.items() if not str(key).startswith("_")
                },
                "legacyMatchingContext": {
                    **context.identity_dict(),
                    "contextKey": context.context_key,
                },
                "matchingDecisionMode": (
                    "authoritative_legacy_output"
                    if preserve_authoritative_decisions
                    else "shadow_rematch"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        snapshot.ready_at = now_kz_naive()
        snapshot.error = ""
        db.flush()
        io_after = _postgres_io_sample(db)
        wal_bytes = (
            _wal_bytes_between(db, io_before[0], io_after[0])
            if io_before is not None and io_after is not None
            else None
        )
        temp_bytes = (
            max(0, io_after[1] - io_before[1])
            if io_before is not None and io_after is not None
            else None
        )
        logger.info(
            "[MATCH_CACHE_SHADOW_READY] snapshot_id=%s source_version_id=%s rows=%s matched=%s duration_sec=%.6f wal_bytes_approx=%s temp_bytes_approx=%s",
            snapshot.id,
            source_version.id,
            persisted_count,
            matched_count,
            elapsed,
            wal_bytes,
            temp_bytes,
        )
        return ShadowSnapshotResult(
            snapshot_id=int(snapshot.id),
            source_version_id=int(source_version.id),
            row_count=persisted_count,
            matched_count=matched_count,
            dependency_fingerprint=vector_after.fingerprint,
            duration_seconds=elapsed,
        )
    finally:
        spool.close()
