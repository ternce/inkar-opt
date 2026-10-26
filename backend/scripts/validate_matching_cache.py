"""Read-only, machine-readable validation of current legacy/cache decisions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from sqlalchemy import select, text

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import SessionLocal
from backend.app.models import (
    CompetitorMatchSnapshot,
    CompetitorSourceVersion,
    PricingWorkflowRun,
)
from backend.app.services.matching_cache.capture import fingerprint_legacy_source
from backend.app.services.matching_cache.dependencies import build_dependency_vector
from backend.app.services.matching_cache.context import (
    build_source_dependency_vector,
    resolve_matching_contexts,
)
from backend.app.services.matching_cache.differential import (
    compare_legacy_and_shadow_matches,
    compare_legacy_and_shadow_materialization,
)
from backend.app.services.competitor_assignments import (
    get_assigned_competitor_price_lists,
)
from backend.app.services.competitor_source_config import (
    MULTI_PRICE_PERCENTILE_MODE,
    effective_percentile_mode,
)


def _ids(value: str) -> list[int]:
    try:
        values = [int(token.strip()) for token in value.split(",") if token.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("source IDs must be integers") from exc
    if not values or any(value <= 0 for value in values):
        raise argparse.ArgumentTypeError("positive source IDs are required")
    return list(dict.fromkeys(values))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-ids", required=True, type=_ids)
    parser.add_argument("--price-format-id", required=True, type=int)
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.price_format_id <= 0:
        parser.error("--price-format-id must be positive")

    reports: list[dict] = []
    exact = True
    snapshots_by_source_id: dict[int, int] = {}
    with SessionLocal() as db:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            ))
            db.execute(text("SET LOCAL statement_timeout = '5min'"))
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
        assigned_source_ids = sorted(
            int(item.price_list.id)
            for item in get_assigned_competitor_price_lists(
                db=db, price_format_id=args.price_format_id
            )
            if effective_percentile_mode(
                item.price_list, item.assignment.percentile_mode
            )
            != MULTI_PRICE_PERCENTILE_MODE
        )
        requested_source_ids = sorted(args.source_ids)
        assignment_exact = assigned_source_ids == requested_source_ids
        if not assignment_exact:
            exact = False
        base_vector = build_dependency_vector(db=db)
        for source_id in args.source_ids:
            vector, requested_context = build_source_dependency_vector(
                db=db,
                competitor_price_list_id=source_id,
                base_vector=base_vector,
                expected_source_ids=tuple(assigned_source_ids),
                price_format_id=int(args.price_format_id),
            )
            version = db.scalar(
                select(CompetitorSourceVersion).where(
                    CompetitorSourceVersion.competitor_price_list_id == source_id,
                    CompetitorSourceVersion.status == "published",
                )
            )
            if version is None:
                reports.append(
                    {"sourceId": source_id, "exact": False, "reason": "published_version_missing"}
                )
                exact = False
                continue
            active = fingerprint_legacy_source(
                db=db, competitor_price_list_id=source_id
            )
            content_exact = (
                active.content_sha256 == version.content_sha256
                and active.row_count == int(version.row_count)
            )
            snapshot = db.scalar(
                select(CompetitorMatchSnapshot)
                .where(
                    CompetitorMatchSnapshot.source_version_id == int(version.id),
                    CompetitorMatchSnapshot.dependency_fingerprint == vector.fingerprint,
                    CompetitorMatchSnapshot.status.in_(("ready", "published")),
                )
                .order_by(CompetitorMatchSnapshot.id.desc())
                .limit(1)
            )
            if snapshot is None:
                reports.append(
                    {
                        "sourceId": source_id,
                        "sourceVersionId": int(version.id),
                        "sourceContentExact": content_exact,
                        "exact": False,
                        "reason": "current_ready_snapshot_missing",
                    }
                )
                exact = False
                continue
            snapshots_by_source_id[source_id] = int(snapshot.id)
            source_contexts = resolve_matching_contexts(
                db=db, competitor_price_list_id=source_id
            )
            shared_context_format_ids = sorted({
                int(format_id)
                for source_context in source_contexts
                for format_id in source_context.price_format_ids
            })
            latest_legacy_run = None
            legacy_context_current = True
            if len(source_contexts) > 1:
                latest_legacy_run = db.scalar(
                    select(PricingWorkflowRun)
                    .where(
                        PricingWorkflowRun.price_format_id.in_(
                            shared_context_format_ids
                        ),
                        PricingWorkflowRun.status == "success",
                    )
                    .order_by(
                        PricingWorkflowRun.finished_at.desc().nulls_last(),
                        PricingWorkflowRun.id.desc(),
                    )
                    .limit(1)
                )
                legacy_context_current = (
                    latest_legacy_run is not None
                    and int(latest_legacy_run.price_format_id)
                    == int(args.price_format_id)
                )
            try:
                snapshot_stats = json.loads(snapshot.stats_json or "{}")
                snapshot_context_ids = sorted(
                    int(value)
                    for value in snapshot_stats["legacyMatchingContext"]["sourceIds"]
                )
                expected_context = {
                    **requested_context.identity_dict(),
                    "contextKey": requested_context.context_key,
                }
                context_exact = (
                    snapshot_stats["legacyMatchingContext"]
                    in (expected_context, requested_context.identity_dict())
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                snapshot_context_ids = []
                context_exact = False
            decision_report = compare_legacy_and_shadow_matches(
                db=db,
                competitor_price_list_id=source_id,
                snapshot_id=int(snapshot.id),
            ).as_dict()
            source_exact = (
                content_exact
                and context_exact
                and legacy_context_current
                and bool(decision_report["exact"])
            )
            reports.append(
                {
                    "sourceId": source_id,
                    "sourceVersionId": int(version.id),
                    "snapshotId": int(snapshot.id),
                    "dependencyFingerprint": vector.fingerprint,
                    "sourceContentExact": content_exact,
                    "activeSourceRows": active.row_count,
                    "snapshotInputRows": int(snapshot.input_row_count),
                    "matchingContextExact": context_exact,
                    "snapshotMatchingSourceIds": snapshot_context_ids,
                    "matchingContextKey": requested_context.context_key,
                    "legacyDecisionBaseline": {
                        "currentForRequestedFormat": legacy_context_current,
                        "sharedContextFormatIds": shared_context_format_ids,
                        "latestSuccessfulWorkflowId": (
                            int(latest_legacy_run.id)
                            if latest_legacy_run is not None else None
                        ),
                        "latestSuccessfulPriceFormatId": (
                            int(latest_legacy_run.price_format_id)
                            if latest_legacy_run is not None else None
                        ),
                    },
                    "exact": source_exact,
                    "decisionComparison": decision_report,
                }
            )
            exact = exact and source_exact

        materialization_report = None
        if len(snapshots_by_source_id) == len(requested_source_ids):
            materialization_report = compare_legacy_and_shadow_materialization(
                db=db,
                price_format_id=args.price_format_id,
                snapshots_by_source_id=snapshots_by_source_id,
            ).as_dict()
            exact = exact and bool(materialization_report["exact"])
        db.rollback()

    payload = {
        "artifact": "matching_cache_shadow_validation",
        "readOnly": True,
        "exact": exact,
        "priceFormatId": args.price_format_id,
        "assignmentComparison": {
            "exact": assignment_exact,
            "requestedSourceIds": requested_source_ids,
            "activeAssignedSourceIds": assigned_source_ids,
        },
        "validationCoverage": {
            "productId": True,
            "sourceItemId": True,
            "sourceGoodsId": True,
            "competitorPrice": True,
            "effectiveCoefficient": True,
            "matchType": True,
            "manualMatchingOverride": True,
            "manufacturerRestrictions": True,
            "duplicateMultiplicity": True,
        },
        "sourceCount": len(reports),
        "sources": reports,
        "materializationComparison": materialization_report,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if exact else 2


if __name__ == "__main__":
    raise SystemExit(main())
