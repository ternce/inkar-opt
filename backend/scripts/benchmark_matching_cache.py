"""Non-destructive Phase 2B.3 benchmark against an isolated PostgreSQL source.

By default the cache artifacts are rolled back.  The legacy price-list rows are
never changed permanently because shadow matching itself uses a savepoint.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import threading
import tempfile
import time
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import Session

from backend.app.models import (
    CompetitorMatchSnapshot,
    CompetitorMatchSnapshotItem,
    CompetitorPriceListItem,
    CompetitorSourceVersion,
    CompetitorSourceVersionItem,
)
from backend.app.services.matching_cache.capture import (
    _postgres_io_sample,
    _wal_bytes_between,
    capture_and_publish_legacy_source_version,
    fingerprint_legacy_source,
)
from backend.app.services.matching_cache.dependencies import build_dependency_vector
from backend.app.services.matching_cache.shadow import build_shadow_snapshot, get_or_create_shadow_snapshot


def _rss_bytes() -> int | None:
    try:
        import psutil  # type: ignore[import-not-found]

        return int(psutil.Process().memory_info().rss)
    except ImportError:
        if sys.platform == "win32":
            try:
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
                if get_memory_info(
                    handle, ctypes.byref(counters), counters.cb
                ):
                    return int(counters.WorkingSetSize)
            except Exception:
                return None
        try:
            import resource

            maximum = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            return maximum if sys.platform == "darwin" else maximum * 1024
        except (ImportError, AttributeError):
            return None


_POSTGRES_LOAD_FIELDS = (
    "xact_commit",
    "xact_rollback",
    "blks_read",
    "blks_hit",
    "tup_returned",
    "tup_fetched",
    "tup_inserted",
    "tup_updated",
    "tup_deleted",
    "temp_files",
    "temp_bytes",
    "deadlocks",
)


def _postgres_load_sample(connection) -> dict[str, int]:
    row = connection.execute(text(
        "SELECT " + ", ".join(_POSTGRES_LOAD_FIELDS)
        + " FROM pg_stat_database WHERE datname = current_database()"
    )).one()
    return {
        field: int(value or 0)
        for field, value in zip(_POSTGRES_LOAD_FIELDS, row)
    }


@contextmanager
def _peak_rss_sampler() -> Iterator[dict[str, int | None]]:
    sample: dict[str, int | None] = {
        "start": _rss_bytes(),
        "peak": _rss_bytes(),
        "shadowSpoolPeakBytes": 0,
        "shadowSpoolPeakFiles": 0,
    }
    stopped = threading.Event()

    def poll() -> None:
        while not stopped.wait(0.05):
            value = _rss_bytes()
            if value is not None:
                sample["peak"] = max(int(sample["peak"] or 0), value)
            spool_paths = list(Path(tempfile.gettempdir()).glob("match-shadow-*-*.sqlite3"))
            spool_sizes = []
            for path in spool_paths:
                try:
                    spool_sizes.append(path.stat().st_size)
                except FileNotFoundError:
                    pass
            sample["shadowSpoolPeakBytes"] = max(
                int(sample["shadowSpoolPeakBytes"] or 0), sum(spool_sizes)
            )
            sample["shadowSpoolPeakFiles"] = max(
                int(sample["shadowSpoolPeakFiles"] or 0), len(spool_sizes)
            )

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    try:
        yield sample
    finally:
        stopped.set()
        thread.join(timeout=1)
        sample["end"] = _rss_bytes()


def run(*, database_url: str, source_id: int, expected_rows: int | None, commit: bool) -> dict:
    engine = create_engine(database_url, pool_size=2, max_overflow=0)
    pool_usage = {"checkedOut": 0, "peakCheckedOut": 0, "checkouts": 0}

    @event.listens_for(engine, "checkout")
    def _record_checkout(*_args) -> None:
        pool_usage["checkedOut"] += 1
        pool_usage["checkouts"] += 1
        pool_usage["peakCheckedOut"] = max(
            pool_usage["peakCheckedOut"], pool_usage["checkedOut"]
        )

    @event.listens_for(engine, "checkin")
    def _record_checkin(*_args) -> None:
        pool_usage["checkedOut"] = max(0, pool_usage["checkedOut"] - 1)

    os.environ["MATCH_CACHE_ENABLED"] = "true"
    os.environ["MATCH_CACHE_DUAL_WRITE"] = "true"
    os.environ["MATCH_CACHE_SHADOW_ENABLED"] = "false"
    os.environ["MATCH_CACHE_READ"] = "false"
    result: dict = {"sourceId": source_id, "committedCacheArtifacts": commit}
    try:
        with engine.connect() as connection:
            load_before = _postgres_load_sample(connection)
        wall_started = time.perf_counter()
        cpu_started = time.process_time()
        with Session(engine) as db, _peak_rss_sampler() as memory:
            row_count = int(db.scalar(select(func.count(CompetitorPriceListItem.id)).where(
                CompetitorPriceListItem.price_list_id == source_id
            )) or 0)
            if expected_rows is not None and row_count != expected_rows:
                raise RuntimeError(f"expected {expected_rows} source rows, found {row_count}")
            result["sourceRows"] = row_count
            connections_before = int(db.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"
            )) or 0)
            io_before = _postgres_io_sample(db)

            started = time.perf_counter()
            fingerprint = fingerprint_legacy_source(db=db, competitor_price_list_id=source_id)
            result["fingerprintSeconds"] = time.perf_counter() - started
            result["contentSha256"] = fingerprint.content_sha256

            started = time.perf_counter()
            capture = capture_and_publish_legacy_source_version(
                db=db, competitor_price_list_id=source_id, source_metadata={"benchmark": "phase2b3"}
            )
            result["sourceVersionSeconds"] = time.perf_counter() - started
            if not capture.succeeded or capture.version_id is None:
                raise RuntimeError(f"source capture failed: {capture.error}")
            result["sourceVersionReused"] = capture.reused
            started = time.perf_counter()
            repeated_capture = capture_and_publish_legacy_source_version(
                db=db,
                competitor_price_list_id=source_id,
                source_metadata={"benchmark": "phase2b3-repeat"},
            )
            result["sourceVersionReuseSeconds"] = time.perf_counter() - started
            result["sourceVersionRepeatReused"] = repeated_capture.reused
            vector = build_dependency_vector(db=db)
            snapshot, snapshot_reused = get_or_create_shadow_snapshot(
                db=db, source_version_id=capture.version_id, vector=vector
            )

            started = time.perf_counter()
            shadow = build_shadow_snapshot(
                db=db,
                snapshot_id=int(snapshot.id),
                expected_dependency_fingerprint=vector.fingerprint,
            )
            result["shadowSeconds"] = time.perf_counter() - started
            result["shadowSnapshotReused"] = snapshot_reused and shadow.duration_seconds == 0

            lookup_times: list[float] = []
            ready = None
            for _ in range(100):
                started = time.perf_counter()
                ready = db.scalar(select(CompetitorMatchSnapshot.id).where(
                    CompetitorMatchSnapshot.source_version_id == capture.version_id,
                    CompetitorMatchSnapshot.dependency_fingerprint == vector.fingerprint,
                    CompetitorMatchSnapshot.status == "ready",
                ))
                lookup_times.append((time.perf_counter() - started) * 1000)
            ordered_lookup = sorted(lookup_times)
            result["readyLookupMilliseconds"] = {
                "iterations": len(lookup_times),
                "min": ordered_lookup[0],
                "median": ordered_lookup[len(ordered_lookup) // 2],
                "p95": ordered_lookup[int(len(ordered_lookup) * 0.95) - 1],
                "max": ordered_lookup[-1],
            }
            result["readySnapshotId"] = ready
            db.flush()

            result["storageBytes"] = {}
            for label, relation in {
                "sourceVersions": "competitor_source_versions",
                "sourceVersionItems": "competitor_source_version_items",
                "matchSnapshots": "competitor_match_snapshots",
                "matchSnapshotItems": "competitor_match_snapshot_items",
            }.items():
                result["storageBytes"][label] = {
                    "table": int(db.scalar(text(
                        "SELECT pg_table_size(CAST(:relation AS regclass))"
                    ), {"relation": relation}) or 0),
                    "indexes": int(db.scalar(text(
                        "SELECT pg_indexes_size(CAST(:relation AS regclass))"
                    ), {"relation": relation}) or 0),
                    "total": int(db.scalar(text(
                        "SELECT pg_total_relation_size(CAST(:relation AS regclass))"
                    ), {"relation": relation}) or 0),
                }
            result["persistedCounts"] = {
                "sourceVersionItems": int(db.scalar(select(func.count(CompetitorSourceVersionItem.id)).where(
                    CompetitorSourceVersionItem.source_version_id == capture.version_id
                )) or 0),
                "snapshotItems": int(db.scalar(select(func.count(CompetitorMatchSnapshotItem.id)).where(
                    CompetitorMatchSnapshotItem.match_snapshot_id == snapshot.id
                )) or 0),
            }
            io_after = _postgres_io_sample(db)
            result["walBytesApprox"] = (
                _wal_bytes_between(db, io_before[0], io_after[0]) if io_before and io_after else None
            )
            result["postgresTempBytesApprox"] = (
                max(0, io_after[1] - io_before[1]) if io_before and io_after else None
            )
            result["databaseConnectionsBefore"] = connections_before
            result["databaseConnectionsAfter"] = int(db.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()"
            )) or 0)
            if commit:
                db.commit()
            else:
                db.rollback()
            # PostgreSQL normally publishes per-backend statistics at an
            # interval.  Force this benchmark backend to flush its completed
            # transaction so the immediately following delta is measurable.
            db.execute(text("SELECT pg_stat_force_next_flush()"))
            db.commit()
        result["totalSeconds"] = time.perf_counter() - wall_started
        result["processCpuSeconds"] = time.process_time() - cpu_started
        result["logicalCpuCount"] = os.cpu_count()
        result["averageProcessCpuCores"] = (
            result["processCpuSeconds"] / result["totalSeconds"]
            if result["totalSeconds"] else None
        )
        with engine.connect() as connection:
            load_after = _postgres_load_sample(connection)
        result["postgresLoadDelta"] = {
            field: max(0, load_after[field] - load_before[field])
            for field in _POSTGRES_LOAD_FIELDS
        }
        result["applicationPool"] = {
            "configuredSize": 2,
            "configuredMaxOverflow": 0,
            "peakCheckedOut": pool_usage["peakCheckedOut"],
            "totalCheckouts": pool_usage["checkouts"],
        }
        result["processRssBytes"] = memory
        return result
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.getenv("TEST_POSTGRES_URL", ""))
    parser.add_argument("--source-id", type=int, required=True)
    parser.add_argument("--expected-rows", type=int)
    parser.add_argument("--commit-cache-artifacts", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("--database-url or TEST_POSTGRES_URL is required")
    report = run(
        database_url=args.database_url,
        source_id=args.source_id,
        expected_rows=args.expected_rows,
        commit=args.commit_cache_artifacts,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
