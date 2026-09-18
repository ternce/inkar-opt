"""Idempotent manual matching task backfill/reconciliation.

Run from repository root after applying the schema migration:
python -m backend.scripts.reconcile_manual_matching_assignments --worker-a-id 1 --worker-b-id 2
"""

import argparse

from backend.app.db import SessionLocal
from backend.app.services.manual_matching_assignments import reconcile_manual_matching_assignments


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-a-id", type=int, required=True)
    parser.add_argument("--worker-b-id", type=int, required=True)
    args = parser.parse_args()
    with SessionLocal() as db:
        result = reconcile_manual_matching_assignments(db, (args.worker_a_id, args.worker_b_id))
    print(result)


if __name__ == "__main__":
    main()
