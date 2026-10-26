"""Idempotent preparation of cache artifacts for already-imported sources."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import (
    CompetitorMatchJob,
    CompetitorSourcePublication,
    CompetitorSourceVersion,
)
from .capture import create_or_reuse_legacy_source_version, fingerprint_legacy_source
from .context import build_source_dependency_vector
from .jobs import enqueue_matching_job
from .lineage import publish_physical_source_lineage
from .shadow import get_or_create_shadow_snapshot
from .versions import (
    mark_source_version_matching,
    mark_source_version_ready,
    mark_source_version_ready_for_republication,
    publish_source_version,
)


@dataclass(frozen=True)
class BackfillPlan:
    source_id: int
    row_count: int
    content_sha256: str
    existing_version_id: int | None
    existing_version_status: str | None
    would_create_version: bool
    fingerprint_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BackfillResult:
    source_id: int
    row_count: int
    content_sha256: str
    source_version_id: int
    source_version_reused: bool
    source_publication_id: int
    source_publication_reused: bool
    snapshot_id: int
    snapshot_reused: bool
    snapshot_status: str
    job_id: int | None
    job_reused: bool
    job_status: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def plan_existing_source(
    *, db: Session, competitor_price_list_id: int, batch_size: int = 2000
) -> BackfillPlan:
    source_id = int(competitor_price_list_id)
    fingerprint = fingerprint_legacy_source(
        db=db, competitor_price_list_id=source_id, batch_size=batch_size
    )
    existing = db.scalar(
        select(CompetitorSourceVersion).where(
            CompetitorSourceVersion.competitor_price_list_id == source_id,
            CompetitorSourceVersion.content_sha256 == fingerprint.content_sha256,
        )
    )
    return BackfillPlan(
        source_id=source_id,
        row_count=int(fingerprint.row_count),
        content_sha256=fingerprint.content_sha256,
        existing_version_id=int(existing.id) if existing is not None else None,
        existing_version_status=existing.status if existing is not None else None,
        would_create_version=existing is None,
        fingerprint_seconds=float(fingerprint.elapsed_seconds),
    )


def backfill_existing_source(
    *,
    db: Session,
    competitor_price_list_id: int,
    price_format_id: int | None = None,
    batch_size: int = 2000,
    enqueue_job: bool = True,
) -> BackfillResult:
    """Create/reuse one source version, lineage, snapshot and matching job.

    The caller owns the transaction.  No competitor source row, price,
    generated price list, calculated price, or stock row is modified.
    """

    source_id = int(competitor_price_list_id)
    created = create_or_reuse_legacy_source_version(
        db=db,
        competitor_price_list_id=source_id,
        source_metadata={
            "origin": "existing_source_backfill",
            "priceFormatId": int(price_format_id) if price_format_id is not None else None,
        },
        batch_size=batch_size,
    )
    version = created.version
    try:
        source_metadata = json.loads(version.source_metadata_json or "{}")
    except (TypeError, json.JSONDecodeError):
        source_metadata = {}
    if not isinstance(source_metadata, dict):
        source_metadata = {}
    source_metadata.update({
        "matchingDecisionMode": "authoritative_legacy_output",
        "origin": "existing_source_backfill",
        "priceFormatId": int(price_format_id) if price_format_id is not None else None,
    })
    version.source_metadata_json = json.dumps(
        source_metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    if version.status in {"pending", "failed"}:
        mark_source_version_matching(db=db, version_id=int(version.id))
        mark_source_version_ready(db=db, version_id=int(version.id))
        version = publish_source_version(db=db, version_id=int(version.id))
    elif version.status == "ready":
        version = publish_source_version(db=db, version_id=int(version.id))
    elif version.status == "superseded":
        mark_source_version_ready_for_republication(db=db, version_id=int(version.id))
        version = publish_source_version(db=db, version_id=int(version.id))
    elif version.status != "published":
        raise RuntimeError(
            f"source version {version.id} has non-recoverable status {version.status}"
        )

    publication = db.scalar(
        select(CompetitorSourcePublication).where(
            CompetitorSourcePublication.competitor_price_list_id == source_id,
            CompetitorSourcePublication.source_version_id == int(version.id),
            CompetitorSourcePublication.status == "published",
        )
    )
    publication_reused = publication is not None
    if publication is None:
        publication = publish_physical_source_lineage(
            db=db, source_version=version
        )
    elif int(publication.row_count) != int(version.row_count):
        raise RuntimeError(
            f"source publication {publication.id} row count does not match version {version.id}"
        )

    vector, _context = build_source_dependency_vector(
        db=db,
        competitor_price_list_id=source_id,
    )
    snapshot, snapshot_reused = get_or_create_shadow_snapshot(
        db=db, source_version_id=int(version.id), vector=vector
    )
    job: CompetitorMatchJob | None = None
    job_reused = False
    if enqueue_job and snapshot.status not in {"ready", "published"}:
        job, job_reused = enqueue_matching_job(
            db=db,
            source_version_id=int(version.id),
            dependency_fingerprint=vector.fingerprint,
            match_snapshot_id=int(snapshot.id),
        )
    return BackfillResult(
        source_id=source_id,
        row_count=int(version.row_count),
        content_sha256=version.content_sha256,
        source_version_id=int(version.id),
        source_version_reused=bool(created.reused),
        source_publication_id=int(publication.id),
        source_publication_reused=publication_reused,
        snapshot_id=int(snapshot.id),
        snapshot_reused=bool(snapshot_reused),
        snapshot_status=str(snapshot.status),
        job_id=int(job.id) if job is not None else None,
        job_reused=bool(job_reused),
        job_status=str(job.status) if job is not None else None,
    )
