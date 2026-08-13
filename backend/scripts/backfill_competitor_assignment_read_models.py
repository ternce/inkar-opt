from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import SessionLocal, init_db
from backend.app.services.competitor_read_models import backfill_competitor_assignment_read_models


def main() -> int:
    init_db()
    with SessionLocal() as db:
        result = backfill_competitor_assignment_read_models(db=db)
        db.commit()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
