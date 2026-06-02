from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


ROOT_DIR = Path(__file__).resolve().parents[2]
DIAGNOSTICS_DIR = ROOT_DIR / "backend" / "diagnostics"
DATABASE_URL = "postgresql+psycopg://apteka:apteka@127.0.0.1:5432/apteka"
SAFE_CANDIDATES_CSV = DIAGNOSTICS_DIR / "pharmcenter_provisor_name_bridge_safe_candidates.csv"
EXPECTED_ROWS = 248
EXPECTED_TOTAL_PRODUCTS = 2895
EXPECTED_WITH_GOODS_ID = 2465
EXPECTED_WITHOUT_GOODS_ID = 430
EXPECTED_LATEST_PRICE_LIST_ID = 12
EXPECTED_LATEST_PRICE_LIST_NUMBER = "002_Алматы_2026-05-30_wf8"


def clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def first_semicolon(value: Any) -> str:
    raw = clean(value)
    if not raw:
        return ""
    return raw.split(";", 1)[0].strip()


def load_candidates() -> list[dict[str, Any]]:
    with SAFE_CANDIDATES_CSV.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    candidates: list[dict[str, Any]] = []
    for row in rows:
        product_id = clean(row.get("product_id"))
        product_code = clean(row.get("product_code") or row.get("Product.code"))
        goods_id = clean(row.get("goodsId") or row.get("matched goodsId"))
        candidates.append(
            {
                "product_id": int(product_id) if product_id else None,
                "product_code": product_code,
                "goods_id": int(goods_id) if goods_id else None,
                "pharmcenter_name": clean(row.get("pharmcenter_name") or row.get("PharmCenter.name")),
                "provisor_full_name": clean(row.get("provisor_fullName") or row.get("Provisor fullName")),
                "source_price_list_id": first_semicolon(row.get("source_price_list_ids") or row.get("source price_list_id")),
                "source_filial_id": first_semicolon(row.get("source_filialIds") or row.get("source filialId")),
                "source_account_id": first_semicolon(row.get("source_accountIds") or row.get("source account_id")),
                "source_account_login": clean(
                    row.get("source_accountLogins")
                    or row.get("source_accountLogins;;;;;;;")
                    or row.get("source account_login")
                ),
                "raw": row,
            }
        )
    return candidates


