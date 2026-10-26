from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import time
from typing import Iterator

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ...models import (
    CompetitorPriceList,
    CompetitorPriceListItem,
    CompetitorSourceVersion,
    CompetitorSourceVersionItem,
)
from ..pricing_workflow.locks import acquire_transaction_write_locks
from .config import matching_cache_dual_write_enabled, matching_cache_shadow_enabled
from .context import build_source_dependency_vector, resolve_matching_contexts
from .fingerprints import (
    CanonicalSourceHasher,
    CanonicalSourceRow,
    canonical_source_row,
)
from .lineage import publish_physical_source_lineage
from .versions import (
    mark_source_version_matching,
    mark_source_version_ready,
    mark_source_version_ready_for_republication,
    publish_source_version,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LegacySourceFingerprint:
    content_sha256: str
    row_count: int
    elapsed_seconds: float


@dataclass(frozen=True)
class CaptureOutcome:
    enabled: bool
    succeeded: bool
    reused: bool = False
    version_id: int | None = None
    publication_id: int | None = None
    row_count: int = 0
    content_sha256: str = ""
    error: str = ""


@dataclass(frozen=True)
class StreamingSourceVersionCreation:
    version: CompetitorSourceVersion
    reused: bool
    fingerprint: LegacySourceFingerprint


def _legacy_item_mapping(item: CompetitorPriceListItem, ordinal: int) -> dict:
    return {
        "source_ordinal": ordinal,
        "legacy_source_item_id": int(item.id),
        "product_id": item.product_id,
        "provisor_id": item.provisor_id,
        "provisor_goods_id": item.provisor_goods_id,
        "filial_id": item.filial_id,
        "name": item.name,
        "reg_number": item.reg_number,
        "distributor_goods_name": item.distributor_goods_name,
        "distributor_goods_id": item.distributor_goods_id,
        "distributor_price": item.distributor_price,
        "stock": item.stock,
        "package_count": item.package_count,
        "expiry_date": item.expiry_date,
        "match_key": item.match_key,
        "match_type": item.match_type,
        "match_score": item.match_score,
        "matched_sku": item.matched_sku,
        "raw_name": item.raw_name,
        "raw_manufacturer": item.raw_manufacturer,
        "normalized_name": item.normalized_name,
        "normalized_manufacturer": item.normalized_manufacturer,
        "parsed_base_name": item.parsed_base_name,
        "parsed_form": item.parsed_form,
        "parsed_forms_json": item.parsed_forms_json,
        "parsed_dosage": item.parsed_dosage,
        "parsed_dosage_volume": item.parsed_dosage_volume,
        "parsed_quantity": item.parsed_quantity,
        "parsed_volume": item.parsed_volume,
        "parsed_weight": item.parsed_weight,
        "parsed_percent_strength": item.parsed_percent_strength,
        "parsed_concentration": item.parsed_concentration,
        "parsed_iu_dosage": item.parsed_iu_dosage,
        "parsed_strength_signature": item.parsed_strength_signature,
        "parsed_dimensions_json": item.parsed_dimensions_json,
        "parsed_critical_tokens_json": item.parsed_critical_tokens_json,
        "raw_json": item.raw_json,
    }


def iter_legacy_source_rows(
    *, db: Session, competitor_price_list_id: int, batch_size: int = 2000
) -> Iterator[CanonicalSourceRow]:
    occurrences: dict[str, int] = {}
    result = db.scalars(
        select(CompetitorPriceListItem)
        .where(CompetitorPriceListItem.price_list_id == int(competitor_price_list_id))
        .order_by(CompetitorPriceListItem.id.asc())
        .execution_options(yield_per=max(1, int(batch_size)), stream_results=True)
    )
    for ordinal, item in enumerate(result):
        yield canonical_source_row(
            _legacy_item_mapping(item, ordinal),
            input_ordinal=ordinal,
            occurrences=occurrences,
        )


def fingerprint_legacy_source(
    *, db: Session, competitor_price_list_id: int, batch_size: int = 2000
) -> LegacySourceFingerprint:
    started_at = time.perf_counter()
    db.flush()
    hasher = CanonicalSourceHasher()
    for row in iter_legacy_source_rows(
        db=db,
        competitor_price_list_id=competitor_price_list_id,
        batch_size=batch_size,
    ):
        hasher.add(row)
    digest, row_count = hasher.finish()
    return LegacySourceFingerprint(
        content_sha256=digest,
        row_count=row_count,
        elapsed_seconds=time.perf_counter() - started_at,
    )


def create_or_reuse_legacy_source_version(
    *,
    db: Session,
    competitor_price_list_id: int,
    source_metadata: dict | None = None,
    batch_size: int = 2000,
) -> StreamingSourceVersionCreation:
    source_id = int(competitor_price_list_id)
    acquire_transaction_write_locks(
        db,
        source_ids=[source_id],
        retry_timeout_seconds=5.0,
    )
    db.flush()
    source = db.scalar(
        select(CompetitorPriceList)
        .where(CompetitorPriceList.id == source_id)
        .with_for_update()
    )
    if source is None:
        raise ValueError(f"competitor price list not found: {source_id}")
    fingerprint = fingerprint_legacy_source(
        db=db, competitor_price_list_id=source_id, batch_size=batch_size
    )
    existing = db.scalar(
        select(CompetitorSourceVersion)
        .where(CompetitorSourceVersion.competitor_price_list_id == source_id)
        .where(CompetitorSourceVersion.content_sha256 == fingerprint.content_sha256)
    )
    if existing is not None:
        return StreamingSourceVersionCreation(
            version=existing,
            reused=True,
            fingerprint=fingerprint,
        )

    generation = db.scalar(
        select(func.max(CompetitorSourceVersion.generation)).where(
            CompetitorSourceVersion.competitor_price_list_id == source_id
        )
    )
    version = CompetitorSourceVersion(
        competitor_price_list_id=source_id,
        generation=int(generation or 0) + 1,
        content_sha256=fingerprint.content_sha256,
        matching_input_sha256=fingerprint.content_sha256,
        row_count=fingerprint.row_count,
        status="pending",
        source_metadata_json=json.dumps(
            source_metadata or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ),
    )
    db.add(version)
    db.flush()

    mappings: list[dict] = []
    persisted = 0
    for row in iter_legacy_source_rows(
        db=db, competitor_price_list_id=source_id, batch_size=batch_size
    ):
        mappings.append(row.persistence_mapping(int(version.id)))
        if len(mappings) >= batch_size:
            db.bulk_insert_mappings(CompetitorSourceVersionItem, mappings)
            persisted += len(mappings)
            mappings.clear()
    if mappings:
        db.bulk_insert_mappings(CompetitorSourceVersionItem, mappings)
        persisted += len(mappings)
    if persisted != fingerprint.row_count:
        raise RuntimeError(
            f"source changed during capture: fingerprint_rows={fingerprint.row_count} persisted_rows={persisted}"
        )
    db.flush()
    return StreamingSourceVersionCreation(
        version=version,
        reused=False,
        fingerprint=fingerprint,
    )


def _postgres_io_sample(db: Session) -> tuple[str, int] | None:
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return None
    row = db.execute(
        text(
            "SELECT pg_current_wal_lsn()::text, COALESCE(temp_bytes, 0) "
            "FROM pg_stat_database WHERE datname = current_database()"
        )
    ).one()
    return str(row[0]), int(row[1])


def _wal_bytes_between(db: Session, before: str, after: str) -> int:
    return int(
        db.scalar(text("SELECT pg_wal_lsn_diff(CAST(:after AS pg_lsn), CAST(:before AS pg_lsn))"), {"after": after, "before": before})
        or 0
    )


def capture_and_publish_legacy_source_version(
    *,
    db: Session,
    competitor_price_list_id: int,
    source_metadata: dict | None = None,
    batch_size: int = 2000,
    force_enabled: bool = False,
    enqueue_shadow: bool | None = None,
) -> CaptureOutcome:
    """Best-effort dual write isolated by a savepoint; never commits."""

    if not force_enabled and not matching_cache_dual_write_enabled():
        return CaptureOutcome(enabled=False, succeeded=False)
    started_at = time.perf_counter()
    io_before = _postgres_io_sample(db)
    try:
        with db.begin_nested():
            created = create_or_reuse_legacy_source_version(
                db=db,
                competitor_price_list_id=competitor_price_list_id,
                source_metadata=source_metadata,
                batch_size=batch_size,
            )
            version = created.version
            if version.status in {"pending", "failed"}:
                mark_source_version_matching(db=db, version_id=int(version.id))
                mark_source_version_ready(db=db, version_id=int(version.id))
                version = publish_source_version(db=db, version_id=int(version.id))
            elif version.status == "ready":
                version = publish_source_version(db=db, version_id=int(version.id))
            elif version.status == "superseded":
                mark_source_version_ready_for_republication(
                    db=db, version_id=int(version.id)
                )
                version = publish_source_version(db=db, version_id=int(version.id))
            publication = publish_physical_source_lineage(
                db=db, source_version=version
            )
            should_enqueue_shadow = (
                force_enabled or matching_cache_shadow_enabled()
                if enqueue_shadow is None
                else bool(enqueue_shadow)
            )
            if should_enqueue_shadow:
                from .jobs import enqueue_matching_job
                from .shadow import get_or_create_shadow_snapshot

                scheduled: set[tuple[int, str]] = set()
                for context in resolve_matching_contexts(
                    db=db,
                    competitor_price_list_id=int(version.competitor_price_list_id),
                ):
                    for price_format_id in context.price_format_ids:
                        for affected_source_id in context.source_ids:
                            affected_version = (
                                version
                                if affected_source_id
                                == int(version.competitor_price_list_id)
                                else db.scalar(select(CompetitorSourceVersion).where(
                                    CompetitorSourceVersion.competitor_price_list_id
                                    == affected_source_id,
                                    CompetitorSourceVersion.status == "published",
                                ))
                            )
                            if affected_version is None:
                                continue
                            vector, affected_context = build_source_dependency_vector(
                                db=db,
                                competitor_price_list_id=affected_source_id,
                                price_format_id=price_format_id,
                            )
                            schedule_key = (
                                int(affected_version.id), vector.fingerprint
                            )
                            if schedule_key in scheduled:
                                continue
                            scheduled.add(schedule_key)
                            snapshot, _ = get_or_create_shadow_snapshot(
                                db=db,
                                source_version_id=int(affected_version.id),
                                vector=vector,
                            )
                            enqueue_matching_job(
                                db=db,
                                source_version_id=int(affected_version.id),
                                dependency_fingerprint=vector.fingerprint,
                                matching_context_key=affected_context.context_key,
                                match_snapshot_id=int(snapshot.id),
                            )
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
                "[MATCH_CACHE_SOURCE_VERSION] source_id=%s version_id=%s reused=%s rows=%s fingerprint_sec=%.6f total_sec=%.6f wal_bytes_approx=%s temp_bytes_approx=%s",
                competitor_price_list_id,
                version.id,
                created.reused,
                version.row_count,
                created.fingerprint.elapsed_seconds,
                time.perf_counter() - started_at,
                wal_bytes,
                temp_bytes,
            )
            return CaptureOutcome(
                enabled=True,
                succeeded=True,
                reused=created.reused,
                version_id=int(version.id),
                publication_id=int(publication.id),
                row_count=int(version.row_count),
                content_sha256=version.content_sha256,
            )
    except Exception as exc:
        # The nested transaction is rolled back while the legacy import remains
        # valid. The structured error log is the Phase 2B.2 failure record.
        logger.exception(
            "[MATCH_CACHE_SOURCE_VERSION_FAILED] source_id=%s elapsed_sec=%.6f error=%s",
            competitor_price_list_id,
            time.perf_counter() - started_at,
            str(exc)[:1000],
        )
        return CaptureOutcome(
            enabled=True,
            succeeded=False,
            error=str(exc)[:1000],
        )
