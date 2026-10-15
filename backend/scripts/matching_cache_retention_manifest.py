from __future__ import annotations

import argparse
import json

from backend.app.db import SessionLocal
from backend.app.services.matching_cache.retention import (
    RetentionPolicy,
    build_retention_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Produce a fail-closed, non-destructive matching-cache retention manifest."
    )
    parser.add_argument("--source-version-days", type=int, default=90)
    parser.add_argument("--snapshot-days", type=int, default=90)
    parser.add_argument("--publication-days", type=int, default=30)
    parser.add_argument("--failed-job-days", type=int, default=30)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Reserved safety switch; physical deletion is intentionally unavailable.",
    )
    args = parser.parse_args()
    if args.apply:
        raise SystemExit("physical retention deletion is disabled; run without --apply")
    policy = RetentionPolicy(
        source_version_days=args.source_version_days,
        match_snapshot_days=args.snapshot_days,
        publication_days=args.publication_days,
        failed_job_days=args.failed_job_days,
    )
    with SessionLocal() as db:
        manifest = build_retention_manifest(db=db, policy=policy)
    print(json.dumps(manifest.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