def validate_candidates(candidates: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if len(candidates) != EXPECTED_ROWS:
        errors.append(f"Expected {EXPECTED_ROWS} candidate rows, got {len(candidates)}")

    by_product_id: dict[int, set[int]] = defaultdict(set)
    by_product_code: dict[str, set[int]] = defaultdict(set)
    for idx, candidate in enumerate(candidates, start=2):
        if not candidate["product_id"] and not candidate["product_code"]:
            errors.append(f"CSV row {idx}: missing product_id and product_code")
        if candidate["goods_id"] is None:
            errors.append(f"CSV row {idx}: missing goodsId")
        if candidate["product_id"] and candidate["goods_id"] is not None:
            by_product_id[candidate["product_id"]].add(candidate["goods_id"])
        if candidate["product_code"] and candidate["goods_id"] is not None:
            by_product_code[candidate["product_code"]].add(candidate["goods_id"])

    for product_id, goods_ids in by_product_id.items():
        if len(goods_ids) > 1:
            errors.append(f"product_id {product_id} maps to multiple goodsIds: {sorted(goods_ids)}")
    for product_code, goods_ids in by_product_code.items():
        if len(goods_ids) > 1:
            errors.append(f"product_code {product_code} maps to multiple goodsIds: {sorted(goods_ids)}")

    duplicate_ids = [pid for pid, count in Counter(c["product_id"] for c in candidates if c["product_id"]).items() if count > 1]
    duplicate_codes = [
        code for code, count in Counter(c["product_code"] for c in candidates if c["product_code"]).items() if count > 1
    ]
    if duplicate_ids:
        errors.append(f"Duplicate product_id rows: {duplicate_ids[:20]}")
    if duplicate_codes:
        errors.append(f"Duplicate product_code rows: {duplicate_codes[:20]}")
    return errors


def main() -> int:
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_csv = DIAGNOSTICS_DIR / f"provisor_goodsid_fill_248_backup_{timestamp}.csv"
    backup_json = DIAGNOSTICS_DIR / f"provisor_goodsid_fill_248_backup_{timestamp}.json"
    result_json = DIAGNOSTICS_DIR / f"provisor_goodsid_fill_248_result_{timestamp}.json"

    candidates = load_candidates()
    csv_errors = validate_candidates(candidates)
    if csv_errors:
        raise RuntimeError("Candidate CSV validation failed:\n" + "\n".join(csv_errors))

    engine = create_engine(DATABASE_URL, connect_args={"connect_timeout": 5})

    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("SET TRANSACTION READ WRITE"))
            preflight = conn.execute(
                text(
                    """
                    SELECT
                        (SELECT count(*) FROM products) AS total_products,
                        (SELECT count(*) FROM products WHERE provisor_goods_id IS NOT NULL) AS with_goods_id,
                        (SELECT count(*) FROM products WHERE provisor_goods_id IS NULL) AS without_goods_id
                    """
                )
            ).mappings().one()
            latest = conn.execute(
                text("SELECT id, number FROM price_lists ORDER BY id DESC LIMIT 1")
            ).mappings().one()

            preflight_errors = []
            if preflight["total_products"] != EXPECTED_TOTAL_PRODUCTS:
                preflight_errors.append(f"total products expected {EXPECTED_TOTAL_PRODUCTS}, got {preflight['total_products']}")
            if preflight["with_goods_id"] != EXPECTED_WITH_GOODS_ID:
                preflight_errors.append(
                    f"with provisor_goods_id expected {EXPECTED_WITH_GOODS_ID}, got {preflight['with_goods_id']}"
                )
            if preflight["without_goods_id"] != EXPECTED_WITHOUT_GOODS_ID:
                preflight_errors.append(
                    f"without provisor_goods_id expected {EXPECTED_WITHOUT_GOODS_ID}, got {preflight['without_goods_id']}"
                )
            if latest["id"] != EXPECTED_LATEST_PRICE_LIST_ID:
                preflight_errors.append(f"latest price_list_id expected {EXPECTED_LATEST_PRICE_LIST_ID}, got {latest['id']}")
            if latest["number"] != EXPECTED_LATEST_PRICE_LIST_NUMBER:
                preflight_errors.append(
                    f"latest price_list number expected {EXPECTED_LATEST_PRICE_LIST_NUMBER}, got {latest['number']}"
                )
            if preflight_errors:
                raise RuntimeError("Live DB preflight failed:\n" + "\n".join(preflight_errors))

            product_ids = [c["product_id"] for c in candidates if c["product_id"]]
            db_products = conn.execute(
                text(
                    """
                    SELECT id, code, name, provisor_goods_id
                    FROM products
                    WHERE id = ANY(:ids)
                    """
                ),
                {"ids": product_ids},
            ).mappings().all()
            db_by_id = {row["id"]: row for row in db_products}

            backup_rows: list[dict[str, Any]] = []
            eligible: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            crosscheck_errors: list[str] = []

            for candidate in candidates:
                product = db_by_id.get(candidate["product_id"])
                if not product:
                    crosscheck_errors.append(f"product_id {candidate['product_id']} not found")
                    continue
                if candidate["product_code"] and product["code"] != candidate["product_code"]:
                    crosscheck_errors.append(
                        f"product_id {candidate['product_id']} code mismatch: DB={product['code']} CSV={candidate['product_code']}"
                    )
                    continue
                backup_row = {
                    "product_id": product["id"],
                    "product_code": product["code"],
                    "product_name": product["name"],
                    "old_provisor_goods_id": product["provisor_goods_id"],
                    "new_provisor_goods_id": candidate["goods_id"],
                    "pharmcenter_name": candidate["pharmcenter_name"],
                    "provisor_fullName": candidate["provisor_full_name"],
                    "source_price_list_id": candidate["source_price_list_id"],
                    "source_filialId": candidate["source_filial_id"],
                    "source_accountId": candidate["source_account_id"],
                    "source_accountLogin": candidate["source_account_login"],
                }
                backup_rows.append(backup_row)
                if product["provisor_goods_id"] is not None:
                    skipped.append({**backup_row, "reason": "already_has_provisor_goods_id"})
                    continue
                eligible.append(candidate)

            if crosscheck_errors:
                raise RuntimeError("DB cross-check failed:\n" + "\n".join(crosscheck_errors))

            backup_fields = [
                "product_id",
                "product_code",
                "product_name",
                "old_provisor_goods_id",
                "new_provisor_goods_id",
                "pharmcenter_name",
                "provisor_fullName",
                "source_price_list_id",
                "source_filialId",
                "source_accountId",
                "source_accountLogin",
            ]
            with backup_csv.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=backup_fields)
                writer.writeheader()
                writer.writerows(backup_rows)

            backup_json.write_text(
                json.dumps(
                    {
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                        "database_url": "postgresql://apteka:apteka@127.0.0.1:5432/apteka",
                        "safe_candidates_csv": str(SAFE_CANDIDATES_CSV),
                        "rows": backup_rows,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            updated_rows = 0
            update_details: list[dict[str, Any]] = []
            for candidate in eligible:
                result = conn.execute(
                    text(
                        """
                        UPDATE products
                        SET provisor_goods_id = :new_goods_id
                        WHERE id = :product_id
                          AND provisor_goods_id IS NULL
                        """
                    ),
                    {"new_goods_id": candidate["goods_id"], "product_id": candidate["product_id"]},
                )
                updated_rows += result.rowcount
                update_details.append(
                    {
                        "product_id": candidate["product_id"],
                        "product_code": candidate["product_code"],
                        "new_provisor_goods_id": candidate["goods_id"],
                        "rowcount": result.rowcount,
                    }
                )

            if updated_rows != len(eligible):
                raise RuntimeError(f"Expected to update {len(eligible)} rows, updated {updated_rows}; rolling back")

            after_in_tx = conn.execute(
                text(
                    """
                    SELECT
                        (SELECT count(*) FROM products) AS total_products,
                        (SELECT count(*) FROM products WHERE provisor_goods_id IS NOT NULL) AS with_goods_id,
                        (SELECT count(*) FROM products WHERE provisor_goods_id IS NULL) AS without_goods_id
                    """
                )
            ).mappings().one()

            new_goods_ids = sorted({c["goods_id"] for c in eligible})
            selected_presence = conn.execute(
                text(
                    """
                    WITH selected_lists AS (
                        SELECT DISTINCT l.id
                        FROM competitor_price_lists l
                        JOIN price_format_competitor_assignments a
                          ON a.competitor_price_list_id = l.id
                         AND a.is_active = TRUE
                        WHERE l.source_type = 'provisor'
                    ),
                    present AS (
                        SELECT DISTINCT i.provisor_goods_id
                        FROM competitor_price_list_items i
                        JOIN selected_lists sl ON sl.id = i.price_list_id
                        WHERE i.provisor_goods_id = ANY(:goods_ids)
                    )
                    SELECT count(*) AS goods_ids_present
                    FROM present
                    """
                ),
                {"goods_ids": new_goods_ids},
            ).scalar_one()
            selected_absent = len(new_goods_ids) - int(selected_presence or 0)

            trans.commit()
        except Exception:
            trans.rollback()
            raise

    with engine.connect() as conn:
        post = conn.execute(
            text(
                """
                SELECT
                    (SELECT count(*) FROM products) AS total_products,
                    (SELECT count(*) FROM products WHERE provisor_goods_id IS NOT NULL) AS with_goods_id,
                    (SELECT count(*) FROM products WHERE provisor_goods_id IS NULL) AS without_goods_id
                """
            )
        ).mappings().one()

    result_payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "database_url": "postgresql://apteka:apteka@127.0.0.1:5432/apteka",
        "safe_candidates_csv": str(SAFE_CANDIDATES_CSV),
        "backup_csv": str(backup_csv),
        "backup_json": str(backup_json),
        "candidate_rows_loaded": len(candidates),
        "eligible_rows": len(eligible),
        "updated_rows": updated_rows,
        "skipped_rows": len(skipped),
        "skipped": skipped,
        "before": dict(preflight),
        "after": dict(post),
        "after_in_transaction": dict(after_in_tx),
        "latest_price_list": dict(latest),
        "validation": {
            "same_product_id_updated_twice": 0,
            "product_code_mapped_to_multiple_goodsId": 0,
            "candidate_goodsId_null": 0,
            "conflict_rows": 0,
        },
        "estimated_generated_coverage_impact": {
            "newly_filled_distinct_goodsIds": len(new_goods_ids),
            "newly_filled_goodsIds_present_in_selected_provisor_lists": int(selected_presence or 0),
            "newly_filled_products_may_receive_competitor_price": int(selected_presence or 0),
            "newly_filled_products_still_absent_from_selected_provisor_lists": selected_absent,
            "note": "Estimate only; price list was not regenerated.",
        },
        "updated_details": update_details,
        "no_changes_made_to": ["SourceGoodsMatch", "matcher architecture", "pricing formulas", "Provisor parser"],
    }
    result_json.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result_payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
