from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import SessionLocal, init_db
from backend.app.services.provisor_plk_backfill import normalize_provisor_plk_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize account-scoped Provisor PLK rows into canonical plk:<external_id> rows.")
    parser.add_argument("--apply", action="store_true", help="Apply the merge. Omit for dry-run.")
    args = parser.parse_args()

    init_db()
    with SessionLocal() as db:
        report = normalize_provisor_plk_rows(db=db, apply=bool(args.apply))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
