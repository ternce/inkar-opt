from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect, text

from backend.app.db import engine
from backend.app.services.competitor_source_config import (
    EMIT_DISPLAY_NAMES_BY_FILIAL_ID,
    emit_display_aliases,
)
from backend.app.services.competitors.percentiles.sources import percentile_source_id


@dataclass(frozen=True)
class Operation:
    table: str
    filial_id: str
    count_sql: str
    update_sql: str
    params: dict[str, object]


def _distinct_expr(columns: tuple[str, ...]) -> str:
    return " OR ".join(f"{column} IS DISTINCT FROM :display_name" for column in columns)


def _saved_source_name_pairs(filial_id: str, display_name: str) -> list[dict[str, str]]:
    source_key = f"emit:{filial_id}"
    aliases = emit_display_aliases(filial_id)
    pairs: list[dict[str, str]] = []
    for percentile in (10, 20, 30, 40, 60):
        for scope in ("regional",):
            new_id = percentile_source_id(
                percentile_source="emit",
                price_format_id="__PF__",
                scope=scope,
                source_key=source_key,
                region=display_name,
                competitor=display_name,
                percentile=percentile,
            )
            for alias in aliases - {display_name}:
                old_id = percentile_source_id(
                    percentile_source="emit",
                    price_format_id="__PF__",
                    scope=scope,
                    source_key=source_key,
                    region=alias,
                    competitor=alias,
                    percentile=percentile,
                )
                old_pattern = f"percentile:%{old_id.removeprefix('__PF__')}"
                new_suffix = new_id.removeprefix("__PF__")
                pairs.append(
                    {
                        "old_pattern": old_pattern,
                        "old_suffix": old_id.removeprefix("__PF__"),
                        "new_suffix": new_suffix,
                    }
                )
    return pairs


def _operations(existing_tables: set[str]) -> list[Operation]:
    operations: list[Operation] = []
    for filial_id, display_name in EMIT_DISPLAY_NAMES_BY_FILIAL_ID.items():
        source_key = f"emit:{filial_id}"
        base_params = {"filial_id": filial_id, "source_key": source_key, "display_name": display_name}
        if "competitor_price_lists" in existing_tables:
            where = (
                "(source_key = :source_key OR (source_type = 'emit' AND external_price_list_id = :filial_id)) "
                f"AND ({_distinct_expr(('display_name', 'supplier', 'region', 'branch_name', 'competitor_name'))})"
            )
            operations.append(
                Operation(
                    table="competitor_price_lists",
                    filial_id=filial_id,
                    count_sql=f"SELECT count(*) FROM competitor_price_lists WHERE {where}",
                    update_sql=(
                        "UPDATE competitor_price_lists "
                        "SET display_name = :display_name, supplier = :display_name, region = :display_name, "
                        "branch_name = :display_name, competitor_name = :display_name "
                        f"WHERE {where}"
                    ),
                    params=dict(base_params),
                )
            )
        for table in ("competitor_price_percentiles", "competitor_price_percentile_source_summaries"):
            if table not in existing_tables:
                continue
            where = (
                "source_key = :source_key "
                "AND COALESCE(percentile_scope, 'regional') = 'regional' "
                f"AND ({_distinct_expr(('branch_name', 'competitor_name'))})"
            )
            operations.append(
                Operation(
                    table=table,
                    filial_id=filial_id,
                    count_sql=f"SELECT count(*) FROM {table} WHERE {where}",
                    update_sql=(
                        f"UPDATE {table} SET branch_name = :display_name, competitor_name = :display_name "
                        f"WHERE {where}"
                    ),
                    params=dict(base_params),
                )
            )
        if "competitors_prices" in existing_tables:
            for index, pair in enumerate(_saved_source_name_pairs(filial_id, display_name)):
                params = {
                    **base_params,
                    "old_pattern": pair["old_pattern"],
                    "old_suffix": pair["old_suffix"],
                    "new_suffix": pair["new_suffix"],
                }
                where = (
                    "product_id IS NULL "
                    "AND source_name LIKE :old_pattern "
                    "AND source_name IS DISTINCT FROM replace(source_name, :old_suffix, :new_suffix)"
                )
                operations.append(
                    Operation(
                        table="competitors_prices",
                        filial_id=filial_id,
                        count_sql=f"SELECT count(*) FROM competitors_prices WHERE {where}",
                        update_sql=(
                            "UPDATE competitors_prices "
                            "SET source_name = replace(source_name, :old_suffix, :new_suffix) "
                            f"WHERE {where}"
                        ),
                        params=params,
                    )
                )
    return operations


def run(*, apply: bool) -> dict[str, object]:
    inspector = inspect(engine)
    existing_tables = {name for name in inspector.get_table_names()}
    operations = _operations(existing_tables)
    report: dict[str, object] = {"apply": apply, "tables": {}, "total": 0}
    with engine.begin() as conn:
        for operation in operations:
            count = int(conn.execute(text(operation.count_sql), operation.params).scalar() or 0)
            table_report = report["tables"].setdefault(operation.table, {})
            filial_report = table_report.setdefault(operation.filial_id, {"matched": 0, "updated": 0})
            filial_report["matched"] += count
            report["total"] = int(report["total"]) + count
            if apply and count:
                result = conn.execute(text(operation.update_sql), operation.params)
                filial_report["updated"] += int(result.rowcount or 0)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill known Emit display names using technical source identity.")
    parser.add_argument("--apply", action="store_true", help="Apply updates. Without this flag the script is a dry-run.")
    args = parser.parse_args()
    print(json.dumps(run(apply=bool(args.apply)), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
