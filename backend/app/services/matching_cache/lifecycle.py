from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import (
    CompetitorMatchJob,
    CompetitorMatchSnapshot,
    CompetitorSourcePublication,
    CompetitorSourceVersion,
    PriceFormatCompetitorMaterialization,
    PricingWorkflowRun,
)
from ..competitor_assignments import get_all_assigned_competitor_price_lists
from ..competitor_source_config import (
    MULTI_PRICE_PERCENTILE_MODE,
    effective_percentile_mode,
)
from .capture import capture_and_publish_legacy_source_version
from .config import (
    matching_cache_read_allowed_for_price_format,
    matching_cache_read_mode,
)
from .context import (
    MatchingContextConflict,
    build_source_dependency_vector,
    resolve_matching_contexts,
)
from .jobs import enqueue_matching_job
from .fingerprints import FINGERPRINT_FORMAT_VERSION
from .shadow import get_or_create_shadow_snapshot


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LifecycleResult:
    eligible_sources: int = 0
    captured_sources: int = 0
    snapshots_created: int = 0
    jobs_created: int = 0
    already_ready: int = 0
    conflicts: int = 0
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return asdict(self)


def eligible_source_ids(*, db: Session) -> tuple[int, ...]:
    return tuple(sorted({
        int(item.price_list.id)
        for item in get_all_assigned_competitor_price_lists(db=db)
        if effective_percentile_mode(
            item.price_list, item.assignment.percentile_mode
        )
        != MULTI_PRICE_PERCENTILE_MODE
    }))


def reconcile_matching_cache_lifecycle(*, db: Session) -> LifecycleResult:
    """Idempotently prepare current artifacts; caller owns the transaction.

    This runs only in the dedicated worker. Source capture and expensive shadow
    matching never execute in an HTTP request.
    """

    source_ids = eligible_source_ids(db=db)
    captured = snapshots_created = jobs_created = already_ready = conflicts = 0
    errors: list[str] = []
    for source_id in source_ids:
        try:
            version = db.scalar(
                select(CompetitorSourceVersion).where(
                    CompetitorSourceVersion.competitor_price_list_id == source_id,
                    CompetitorSourceVersion.status == "published",
                )
            )
            publication = None
            if version is not None:
                publication = db.scalar(
                    select(CompetitorSourcePublication).where(
                        CompetitorSourcePublication.competitor_price_list_id
                        == source_id,
                        CompetitorSourcePublication.source_version_id == int(version.id),
                        CompetitorSourcePublication.status == "published",
                    )
                )
            # Physical-row triggers invalidate the publication on every source
            # mutation. Re-capture only when lineage is absent/invalid instead
            # of hashing every large source on every idle worker poll.
            if (
                version is None
                or publication is None
                or publication.fingerprint_format_version
                != FINGERPRINT_FORMAT_VERSION
            ):
                outcome = capture_and_publish_legacy_source_version(
                    db=db,
                    competitor_price_list_id=source_id,
                    source_metadata={
                        "origin": "automatic_lifecycle_reconciliation",
                        "matchingDecisionMode": "shadow_rematch",
                    },
                    force_enabled=True,
                    enqueue_shadow=False,
                )
                if not outcome.succeeded or outcome.version_id is None:
                    raise RuntimeError(outcome.error or "automatic source capture failed")
                captured += 1
                version = db.get(CompetitorSourceVersion, int(outcome.version_id))
            if version is None:
                raise RuntimeError("published source version is missing after capture")
            for context in resolve_matching_contexts(
                db=db, competitor_price_list_id=source_id
            ):
                vector, _ = build_source_dependency_vector(
                    db=db,
                    competitor_price_list_id=source_id,
                    context_key=context.context_key,
                )
                snapshot, reused = get_or_create_shadow_snapshot(
                    db=db, source_version_id=int(version.id), vector=vector
                )
                if not reused:
                    snapshots_created += 1
                if snapshot.status in {"ready", "published"}:
                    already_ready += 1
                    continue
                job, job_reused = enqueue_matching_job(
                    db=db,
                    source_version_id=int(version.id),
                    dependency_fingerprint=vector.fingerprint,
                    matching_context_key=context.context_key,
                    match_snapshot_id=int(snapshot.id),
                )
                if not job_reused:
                    jobs_created += 1
                elif job.status == "failed" and int(job.attempt_count) < int(job.max_attempts):
                    job.status = "pending"
                    job.error = ""
        except MatchingContextConflict as exc:
            conflicts += 1
            errors.append(f"source {source_id}: {exc}")
        except Exception as exc:
            errors.append(f"source {source_id}: {exc}")
            logger.exception(
                "[MATCH_CACHE_LIFECYCLE_SOURCE_FAILED] source_id=%s", source_id
            )
    db.flush()
    return LifecycleResult(
        eligible_sources=len(source_ids),
        captured_sources=captured,
        snapshots_created=snapshots_created,
        jobs_created=jobs_created,
        already_ready=already_ready,
        conflicts=conflicts,
        errors=tuple(errors),
    )


