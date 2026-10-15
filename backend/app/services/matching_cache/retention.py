from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import json
import os
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import (
    CompetitorMatchJob,
    CompetitorMatchSnapshot,
    CompetitorMatchSnapshotItem,
    CompetitorSourcePublication,
    CompetitorSourcePublicationItem,
    CompetitorSourceVersion,
    CompetitorSourceVersionItem,
    PriceFormatCompetitorMaterialization,
    PricingWorkflowRun,
)
from ...timezone import now_kz_naive


@dataclass(frozen=True)
class RetentionPolicy:
    source_version_days: int = 90
    match_snapshot_days: int = 90
    publication_days: int = 30
    failed_job_days: int = 30

    @classmethod
    def from_environment(cls) -> "RetentionPolicy":
        def days(name: str, default: int) -> int:
            raw = int(os.getenv(name, str(default)))
            if raw < 1:
                raise ValueError(f"{name} must be at least one day")
            return raw

        return cls(
            source_version_days=days("MATCH_CACHE_RETENTION_SOURCE_VERSION_DAYS", 90),
            match_snapshot_days=days("MATCH_CACHE_RETENTION_SNAPSHOT_DAYS", 90),
            publication_days=days("MATCH_CACHE_RETENTION_PUBLICATION_DAYS", 30),
            failed_job_days=days("MATCH_CACHE_RETENTION_FAILED_JOB_DAYS", 30),
        )


@dataclass
class RetentionManifest:
    generated_at: str
    dry_run: bool
    policy: dict[str, int]
    blocked: bool = False
    blockers: list[str] = field(default_factory=list)
    protected: dict[str, list[int]] = field(default_factory=dict)
    candidates: dict[str, list[int]] = field(default_factory=dict)
    candidate_rows: dict[str, int] = field(default_factory=dict)
    estimated_payload_bytes: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_VERSION_KEYS = {"sourceversionid", "sourceversionids"}
_SNAPSHOT_KEYS = {
    "matchsnapshotid",
    "matchsnapshotids",
    "sourcesnapshotid",
    "sourcesnapshotids",
}


