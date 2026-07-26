from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import find_dotenv, load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import PriceFormat
from backend.app.services.emit_worker import (
    EmitConfig,
    EmitStats,
    _STAGE_INSERT_SQL,
    _delete_stage_files,
    _stage_values,
    open_stage_db,
    replace_emit_price_list_from_staging,
)

load_dotenv(find_dotenv())


FINAL_COLUMNS = (
    "price_list_id",
    "provisor_id",
    "provisor_goods_id",
    "filial_id",
    "name",
    "reg_number",
    "distributor_goods_name",
    "distributor_goods_id",
    "distributor_price",
    "stock",
    "package_count",
    "expiry_date",
    "raw_name",
    "raw_manufacturer",
    "raw_json",
)


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is required for the DB replace benchmark.")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _assert_non_production(url: str, confirmation: str) -> None:
    if confirmation != "NON_PRODUCTION_DB":
        raise SystemExit("Refusing to run: pass --confirm-non-production NON_PRODUCTION_DB")
    parsed = urlparse(url)
    db_name = (parsed.path or "").lstrip("/").casefold()
    full = url.casefold()
    if "prod" in db_name or "production" in full:
        raise SystemExit("Refusing to run against a database URL that looks like production.")


def _make_stage(stage_path: Path, *, rows: int, filial_id: int, batch_size: int) -> dict[str, Any]:
    conn = open_stage_db(stage_path)
    started = time.perf_counter()
    try:
        for start in range(0, rows, batch_size):
            batch = []
            for index in range(start, min(rows, start + batch_size)):
                goods_id = index % max(1, rows // 4)
                price = 1000 + (index % 100) + 0.25
                item = {
                    "provisor_id": index + 1,
                    "provisor_goods_id": goods_id,
                    "filial_id": filial_id,
                    "name": f"Emit Bench Item {goods_id} N{(index % 20) + 1}",
                    "reg_number": "",
                    "distributor_goods_name": f"Emit Bench Item {goods_id} N{(index % 20) + 1}",
                    "distributor_goods_id": f"SKU-{goods_id % 100000}",
                    "distributor_price": price,
                    "stock": index % 50,
                    "package_count": (index % 10) + 1,
                    "expiry_date": "",
                    "raw_name": f"Emit Bench Item {goods_id} N{(index % 20) + 1}",
                    "raw_manufacturer": f"MAKER {index % 1000}",
                    "raw_json": json.dumps(
                        {"id": index + 1, "goodsId": goods_id, "goodsPrice": price},
                        ensure_ascii=False,
                        default=str,
                    ),
                    "variant_key": f"sku:sku-{goods_id % 100000}",
                    "pack_signature": f"box:{(index % 10) + 1}",
                    "producer_key": f"maker {index % 1000}",
                    "source_timestamp": "2026-07-25T00:00:00",
                }
                batch.append(_stage_values(item))
            conn.executemany(_STAGE_INSERT_SQL, batch)
            conn.commit()
        count = int(conn.execute("SELECT COUNT(*) FROM stage_items").fetchone()[0] or 0)
    finally:
        conn.close()
    return {"stage_rows": count, "stage_build_elapsed_sec": round(time.perf_counter() - started, 3)}


def _final_checksum(connection, *, price_list_id: int) -> dict[str, Any]:
    digest = hashlib.sha256()
    total = 0
    result = connection.execution_options(stream_results=True).execute(
        text(
            f"""
            SELECT {", ".join(FINAL_COLUMNS)}
            FROM competitor_price_list_items
            WHERE price_list_id = :price_list_id
            ORDER BY id
            """
        ),
        {"price_list_id": price_list_id},
    )
    for row in result:
        total += 1
        digest.update(json.dumps(list(row), ensure_ascii=False, default=str).encode("utf-8"))
        digest.update(b"\n")
    return {"final_rows": total, "final_sha256": digest.hexdigest()}


def _explain(connection, sql: str, params: dict[str, Any]) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql}"),
            params,
        ).fetchall()
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Emit SQLite -> PostgreSQL DB replace in an isolated schema.")
    parser.add_argument("--rows", type=int, default=750000)
    parser.add_argument("--old-rows", type=int, default=0)
    parser.add_argument("--filial-id", type=int, default=1106)
    parser.add_argument("--batch-size", type=int, default=10000)
    parser.add_argument("--keep-stage", action="store_true")
    parser.add_argument("--keep-schema", action="store_true")
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--confirm-non-production", required=True)
    args = parser.parse_args()

    url = _database_url()
    _assert_non_production(url, args.confirm_non_production)
    schema = f"emit_bench_{uuid.uuid4().hex}"
    temp_dir = Path(tempfile.mkdtemp(prefix="emit_db_replace_bench_"))
    stage_path = temp_dir / "stage.sqlite"
    stage_summary = _make_stage(stage_path, rows=args.rows, filial_id=args.filial_id, batch_size=args.batch_size)
    engine = create_engine(url)
    explain_output: dict[str, list[str]] = {}
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            conn.execute(text(f'SET search_path TO "{schema}"'))
            Base.metadata.create_all(bind=conn)
            Session = sessionmaker(bind=conn)
            with Session() as db:
                db.add(PriceFormat(code="BENCH", name="Benchmark"))
                db.commit()
                old_price_list_id = None
                if args.old_rows > 0:
                    old_stats = EmitStats()
                    old_stage = temp_dir / "old_stage.sqlite"
                    _make_stage(old_stage, rows=args.old_rows, filial_id=args.filial_id, batch_size=args.batch_size)
                    old_row = replace_emit_price_list_from_staging(
                        db=db,
                        config=EmitConfig(temp_dir=str(temp_dir), min_free_disk_gb=0, min_final_rows=1, batch_insert_size=args.batch_size),
                        filial_id=args.filial_id,
                        filial_name=f"Emit International {args.filial_id}",
                        staging_path=old_stage,
                        stats=old_stats,
                        price_format_code="BENCH",
                    )
                    old_price_list_id = int(old_row.id)
                    _delete_stage_files(old_stage)
                stats = EmitStats()
                started = time.perf_counter()
                row = replace_emit_price_list_from_staging(
                    db=db,
                    config=EmitConfig(temp_dir=str(temp_dir), min_free_disk_gb=0, min_final_rows=1, batch_insert_size=args.batch_size),
                    filial_id=args.filial_id,
                    filial_name=f"Emit International {args.filial_id}",
                    staging_path=stage_path,
                    stats=stats,
                    price_format_code="BENCH",
                )
                elapsed = round(time.perf_counter() - started, 3)
                price_list_id = int(row.id)
                checksum = _final_checksum(conn, price_list_id=price_list_id)
                if args.explain:
                    explain_output["delete_by_price_list_id"] = _explain(
                        conn,
                        "DELETE FROM competitor_price_list_items WHERE price_list_id = :price_list_id",
                        {"price_list_id": -1},
                    )
                    explain_output["validation_count"] = _explain(
                        conn,
                        "SELECT COUNT(*) FROM competitor_price_list_items WHERE price_list_id = :price_list_id",
                        {"price_list_id": price_list_id},
                    )
            payload = {
                "schema": schema,
                "stage": stage_summary,
                "old_price_list_id": old_price_list_id,
                "replace_elapsed_sec": elapsed,
                "stats": stats.to_dict(),
                "checksum": checksum,
                "explain": explain_output,
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            if not args.keep_schema:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                conn.commit()
    finally:
        if not args.keep_stage:
            _delete_stage_files(stage_path)
        engine.dispose()


if __name__ == "__main__":
    main()
