from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.services.percentile_differential_publication import (
    prepare_emit_percentile_stage,
    publish_emit_percentile_stage,
)


def _wal_lsn(db: Session) -> str:
    return str(db.scalar(text("SELECT pg_current_wal_lsn()")))


def _wal_bytes(db: Session, before: str) -> int:
    return int(db.scalar(text("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), :before)"), {"before": before}) or 0)


def _rss_mib() -> float:
    try:
        import psutil  # type: ignore[import-not-found]

        value = int(psutil.Process().memory_info().rss)
    except ImportError:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            get_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
            get_memory_info.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ProcessMemoryCounters),
                wintypes.DWORD,
            ]
            get_memory_info.restype = wintypes.BOOL
            if not get_memory_info(
                handle, ctypes.byref(counters), counters.cb
            ):
                return 0.0
            value = int(counters.WorkingSetSize)
        else:
            import resource

            maximum = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            value = maximum if sys.platform == "darwin" else maximum * 1024
    return round(value / (1024 * 1024), 3)


def _start_rss_sampler() -> tuple[threading.Event, list[float], threading.Thread]:
    stop = threading.Event()
    peak = [_rss_mib()]

    def sample() -> None:
        while not stop.wait(0.01):
            peak[0] = max(peak[0], _rss_mib())

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    return stop, peak, thread


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthetic Phase 3 percentile publication benchmark")
    parser.add_argument("--database-url", default=os.getenv("TEST_POSTGRES_URL", ""))
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--confirm-isolated", action="store_true")
    args = parser.parse_args()
    if not args.confirm_isolated:
        raise SystemExit("--confirm-isolated is required")
    if not args.database_url:
        raise SystemExit("--database-url or TEST_POSTGRES_URL is required")
    engine = create_engine(args.database_url, poolclass=NullPool)
    schema = "phase3_percentile_bench_" + uuid.uuid4().hex
    results: list[dict] = []
    try:
        with engine.begin() as connection:
            version = int(connection.scalar(text("SHOW server_version_num")))
            if not 160000 <= version < 170000:
                raise SystemExit("PostgreSQL 16 is required")
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        scoped = create_engine(
            args.database_url,
            poolclass=NullPool,
            connect_args={"options": f"-csearch_path={schema},public"},
        )
        with Session(scoped) as db:
            db.execute(text("""
                CREATE TABLE competitor_price_percentiles (
                    id BIGSERIAL PRIMARY KEY,
                    price_format_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    competitor_price_list_id BIGINT,
                    source_type VARCHAR(32) NOT NULL DEFAULT '',
                    source_key TEXT NOT NULL DEFAULT '',
                    branch_name TEXT NOT NULL DEFAULT '',
                    competitor_name TEXT NOT NULL DEFAULT '',
                    percentile_scope VARCHAR(32) NOT NULL DEFAULT 'regional',
                    percentile INTEGER NOT NULL,
                    value NUMERIC(18,4),
                    source_count INTEGER NOT NULL DEFAULT 0,
                    price_count INTEGER NOT NULL DEFAULT 0,
                    used_price_count INTEGER NOT NULL DEFAULT 0,
                    status VARCHAR(64) NOT NULL DEFAULT '',
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (price_format_id,product_id,source_key,branch_name,
                            competitor_name,percentile_scope,percentile)
                )
            """))
            db.execute(text("""
                INSERT INTO competitor_price_percentiles (
                    price_format_id,product_id,competitor_price_list_id,
                    source_type,source_key,branch_name,competitor_name,
                    percentile_scope,percentile,value,source_count,price_count,
                    used_price_count,status
                )
                SELECT 1,g,1,'emit','emit:benchmark','Benchmark','Synthetic',
                       'regional',10,
                       CASE WHEN g % 17 = 0 THEN NULL ELSE g::numeric / 100 END,
                       1,2,2,'Calculated'
                FROM generate_series(1,:rows) g
            """), {"rows": args.rows})
            db.commit()

            for case_name, ratio, marker in (
                ("unchanged", 0.0, 0),
                ("changed_1_percent", 0.01, 1),
                ("changed_10_percent", 0.10, 2),
                ("changed_100_percent", 1.0, 3),
            ):
                prepare_emit_percentile_stage(db, table_name="tmp_benchmark_percentiles")
                changed = int(args.rows * ratio)
                db.execute(text("""
                    INSERT INTO tmp_benchmark_percentiles (
                        price_format_id,product_id,competitor_price_list_id,
                        source_type,source_key,branch_name,competitor_name,
                        percentile_scope,percentile,value,source_count,price_count,
                        used_price_count,status,updated_at
                    )
                    SELECT price_format_id,product_id,competitor_price_list_id,
                           source_type,source_key,branch_name,competitor_name,
                           percentile_scope,percentile,
                           CASE
                             WHEN product_id <= :changed AND value IS NOT NULL
                             THEN (product_id::numeric / 100) + :marker
                             WHEN product_id <= :changed THEN :marker
                             WHEN product_id % 17 = 0 THEN NULL
                             ELSE product_id::numeric / 100
                           END,
                           source_count,price_count,used_price_count,status,
                           CURRENT_TIMESTAMP
                    FROM competitor_price_percentiles
                """), {"changed": changed, "marker": marker})
                before_size = int(db.scalar(text("SELECT pg_total_relation_size('competitor_price_percentiles')")) or 0)
                before_temp = db.execute(text(
                    "SELECT temp_bytes,temp_files FROM pg_stat_database WHERE datname=current_database()"
                )).one()
                before_wal = _wal_lsn(db)
                started = time.perf_counter()
                rss_stop, peak_rss, rss_thread = _start_rss_sampler()
                stats = publish_emit_percentile_stage(
                    db,
                    table_name="tmp_benchmark_percentiles",
                    scope_sql="t.price_format_id = :price_format_id",
                    params={"price_format_id": 1},
                )
                db.commit()
                rss_stop.set()
                rss_thread.join(timeout=1)
                duration = time.perf_counter() - started
                with Session(scoped) as measured:
                    after_size = int(measured.scalar(text("SELECT pg_total_relation_size('competitor_price_percentiles')")) or 0)
                    after_temp = measured.execute(text(
                        "SELECT temp_bytes,temp_files FROM pg_stat_database WHERE datname=current_database()"
                    )).one()
                    wal = _wal_bytes(measured, before_wal)
                results.append({
                    "strategy": (
                        "adaptive_replacement"
                        if stats.replacement_used
                        else "differential"
                    ),
                    "case": case_name,
                    "synthetic_rows": args.rows,
                    "requested_changed_rows": changed,
                    "inserted": stats.inserted,
                    "updated": stats.updated,
                    "deleted": stats.deleted,
                    "unchanged": stats.unchanged,
                    "duration_seconds": round(duration, 3),
                    "wal_bytes": wal,
                    "temp_bytes_delta": int(after_temp.temp_bytes or 0) - int(before_temp.temp_bytes or 0),
                    "temp_files_delta": int(after_temp.temp_files or 0) - int(before_temp.temp_files or 0),
                    "relation_bytes_delta": after_size - before_size,
                    "peak_rss_mib": peak_rss[0],
                })

                prepare_emit_percentile_stage(db, table_name="tmp_benchmark_percentiles")
                db.execute(text("""
                    INSERT INTO tmp_benchmark_percentiles (
                        price_format_id,product_id,competitor_price_list_id,
                        source_type,source_key,branch_name,competitor_name,
                        percentile_scope,percentile,value,source_count,price_count,
                        used_price_count,status,updated_at
                    )
                    SELECT price_format_id,product_id,competitor_price_list_id,
                           source_type,source_key,branch_name,competitor_name,
                           percentile_scope,percentile,value,source_count,price_count,
                           used_price_count,status,updated_at
                    FROM competitor_price_percentiles
                    WHERE price_format_id=1
                """))
                before_size = int(db.scalar(text("SELECT pg_total_relation_size('competitor_price_percentiles')")) or 0)
                before_temp = db.execute(text(
                    "SELECT temp_bytes,temp_files FROM pg_stat_database WHERE datname=current_database()"
                )).one()
                before_wal = _wal_lsn(db)
                started = time.perf_counter()
                rss_stop, peak_rss, rss_thread = _start_rss_sampler()
                deleted = int(db.execute(text(
                    "DELETE FROM competitor_price_percentiles WHERE price_format_id=1"
                )).rowcount or 0)
                inserted = int(db.execute(text("""
                    INSERT INTO competitor_price_percentiles (
                        price_format_id,product_id,competitor_price_list_id,
                        source_type,source_key,branch_name,competitor_name,
                        percentile_scope,percentile,value,source_count,price_count,
                        used_price_count,status,updated_at
                    )
                    SELECT price_format_id,product_id,competitor_price_list_id,
                           source_type,source_key,branch_name,competitor_name,
                           percentile_scope,percentile,value,source_count,price_count,
                           used_price_count,status,updated_at
                    FROM tmp_benchmark_percentiles
                """ )).rowcount or 0)
                db.commit()
                rss_stop.set()
                rss_thread.join(timeout=1)
                duration = time.perf_counter() - started
                with Session(scoped) as measured:
                    after_size = int(measured.scalar(text("SELECT pg_total_relation_size('competitor_price_percentiles')")) or 0)
                    after_temp = measured.execute(text(
                        "SELECT temp_bytes,temp_files FROM pg_stat_database WHERE datname=current_database()"
                    )).one()
                    wal = _wal_bytes(measured, before_wal)
                results.append({
                    "strategy": "legacy_delete_insert",
                    "case": case_name,
                    "synthetic_rows": args.rows,
                    "requested_changed_rows": changed,
                    "inserted": inserted,
                    "updated": 0,
                    "deleted": deleted,
                    "unchanged": 0,
                    "duration_seconds": round(duration, 3),
                    "wal_bytes": wal,
                    "temp_bytes_delta": int(after_temp.temp_bytes or 0) - int(before_temp.temp_bytes or 0),
                    "temp_files_delta": int(after_temp.temp_files or 0) - int(before_temp.temp_files or 0),
                    "relation_bytes_delta": after_size - before_size,
                    "peak_rss_mib": peak_rss[0],
                })
        scoped.dispose()
        print(json.dumps({"dataset": "synthetic", "results": results}, indent=2, sort_keys=True))
        return 0
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