def _collect_ids(value: Any, *, versions: set[int], snapshots: set[int]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = "".join(ch for ch in str(key).casefold() if ch.isalnum())
            if normalized in _VERSION_KEYS:
                values = nested if isinstance(nested, list) else [nested]
                versions.update(int(item) for item in values if str(item).isdigit())
            elif normalized in _SNAPSHOT_KEYS:
                values = nested if isinstance(nested, list) else [nested]
                snapshots.update(int(item) for item in values if str(item).isdigit())
            _collect_ids(nested, versions=versions, snapshots=snapshots)
    elif isinstance(value, list):
        for nested in value:
            _collect_ids(nested, versions=versions, snapshots=snapshots)


def _audit_references(db: Session) -> tuple[set[int], set[int], list[str]]:
    versions: set[int] = set()
    snapshots: set[int] = set()
    blockers: list[str] = []
    documents: list[tuple[str, int, str]] = []
    for row in db.execute(
        select(
            PriceFormatCompetitorMaterialization.id,
            PriceFormatCompetitorMaterialization.source_snapshot_ids_json,
        )
    ):
        documents.append(("materialization", int(row.id), row.source_snapshot_ids_json or "[]"))
    for row in db.execute(
        select(
            PricingWorkflowRun.id,
            PricingWorkflowRun.run_snapshot_json,
            PricingWorkflowRun.run_sources_json,
            PricingWorkflowRun.run_reference_versions_json,
            PricingWorkflowRun.analytics_json,
            PricingWorkflowRun.competitor_sources_json,
        )
    ):
        for raw in (
            row.run_snapshot_json,
            row.run_sources_json,
            row.run_reference_versions_json,
            row.analytics_json,
            row.competitor_sources_json,
        ):
            documents.append(("workflow", int(row.id), raw or "{}"))
    for kind, record_id, raw in documents:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            blockers.append(f"malformed_{kind}_reference:{record_id}")
            continue
        if kind == "materialization" and isinstance(parsed, list):
            snapshots.update(int(item) for item in parsed if str(item).isdigit())
        _collect_ids(parsed, versions=versions, snapshots=snapshots)
    return versions, snapshots, blockers


def build_retention_manifest(
    *,
    db: Session,
    policy: RetentionPolicy | None = None,
    now: datetime | None = None,
) -> RetentionManifest:
    """Mark immutable cache artifacts without deleting anything.

    Historical workflow/materialization JSON is treated as an audit reference.
    Malformed reference JSON blocks the complete sweep because absence of a
    reference cannot then be proven.
    """

    policy = policy or RetentionPolicy.from_environment()
    current_time = now or now_kz_naive()
    manifest = RetentionManifest(
        generated_at=current_time.isoformat(),
        dry_run=True,
        policy=asdict(policy),
    )
    referenced_versions, referenced_snapshots, blockers = _audit_references(db)
    if blockers:
        manifest.blocked = True
        manifest.blockers.extend(sorted(blockers))

    current_versions = set(
        db.scalars(
            select(CompetitorSourceVersion.id).where(
                CompetitorSourceVersion.status == "published"
            )
        )
    )
    current_publications = set(
        db.scalars(
            select(CompetitorSourcePublication.id).where(
                CompetitorSourcePublication.status == "published"
            )
        )
    )
    job_cutoff = current_time - timedelta(days=policy.failed_job_days)
    candidate_jobs = list(
        db.scalars(
            select(CompetitorMatchJob).where(
                CompetitorMatchJob.created_at < job_cutoff,
                CompetitorMatchJob.status.in_(("failed", "cancelled")),
            )
        )
    )
    candidate_job_ids = {int(row.id) for row in candidate_jobs}
    retained_jobs = list(
        db.scalars(
            select(CompetitorMatchJob).where(
                CompetitorMatchJob.id.not_in(candidate_job_ids or {-1})
            )
        )
    )
    job_version_ids = {int(row.source_version_id) for row in retained_jobs}
    job_snapshot_ids = {
        int(row.match_snapshot_id)
        for row in retained_jobs
        if row.match_snapshot_id is not None
    }
    protected_versions = {int(value) for value in current_versions | job_version_ids | referenced_versions}
    protected_snapshots = {int(value) for value in job_snapshot_ids | referenced_snapshots}

    snapshot_cutoff = current_time - timedelta(days=policy.match_snapshot_days)
    snapshot_candidates = {
        int(row.id)
        for row in db.scalars(
            select(CompetitorMatchSnapshot).where(
                CompetitorMatchSnapshot.created_at < snapshot_cutoff,
                CompetitorMatchSnapshot.status.in_(("failed", "superseded")),
            )
        )
        if int(row.id) not in protected_snapshots
    }
    version_cutoff = current_time - timedelta(days=policy.source_version_days)
    version_candidates = {
        int(row.id)
        for row in db.scalars(
            select(CompetitorSourceVersion).where(
                CompetitorSourceVersion.created_at < version_cutoff,
                CompetitorSourceVersion.status.in_(("failed", "superseded")),
            )
        )
        if int(row.id) not in protected_versions
    }
    publication_cutoff = current_time - timedelta(days=policy.publication_days)
    publication_candidates = set(
        int(value)
        for value in db.scalars(
            select(CompetitorSourcePublication.id).where(
                CompetitorSourcePublication.created_at < publication_cutoff,
                CompetitorSourcePublication.status.in_(("failed", "superseded")),
            )
        )
        if int(value) not in current_publications
    )

    # A version cannot be swept while any non-candidate child remains.
    blocked_version_ids = set(
        int(value)
        for value in db.scalars(
            select(CompetitorMatchSnapshot.source_version_id).where(
                CompetitorMatchSnapshot.source_version_id.in_(version_candidates),
                CompetitorMatchSnapshot.id.not_in(snapshot_candidates or {-1}),
            )
        )
    )
    blocked_version_ids.update(
        int(value)
        for value in db.scalars(
            select(CompetitorSourcePublication.source_version_id).where(
                CompetitorSourcePublication.source_version_id.in_(version_candidates),
                CompetitorSourcePublication.id.not_in(publication_candidates or {-1}),
            )
        )
    )
    version_candidates.difference_update(blocked_version_ids)

    if manifest.blocked:
        candidate_job_ids.clear()
        snapshot_candidates.clear()
        version_candidates.clear()
        publication_candidates.clear()

    manifest.protected = {
        "source_version_ids": sorted(protected_versions | blocked_version_ids),
        "match_snapshot_ids": sorted(protected_snapshots),
        "source_publication_ids": sorted(int(value) for value in current_publications),
    }
    manifest.candidates = {
        "source_version_ids": sorted(version_candidates),
        "match_snapshot_ids": sorted(snapshot_candidates),
        "source_publication_ids": sorted(publication_candidates),
        "match_job_ids": sorted(candidate_job_ids),
    }

    def count(model, column, ids: set[int]) -> int:
        if not ids:
            return 0
        return int(db.scalar(select(func.count()).select_from(model).where(column.in_(ids))) or 0)

    manifest.candidate_rows = {
        "source_versions": len(version_candidates),
        "source_version_items": count(
            CompetitorSourceVersionItem,
            CompetitorSourceVersionItem.source_version_id,
            version_candidates,
        ),
        "match_snapshots": len(snapshot_candidates),
        "match_snapshot_items": count(
            CompetitorMatchSnapshotItem,
            CompetitorMatchSnapshotItem.match_snapshot_id,
            snapshot_candidates,
        ),
        "source_publications": len(publication_candidates),
        "source_publication_items": count(
            CompetitorSourcePublicationItem,
            CompetitorSourcePublicationItem.source_publication_id,
            publication_candidates,
        ),
        "match_jobs": len(candidate_job_ids),
    }
    # Exact reclaimable disk space requires a real sweep plus VACUUM. This
    # payload estimate is intentionally conservative and excludes index pages.
    manifest.estimated_payload_bytes = {
        key: rows * 256 for key, rows in manifest.candidate_rows.items()
    }
    manifest.blockers.append(
        "physical_deletion_not_implemented; manifest is dry-run only"
    )
    return manifest
