from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import tempfile
import time
import uuid
import gc
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Any

from backend.app.services.emit_worker import EmitConfig, _delete_stage_files, parse_normalize_stage


def _stage_summary(stage_path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with closing(sqlite3.connect(str(stage_path))) as conn:
        conn.row_factory = sqlite3.Row
        total = int(conn.execute("SELECT COUNT(*) FROM stage_items").fetchone()[0] or 0)
        price_min, price_max, distinct_prices = conn.execute(
            "SELECT MIN(distributor_price), MAX(distributor_price), COUNT(DISTINCT distributor_price) FROM stage_items"
        ).fetchone()
        rows_seen_sum = int(conn.execute("SELECT COALESCE(SUM(rows_seen), 0) FROM stage_items").fetchone()[0] or 0)
        key_counts = {
            str(key or ""): int(count or 0)
            for key, count in conn.execute("SELECT key_type, COUNT(*) FROM stage_items GROUP BY key_type").fetchall()
        }
        last_rowid = 0
        while True:
            rows = conn.execute(
                """
                SELECT rowid, dedupe_key, key_type, quality_json, provisor_id, provisor_goods_id,
                       goods_id_text, variant_key, pack_signature, producer_key, rows_seen,
                       names_sample_json, producers_sample_json, price_min, price_max, filial_id,
                       name, reg_number, distributor_goods_name, distributor_goods_id,
                       distributor_price, stock, package_count, expiry_date, raw_name,
                       raw_manufacturer, raw_json
                FROM stage_items
                WHERE rowid > ?
                ORDER BY rowid
                LIMIT 5000
                """,
                (last_rowid,),
            ).fetchall()
            if not rows:
                break
            last_rowid = int(rows[-1]["rowid"])
            for row in rows:
                payload = [row[key] for key in row.keys()]
                digest.update(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
                digest.update(b"\n")
    return {
        "rows": total,
        "rows_seen_sum": rows_seen_sum,
        "key_type_counts": key_counts,
        "price_min": price_min,
        "price_max": price_max,
        "distinct_prices": int(distinct_prices or 0),
        "stage_sha256": digest.hexdigest(),
    }


def _delete_stage_files_with_retry(stage_path: Path) -> None:
    for attempt in range(5):
        try:
            gc.collect()
            _delete_stage_files(stage_path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Emit parse into a temporary SQLite stage.")
    parser.add_argument("--input", required=True, help="Already downloaded Emit JSON/NDJSON/CSV/XLSX file.")
    parser.add_argument("--filial-id", required=True, type=int)
    parser.add_argument("--filial-name", default="")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=10000)
    parser.add_argument("--temp-dir", default="")
    parser.add_argument("--keep-stage", action="store_true")
    parser.add_argument("--min-final-rows", type=int, default=1)
    args = parser.parse_args()

    source_path = Path(args.input).resolve()
    if not source_path.exists():
        raise SystemExit(f"Input does not exist: {source_path}")
    temp_root = Path(args.temp_dir).resolve() if args.temp_dir else Path(tempfile.mkdtemp(prefix="emit_bench_"))
    temp_root.mkdir(parents=True, exist_ok=True)
    durations: list[float] = []
    summaries: list[dict[str, Any]] = []
    for run_number in range(1, max(1, args.runs) + 1):
        stage_path = temp_root / f"emit_bench_{args.filial_id}_{run_number}_{uuid.uuid4().hex}.sqlite"
        config = EmitConfig(
            temp_dir=str(temp_root),
            min_free_disk_gb=0,
            min_final_rows=max(1, int(args.min_final_rows)),
            batch_insert_size=max(1, int(args.batch_size)),
            delete_temp_after_success=False,
        )
        started = time.perf_counter()
        stats = parse_normalize_stage(
            source_path=source_path,
            stage_db_path=stage_path,
            filial_id=args.filial_id,
            filial_name=args.filial_name or f"Emit International {args.filial_id}",
            config=config,
        )
        elapsed = time.perf_counter() - started
        durations.append(elapsed)
        summary = {
            "run": run_number,
            "elapsed_sec": round(elapsed, 3),
            "stats": stats.to_dict(),
            "stage": _stage_summary(stage_path),
            "stage_path": str(stage_path) if args.keep_stage else "",
        }
        summaries.append(summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        if not args.keep_stage:
            _delete_stage_files_with_retry(stage_path)
    aggregate = {
        "runs": len(durations),
        "elapsed_min_sec": round(min(durations), 3),
        "elapsed_median_sec": round(statistics.median(durations), 3),
        "elapsed_max_sec": round(max(durations), 3),
        "input": str(source_path),
        "filial_id": args.filial_id,
        "batch_size": args.batch_size,
        "checksums": [item["stage"]["stage_sha256"] for item in summaries],
    }
    print(json.dumps({"aggregate": aggregate}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