def matching_cache_status(*, db: Session) -> dict:
    """Concise, read-only readiness for operators and admin APIs."""

    formats: dict[int, dict] = {}
    for assigned in get_all_assigned_competitor_price_lists(db=db):
        if effective_percentile_mode(
            assigned.price_list, assigned.assignment.percentile_mode
        ) == MULTI_PRICE_PERCENTILE_MODE:
            continue
        source = assigned.price_list
        format_id = int(assigned.assignment.price_format_id)
        row = formats.setdefault(format_id, {
            "priceFormatId": format_id,
            "ready": True,
            "sources": [],
        })
        source_status = {
            "sourceId": int(source.id),
            "ready": False,
            "reason": "source_version_missing",
        }
        try:
            vector, context = build_source_dependency_vector(
                db=db,
                competitor_price_list_id=int(source.id),
                price_format_id=format_id,
            )
            version = db.scalar(select(CompetitorSourceVersion).where(
                CompetitorSourceVersion.competitor_price_list_id == int(source.id),
                CompetitorSourceVersion.status == "published",
            ))
            if version is not None:
                snapshot = db.scalar(select(CompetitorMatchSnapshot).where(
                    CompetitorMatchSnapshot.source_version_id == int(version.id),
                    CompetitorMatchSnapshot.dependency_fingerprint == vector.fingerprint,
                ).order_by(CompetitorMatchSnapshot.id.desc()).limit(1))
                pending_jobs = int(db.query(CompetitorMatchJob).filter(
                    CompetitorMatchJob.source_version_id == int(version.id),
                    CompetitorMatchJob.matching_context_key == context.context_key,
                    CompetitorMatchJob.status.in_(("pending", "running")),
                ).count())
                source_status.update({
                    "sourceVersionId": int(version.id),
                    "snapshotId": int(snapshot.id) if snapshot is not None else None,
                    "snapshotStatus": snapshot.status if snapshot is not None else "missing",
                    "matcherVersion": snapshot.matcher_version if snapshot is not None else None,
                    "lastBuildAt": (
                        snapshot.ready_at.isoformat() if snapshot is not None and snapshot.ready_at else None
                    ),
                    "pendingJobs": pending_jobs,
                    "matchingSourceIds": list(context.source_ids),
                    "matchingContextKey": context.context_key,
                    "matchingPriceFormatIds": list(context.price_format_ids),
                })
                if snapshot is not None and snapshot.status in {"ready", "published"}:
                    source_status.update({"ready": True, "reason": "ready"})
                else:
                    source_status["reason"] = "snapshot_not_ready"
        except MatchingContextConflict as exc:
            source_status["reason"] = "incompatible_matching_cohort"
            source_status["detail"] = str(exc)
        row["sources"].append(source_status)
        row["ready"] = bool(row["ready"] and source_status["ready"])
    for format_id, row in formats.items():
        try:
            read_mode = matching_cache_read_mode()
            read_authorized = (
                read_mode != "disabled"
                and matching_cache_read_allowed_for_price_format(format_id)
            )
            config_error = None
        except ValueError as exc:
            read_mode = "invalid"
            read_authorized = False
            config_error = str(exc)
        row["readPolicy"] = {
            "mode": read_mode,
            "authorized": read_authorized,
            "configurationError": config_error,
        }
        current = db.scalar(
            select(PriceFormatCompetitorMaterialization)
            .where(
                PriceFormatCompetitorMaterialization.price_format_id == format_id,
                PriceFormatCompetitorMaterialization.is_current.is_(True),
            )
            .limit(1)
        )
        row["cacheHitMetrics"] = {
            "currentMaterializationId": int(current.id) if current is not None else None,
            "materializedRows": int(current.row_count) if current is not None else 0,
            "publishedAt": (
                current.published_at.isoformat()
                if current is not None and current.published_at is not None
                else None
            ),
        }
        recent_runs = db.scalars(
            select(PricingWorkflowRun)
            .where(PricingWorkflowRun.price_format_id == format_id)
            .order_by(PricingWorkflowRun.id.desc())
            .limit(20)
        )
        hits = fallbacks = observed = 0
        last_result: dict | None = None
        for run in recent_runs:
            try:
                snapshot = json.loads(run.run_snapshot_json or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            details = snapshot.get("matchingCache") if isinstance(snapshot, dict) else None
            if not isinstance(details, dict) or "hit" not in details:
                continue
            observed += 1
            if bool(details.get("hit")):
                hits += 1
            else:
                fallbacks += 1
            if last_result is None:
                last_result = {
                    "workflowRunId": int(run.id),
                    "hit": bool(details.get("hit")),
                    "reason": str(details.get("reason") or ""),
                }
        row["cacheHitMetrics"].update({
            "recentRunsObserved": observed,
            "cacheHits": hits,
            "legacyFallbacks": fallbacks,
            "lastResult": last_result,
        })
    return {
        "artifact": "matching_cache_readiness",
        "formats": [formats[key] for key in sorted(formats)],
    }
