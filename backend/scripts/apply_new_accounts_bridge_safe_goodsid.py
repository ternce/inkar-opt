from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]
DIAGNOSTICS_DIR = BACKEND_DIR / "diagnostics"
INPUT_CSV = DIAGNOSTICS_DIR / "new_accounts_bridge_safe.csv"
EXPECTED_ROWS = 28


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def database_url_candidates() -> list[str]:
    raw = os.getenv("DATABASE_URL", "postgresql://apteka:apteka@127.0.0.1:5432/apteka")
    candidates = [raw]
    if "@postgres:" in raw:
        candidates.append(raw.replace("@postgres:", "@127.0.0.1:"))
    out: list[str] = []
    for url in candidates:
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://") and "+psycopg" not in url:
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        if url not in out:
            out.append(url)
    return out


def connect_engine():
    last_error: Exception | None = None
    for url in database_url_candidates():
        try:
            engine = create_engine(url, connect_args={"connect_timeout": 5})
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine, url
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Could not connect to PostgreSQL: {last_error}") from last_error


def txt(value: Any) -> str:
    return "" if value is None else str(value).strip()


def int_text(value: Any) -> str:
    raw = txt(value)
    if not raw:
        return ""
    return str(int(raw))


def split_ids(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[;|,]\s*", txt(value)) if part.strip()]


def read_candidates() -> list[dict[str, Any]]:
    if not INPUT_CSV.exists():
        raise RuntimeError(f"Input CSV not found: {INPUT_CSV}")
    with INPUT_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    load_env_file(ROOT_DIR / ".env")
    load_env_file(BACKEND_DIR / ".env")
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    rows = read_candidates()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_csv = DIAGNOSTICS_DIR / f"new_accounts_bridge_goodsid_fill_backup_{timestamp}.csv"
    backup_json = DIAGNOSTICS_DIR / f"new_accounts_bridge_goodsid_fill_backup_{timestamp}.json"

    preflight_errors: list[str] = []
    if len(rows) != EXPECTED_ROWS:
        preflight_errors.append(f"CSV row count is {len(rows)}, expected {EXPECTED_ROWS}")

    parsed: list[dict[str, Any]] = []
    by_product: dict[int, set[int]] = defaultdict(set)
    for index, row in enumerate(rows, start=2):
        classification = txt(row.get("classification"))
        if classification != "SAFE":
            preflight_errors.append(f"line {index}: classification is {classification!r}, expected SAFE")
        try:
            product_id = int(txt(row.get("product_id")))
        except Exception:
            preflight_errors.append(f"line {index}: invalid product_id={row.get('product_id')!r}")
            continue
        goods_id_raw = txt(row.get("goodsId"))
        goods_ids = split_ids(row.get("goodsIds") or goods_id_raw)
        if not goods_id_raw or not goods_id_raw.isdigit():
            preflight_errors.append(f"line {index}: invalid goodsId={goods_id_raw!r}")
            continue
        if len(set(goods_ids)) != 1 or goods_ids[0] != goods_id_raw:
            preflight_errors.append(f"line {index}: candidate does not have exactly one goodsId: goodsId={goods_id_raw!r}, goodsIds={goods_ids!r}")
            continue
        goods_id = int(goods_id_raw)
        by_product[product_id].add(goods_id)
        parsed.append(
            {
                **row,
                "product_id_int": product_id,
                "new_goods_id_int": goods_id,
            }
        )

    for product_id, goods_ids in by_product.items():
        if len(goods_ids) > 1:
            preflight_errors.append(f"product_id={product_id} maps to multiple goodsIds: {sorted(goods_ids)}")

    engine, db_url = connect_engine()
    report: dict[str, Any] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "database_url_used": db_url.replace("+psycopg", ""),
        "input_csv": str(INPUT_CSV),
        "backup_csv": str(backup_csv),
        "backup_json": str(backup_json),
        "candidate_rows_loaded": len(rows),
        "preflight_errors": preflight_errors,
        "read_write_scope": "products.provisor_goods_id only",
    }

    with engine.begin() as conn:
        before_total = conn.execute(text("SELECT count(*) FROM products")).scalar_one()
        before_mapped = conn.execute(text("SELECT count(*) FROM products WHERE provisor_goods_id IS NOT NULL")).scalar_one()
        product_ids = [row["product_id_int"] for row in parsed]
        products = {}
        if product_ids:
            for row in conn.execute(
                text(
                    """
                    SELECT id, code, name, provisor_goods_id
                    FROM products
                    WHERE id = ANY(:ids)
                    """
                ),
                {"ids": product_ids},
            ).mappings():
                products[int(row["id"])] = dict(row)

        eligible: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in parsed:
            product = products.get(row["product_id_int"])
            if product is None:
                skipped.append({"product_id": row["product_id"], "reason": "product_not_found"})
                preflight_errors.append(f"product_id={row['product_id']} not found")
                continue
            if txt(product["code"]) != txt(row.get("product_code")):
                skipped.append({"product_id": row["product_id"], "reason": "product_code_mismatch", "db_code": product["code"], "csv_code": row.get("product_code")})
                preflight_errors.append(f"product_id={row['product_id']} code mismatch db={product['code']!r} csv={row.get('product_code')!r}")
                continue
            if product["provisor_goods_id"] is not None:
                skipped.append(
                    {
                        "product_id": row["product_id"],
                        "product_code": row.get("product_code"),
                        "reason": "already_has_provisor_goods_id",
                        "existing_goods_id": product["provisor_goods_id"],
                    }
                )
                preflight_errors.append(f"product_id={row['product_id']} already has provisor_goods_id={product['provisor_goods_id']}")
                continue
            backup_row = {
                "product_id": row["product_id_int"],
                "product_code": product["code"],
                "product_name": product["name"],
                "old_provisor_goods_id": product["provisor_goods_id"],
                "new_provisor_goods_id": row["new_goods_id_int"],
                "pharmcenter_name": row.get("pharmcenter_name") or "",
                "provisor_fullName": row.get("provisor_fullNames") or row.get("provisor_fullName") or "",
                "source_account": row.get("source_accountLogins") or "",
                "source_filial": row.get("source_filialNames") or "",
                "source_price_list_id": row.get("source_price_list_ids") or "",
            }
            eligible.append({**row, "backup_row": backup_row})

        duplicate_product_rows = sum(count - 1 for count in Counter(row["product_id_int"] for row in parsed).values() if count > 1)
        goods_id_null_count = sum(1 for row in parsed if not txt(row.get("goodsId")))
        if preflight_errors:
            report.update(
                {
                    "status": "blocked_preflight",
                    "eligible_rows": len(eligible),
                    "skipped_rows": skipped,
                    "before_coverage": {
                        "mapped": before_mapped,
                        "total": before_total,
                        "coverage_percent": round(before_mapped / before_total * 100, 2) if before_total else 0,
                    },
                    "validation": {
                        "duplicate_product_rows": duplicate_product_rows,
                        "goodsId_null": goods_id_null_count,
                    },
                }
            )
            backup_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            raise RuntimeError("Preflight failed; no updates applied. See backup/report JSON.")

        backup_rows = [row["backup_row"] for row in eligible]
        backup_fields = [
            "product_id",
            "product_code",
            "product_name",
            "old_provisor_goods_id",
            "new_provisor_goods_id",
            "pharmcenter_name",
            "provisor_fullName",
            "source_account",
            "source_filial",
            "source_price_list_id",
        ]
        write_csv(backup_csv, backup_rows, backup_fields)

        update_results = []
        overwritten_attempts = 0
        for row in eligible:
            result = conn.execute(
                text(
                    """
                    UPDATE products
                    SET provisor_goods_id = :new_goods_id
                    WHERE id = :product_id
                      AND provisor_goods_id IS NULL
                    """
                ),
                {"new_goods_id": row["new_goods_id_int"], "product_id": row["product_id_int"]},
            )
            update_results.append(
                {
                    "product_id": row["product_id_int"],
                    "new_goods_id": row["new_goods_id_int"],
                    "rowcount": result.rowcount,
                }
            )
            if result.rowcount == 0:
                overwritten_attempts += 1

        after_total = conn.execute(text("SELECT count(*) FROM products")).scalar_one()
        after_mapped = conn.execute(text("SELECT count(*) FROM products WHERE provisor_goods_id IS NOT NULL")).scalar_one()
        post_rows = {
            int(row["id"]): dict(row)
            for row in conn.execute(
                text("SELECT id, provisor_goods_id FROM products WHERE id = ANY(:ids)"),
                {"ids": product_ids},
            ).mappings()
        }
        post_update_not_set = []
        for row in eligible:
            post = post_rows.get(row["product_id_int"])
            if not post or int(post["provisor_goods_id"] or 0) != row["new_goods_id_int"]:
                post_update_not_set.append(row["product_id_int"])

        newly_filled_goods_ids = sorted({row["new_goods_id_int"] for row in eligible})
        selected_rows = conn.execute(
            text(
                """
                SELECT DISTINCT i.provisor_goods_id
                FROM competitor_price_list_items i
                JOIN competitor_price_lists l ON l.id = i.price_list_id
                LEFT JOIN price_format_competitor_assignments a
                  ON a.competitor_price_list_id = l.id
                 AND a.is_active IS TRUE
                WHERE i.provisor_goods_id = ANY(:goods_ids)
                  AND (l.is_selected IS TRUE OR a.id IS NOT NULL)
                """
            ),
            {"goods_ids": newly_filled_goods_ids},
        ).scalars().all()
        present_selected = sorted({int(x) for x in selected_rows if x is not None})
        absent_selected = sorted(set(newly_filled_goods_ids) - set(present_selected))

        report.update(
            {
                "status": "updated",
                "candidate_rows_loaded": len(rows),
                "eligible_rows": len(eligible),
                "updated_rows": sum(1 for item in update_results if item["rowcount"] == 1),
                "skipped_rows_count": len(skipped) + sum(1 for item in update_results if item["rowcount"] == 0),
                "skipped_rows": skipped
                + [
                    {
                        "product_id": item["product_id"],
                        "reason": "guarded_update_rowcount_zero",
                    }
                    for item in update_results
                    if item["rowcount"] == 0
                ],
                "before_coverage": {
                    "mapped": before_mapped,
                    "total": before_total,
                    "coverage_percent": round(before_mapped / before_total * 100, 2) if before_total else 0,
                },
                "after_coverage": {
                    "mapped": after_mapped,
                    "total": after_total,
                    "coverage_percent": round(after_mapped / after_total * 100, 2) if after_total else 0,
                },
                "validation": {
                    "duplicate_product_rows": duplicate_product_rows,
                    "goodsId_null": goods_id_null_count,
                    "existing_goodsId_overwritten": overwritten_attempts,
                    "post_update_not_set": len(post_update_not_set),
                    "post_update_not_set_product_ids": post_update_not_set,
                },
                "estimate": {
                    "newly_filled_goodsIds": newly_filled_goods_ids,
                    "newly_filled_goodsIds_count": len(newly_filled_goods_ids),
                    "present_in_selected_competitor_lists": present_selected,
                    "present_in_selected_competitor_lists_count": len(present_selected),
                    "absent_from_selected_competitor_lists": absent_selected,
                    "absent_from_selected_competitor_lists_count": len(absent_selected),
                    "expected_competitor_coverage_gain": len(present_selected),
                    "expected_competitor_coverage_gain_note": "Counts filled goodsIds present in any currently selected competitor list or active price-format assignment; actual generated coverage depends on selected lists for the specific generated format.",
                },
            }
        )
        backup_json.write_text(json.dumps({"backup": backup_rows, "report": report}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
