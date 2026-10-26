from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Iterable, Mapping

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ...models import (
    CompetitorPriceList,
    CompetitorSourceVersion,
    CompetitorSourceVersionItem,
)
from ...timezone import now_kz_naive
from .fingerprints import CanonicalSourceContent, canonical_source_content


class SourceVersionStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceVersionCreation:
    version: CompetitorSourceVersion
    reused: bool
    fingerprint: CanonicalSourceContent


@dataclass(frozen=True)
class SourceVersionMetadata:
    id: int
    competitor_price_list_id: int
    generation: int
    content_sha256: str
    matching_input_sha256: str
    row_count: int
    status: str
    source_metadata: dict[str, Any]
    error: str
    created_at: datetime
    matching_started_at: datetime | None
    ready_at: datetime | None
    published_at: datetime | None
    failed_at: datetime | None


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def create_or_reuse_pending_source_version(
    *,
    db: Session,
    competitor_price_list_id: int,
    rows: Iterable[Mapping[str, Any]],
    source_metadata: Mapping[str, Any] | None = None,
) -> SourceVersionCreation:
    """Create one immutable generation without committing the caller's transaction.

    The parent source row is locked so concurrent creators serialize per source.
    A content hash unique constraint remains the final database invariant.
    """

    source_id = int(competitor_price_list_id)
    locked_source_id = db.execute(
        select(CompetitorPriceList.id)
        .where(CompetitorPriceList.id == source_id)
        .with_for_update()
    ).scalar_one_or_none()
    if locked_source_id is None:
        raise ValueError(f"competitor price list not found: {source_id}")
    source_type = db.scalar(
        select(CompetitorPriceList.source_type).where(
            CompetitorPriceList.id == source_id
        )
    )
    fingerprint = canonical_source_content(
        rows,
        include_match_decisions=str(source_type or "") == "manual_vidman",
    )

    existing = db.execute(
        select(CompetitorSourceVersion)
        .where(CompetitorSourceVersion.competitor_price_list_id == source_id)
        .where(CompetitorSourceVersion.content_sha256 == fingerprint.content_sha256)
    ).scalar_one_or_none()
    if existing is not None:
        return SourceVersionCreation(version=existing, reused=True, fingerprint=fingerprint)

    current_generation = db.execute(
        select(func.max(CompetitorSourceVersion.generation)).where(
            CompetitorSourceVersion.competitor_price_list_id == source_id
        )
    ).scalar_one()
    version = CompetitorSourceVersion(
        competitor_price_list_id=source_id,
        generation=int(current_generation or 0) + 1,
        content_sha256=fingerprint.content_sha256,
        matching_input_sha256=fingerprint.matching_input_sha256,
        row_count=fingerprint.row_count,
        status="pending",
        source_metadata_json=_json(source_metadata),
        error="",
    )
    db.add(version)
    db.flush()
    if fingerprint.rows:
        db.bulk_insert_mappings(
            CompetitorSourceVersionItem,
            [row.persistence_mapping(int(version.id)) for row in fingerprint.rows],
        )
    db.flush()
    return SourceVersionCreation(version=version, reused=False, fingerprint=fingerprint)


def _transition(
    *,
    db: Session,
    version_id: int,
    expected_statuses: set[str],
    status: str,
    values: Mapping[str, Any] | None = None,
) -> CompetitorSourceVersion:
    result = db.execute(
        update(CompetitorSourceVersion)
        .where(CompetitorSourceVersion.id == int(version_id))
        .where(CompetitorSourceVersion.status.in_(sorted(expected_statuses)))
        .values(status=status, **dict(values or {}))
        .execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        current = db.get(CompetitorSourceVersion, int(version_id))
        current_status = current.status if current is not None else "missing"
        raise SourceVersionStateError(
            f"source version {version_id} cannot transition from {current_status} to {status}"
        )
    db.flush()
    db.expire_all()
    return db.get(CompetitorSourceVersion, int(version_id))


def mark_source_version_matching(*, db: Session, version_id: int) -> CompetitorSourceVersion:
    return _transition(
        db=db,
        version_id=version_id,
        expected_statuses={"pending", "failed"},
        status="matching",
        values={"matching_started_at": now_kz_naive(), "failed_at": None, "error": ""},
    )


def mark_source_version_ready(*, db: Session, version_id: int) -> CompetitorSourceVersion:
    return _transition(
        db=db,
        version_id=version_id,
        expected_statuses={"matching"},
        status="ready",
        values={"ready_at": now_kz_naive(), "error": ""},
    )


def mark_source_version_ready_for_republication(
    *, db: Session, version_id: int
) -> CompetitorSourceVersion:
    return _transition(
        db=db,
        version_id=version_id,
        expected_statuses={"superseded"},
        status="ready",
        values={"ready_at": now_kz_naive(), "error": ""},
    )


def publish_source_version(*, db: Session, version_id: int) -> CompetitorSourceVersion:
    version = db.execute(
        select(CompetitorSourceVersion)
        .where(CompetitorSourceVersion.id == int(version_id))
        .with_for_update()
    ).scalar_one_or_none()
    if version is None:
        raise SourceVersionStateError(f"source version {version_id} cannot transition from missing to published")
    if version.status != "ready":
        raise SourceVersionStateError(
            f"source version {version_id} cannot transition from {version.status} to published"
        )

    # Serialize publication per logical source. Superseding the old version and
    # publishing the new one are deliberately part of the caller's transaction.
    db.execute(
        select(CompetitorPriceList.id)
        .where(CompetitorPriceList.id == version.competitor_price_list_id)
        .with_for_update()
    ).scalar_one()
    db.execute(
        update(CompetitorSourceVersion)
        .where(CompetitorSourceVersion.competitor_price_list_id == version.competitor_price_list_id)
        .where(CompetitorSourceVersion.status == "published")
        .where(CompetitorSourceVersion.id != int(version_id))
        .values(status="superseded")
        .execution_options(synchronize_session=False)
    )
    return _transition(
        db=db,
        version_id=version_id,
        expected_statuses={"ready"},
        status="published",
        values={"published_at": now_kz_naive(), "error": ""},
    )


def record_source_version_failure(
    *, db: Session, version_id: int, error: str
) -> CompetitorSourceVersion:
    return _transition(
        db=db,
        version_id=version_id,
        expected_statuses={"pending", "matching", "ready", "failed"},
        status="failed",
        values={"failed_at": now_kz_naive(), "error": str(error or "")[:4000]},
    )


def get_source_version_metadata(*, db: Session, version_id: int) -> SourceVersionMetadata | None:
    row = db.get(CompetitorSourceVersion, int(version_id))
    if row is None:
        return None
    try:
        metadata = json.loads(row.source_metadata_json or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return SourceVersionMetadata(
        id=int(row.id),
        competitor_price_list_id=int(row.competitor_price_list_id),
        generation=int(row.generation),
        content_sha256=row.content_sha256,
        matching_input_sha256=row.matching_input_sha256,
        row_count=int(row.row_count),
        status=row.status,
        source_metadata=metadata,
        error=row.error or "",
        created_at=row.created_at,
        matching_started_at=row.matching_started_at,
        ready_at=row.ready_at,
        published_at=row.published_at,
        failed_at=row.failed_at,
    )
