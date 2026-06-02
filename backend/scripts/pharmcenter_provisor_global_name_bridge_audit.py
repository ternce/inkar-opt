from __future__ import annotations

import csv
import json
import os
import re
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


ROOT_DIR = Path(__file__).resolve().parents[2]
DIAGNOSTICS_DIR = Path(__file__).resolve().parents[1] / "diagnostics"
PHARMCENTER_URL = "https://ph.center/api/Report/PricesAnalysis"
PHARMCENTER_TOKEN = "Bearer c5741fd869434cfb5a032b44c1cdcd8d"
UNKNOWN_GOODS_NAME = "\u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u044b\u0439 \u0442\u043e\u0432\u0430\u0440"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
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
            url = "postgresql://" + url[len("postgres://") :]
        if url.startswith("postgresql://") and "+psycopg" not in url:
            url = "postgresql+psycopg://" + url[len("postgresql://") :]
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


def normalize_name(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip().lower().replace("\u0451", "\u0435")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*/\s*", "/", s)
    s = re.sub(r"/+", "/", s)
    return s.strip(" /")


def txt(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_raw_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def nested_get(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = payload
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def first_nonempty(*values: Any) -> str:
    for value in values:
        text_value = txt(value)
        if text_value:
            return text_value
    return ""


def fetch_pharmcenter() -> list[dict[str, Any]]:
    token = os.getenv("PHCENTER_TOKEN", PHARMCENTER_TOKEN)
    query = urllib.parse.urlencode({"region": 1, "price_mode": 0, "distributors": 1})
    request = urllib.request.Request(f"{PHARMCENTER_URL}?{query}", headers={"Authorization": token})
    with urllib.request.urlopen(request, timeout=90) as response:
        data = json.loads(response.read().decode("utf-8"))
    root_goods = nested_get(data, ("root", "goods", "good")) if isinstance(data, dict) else None
    if isinstance(root_goods, list):
        return root_goods
    if isinstance(data, list):
        return data
    raise RuntimeError(f"Unexpected PharmCenter response shape: {type(data).__name__}")


def is_average_price_list(row: dict[str, Any]) -> bool:
    haystack = " ".join(
        txt(row.get(key))
        for key in ("branch_name", "display_name", "competitor_name", "source_key")
    ).lower()
    return "средняя цена" in haystack or "average" in haystack


def source_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (txt(row.get("price_list_id")), txt(row.get("account_id")), txt(row.get("filial_id")))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def compact_sources(rows: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for row in rows:
        key = source_key(row)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "price_list_id": row["price_list_id"],
                "filialId": row["filial_id"],
                "filialName": row["branch_name"],
                "accountId": row["account_id"],
                "accountLogin": row["account_login"],
                "sourceType": "average_price" if row["is_average_price_list"] else "real_distributor",
            }
        )
        if len(result) >= limit:
            break
    return result


def main() -> int:
    load_env_file(ROOT_DIR / ".env")
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    engine, db_url = connect_engine()

    with engine.connect() as conn:
        trans = conn.begin()
        conn.execute(text("SET TRANSACTION READ ONLY"))
        total_products = conn.execute(text("SELECT count(*) FROM products")).scalar_one()
        mapped_products = conn.execute(
            text("SELECT count(*) FROM products WHERE provisor_goods_id IS NOT NULL")
        ).scalar_one()
        unresolved = conn.execute(
            text(
                """
                SELECT id AS product_id, code, name
                FROM products
                WHERE provisor_goods_id IS NULL
                ORDER BY id
                """
            )
        ).mappings().all()
        provisor_rows = conn.execute(
            text(
                """
                SELECT
                    i.id AS item_id,
                    i.price_list_id,
                    i.provisor_goods_id,
                    i.filial_id,
                    i.name,
                    i.distributor_goods_id,
                    i.distributor_goods_name,
                    i.raw_json,
                    l.account_id,
                    l.account_login,
                    l.branch_id,
                    l.branch_code,
                    l.branch_name,
                    l.display_name,
                    l.competitor_name,
                    l.source_key,
                    l.external_price_list_id
                FROM competitor_price_list_items i
                JOIN competitor_price_lists l ON l.id = i.price_list_id
                WHERE l.source_type = 'provisor'
                """
            )
        ).mappings().all()
        latest_price_list = conn.execute(
            text("SELECT id, number FROM price_lists ORDER BY id DESC LIMIT 1")
        ).mappings().first()
        generated_total = generated_with_competitor = None
        if latest_price_list:
            generated_total = conn.execute(
                text("SELECT count(*) FROM calculated_prices WHERE price_list_id = :id"),
                {"id": latest_price_list["id"]},
            ).scalar_one()
            generated_with_competitor = conn.execute(
                text(
                    """
                    SELECT count(*)
                    FROM calculated_prices
                    WHERE price_list_id = :id
                      AND competitor_price IS NOT NULL
                    """
                ),
                {"id": latest_price_list["id"]},
            ).scalar_one()
        trans.rollback()

    pharm_rows = fetch_pharmcenter()
    pharm_by_id: dict[str, dict[str, Any]] = {}
    for row in pharm_rows:
        sku = txt(row.get("id"))
        if sku and sku not in pharm_by_id:
            pharm_by_id[sku] = row

    provisor_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    invalid_provisor_rows = 0
    nonempty_price_lists = set()
    for raw_row in provisor_rows:
        row = dict(raw_row)
        nonempty_price_lists.add(row["price_list_id"])
        raw = parse_raw_json(row.get("raw_json"))
        goods = raw.get("goods") if isinstance(raw.get("goods"), dict) else {}
        full_name = first_nonempty(
            nested_get(raw, ("goods", "fullName")),
            raw.get("goodsFullName"),
            raw.get("fullName"),
            nested_get(raw, ("raw", "goods", "fullName")),
            row.get("name"),
        )
        goods_id = first_nonempty(
            row.get("provisor_goods_id"),
            nested_get(raw, ("goods", "id")),
            raw.get("goodsId"),
            goods.get("goodsId") if isinstance(goods, dict) else None,
        )
        norm = normalize_name(full_name)
        if not norm or norm == UNKNOWN_GOODS_NAME or not goods_id:
            invalid_provisor_rows += 1
            continue
        row.update(
            {
                "goods_id": goods_id,
                "provisor_full_name": full_name,
                "normalized_full_name": norm,
                "is_average_price_list": is_average_price_list(row),
            }
        )
        provisor_index[norm].append(row)

    classifications = Counter()
    safe_candidates: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    account_candidate_counter: Counter[tuple[str, str]] = Counter()
    filial_candidate_counter: Counter[tuple[str, str]] = Counter()
    all_account_ids: set[str] = set()
    all_account_logins: set[str] = set()
    all_filial_ids: set[str] = set()
    all_filial_names: set[str] = set()
    source_bucket_counter = Counter()

    for product in unresolved:
        code = txt(product["code"])
        pharm = pharm_by_id.get(code)
        if not pharm:
            classifications["pharmcenter_not_found"] += 1
            continue
        pharm_name = txt(pharm.get("name"))
        pharm_norm = normalize_name(pharm_name)
        if not pharm_norm:
            classifications["pharmcenter_name_empty"] += 1
            continue
        matches = provisor_index.get(pharm_norm, [])
        if not matches:
            classifications["provisor_name_not_found"] += 1
            continue
        goods_ids = sorted({txt(row["goods_id"]) for row in matches if txt(row["goods_id"])})
        if len(goods_ids) != 1:
            classifications["conflict_multiple_goodsIds"] += 1
            conflicts.append(
                {
                    "product_code": code,
                    "product_name": product["name"],
                    "pharmcenter_name": pharm_name,
                    "conflicting_goodsIds": "; ".join(goods_ids),
                    "source_examples": json.dumps(compact_sources(matches), ensure_ascii=False),
                    "provisor_fullNames": " | ".join(sorted({txt(m["provisor_full_name"]) for m in matches})),
                    "matched_rows_count": len(matches),
                }
            )
            continue

        classifications["safe_unique_goodsId"] += 1
        has_average = any(row["is_average_price_list"] for row in matches)
        has_real = any(not row["is_average_price_list"] for row in matches)
        if has_average and has_real:
            source_bucket = "both_average_and_real"
        elif has_average:
            source_bucket = "average_price_only"
        else:
            source_bucket = "real_distributor_only"
        source_bucket_counter[source_bucket] += 1

        unique_accounts = {(txt(m["account_id"]), txt(m["account_login"])) for m in matches}
        unique_filials = {(txt(m["filial_id"]), txt(m["branch_name"])) for m in matches}
        for account in unique_accounts:
            account_candidate_counter[account] += 1
            all_account_ids.add(account[0])
            all_account_logins.add(account[1])
        for filial in unique_filials:
            filial_candidate_counter[filial] += 1
            all_filial_ids.add(filial[0])
            all_filial_names.add(filial[1])

        example = sorted(matches, key=lambda m: (m["is_average_price_list"], int(m["price_list_id"])))[0]
        safe_candidates.append(
            {
                "product_id": product["product_id"],
                "product_code": code,
                "product_name": product["name"],
                "pharmcenter_name": pharm_name,
                "goodsId": goods_ids[0],
                "provisor_fullName": example["provisor_full_name"],
                "source_bucket": source_bucket,
                "matched_rows_count": len(matches),
                "matched_price_list_count": len({source_key(m) for m in matches}),
                "average_price_source_count": len({source_key(m) for m in matches if m["is_average_price_list"]}),
                "real_distributor_source_count": len({source_key(m) for m in matches if not m["is_average_price_list"]}),
                "source_price_list_ids": "; ".join(sorted({txt(m["price_list_id"]) for m in matches}, key=lambda x: int(x))),
                "source_filialIds": "; ".join(sorted({txt(m["filial_id"]) for m in matches}, key=lambda x: int(x) if x.isdigit() else 0)),
                "source_filialNames": " | ".join(sorted({txt(m["branch_name"]) for m in matches})),
                "source_accountIds": "; ".join(sorted({txt(m["account_id"]) for m in matches}, key=lambda x: int(x) if x.isdigit() else 0)),
                "source_accountLogins": " | ".join(sorted({txt(m["account_login"]) for m in matches})),
            }
        )

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "read_only": True,
        "scope": "All non-empty Provisor competitor data in DB; no selected/assigned/prior-match restriction.",
        "database_url_used": db_url.replace("+psycopg", ""),
        "total_products": total_products,
        "products_with_provisor_goods_id": mapped_products,
        "total_unresolved_products": len(unresolved),
        "pharmcenter_fetched_rows_count": len(pharm_rows),
        "pharmcenter_found_for_unresolved_count": len(unresolved)
        - classifications["pharmcenter_not_found"],
        "provisor_rows_scanned": len(provisor_rows),
        "provisor_nonempty_price_lists_scanned": len(nonempty_price_lists),
        "invalid_provisor_name_rows": invalid_provisor_rows,
        "classification_counts": {
            "safe_unique_goodsId": classifications["safe_unique_goodsId"],
            "conflict_multiple_goodsIds": classifications["conflict_multiple_goodsIds"],
            "pharmcenter_not_found": classifications["pharmcenter_not_found"],
            "pharmcenter_name_empty": classifications["pharmcenter_name_empty"],
            "provisor_name_not_found": classifications["provisor_name_not_found"],
        },
        "estimated_product_provisor_goods_id_coverage_after_safe_candidates": {
            "mapped_now": mapped_products,
            "safe_candidates": len(safe_candidates),
            "mapped_after": mapped_products + len(safe_candidates),
            "total_products": total_products,
            "coverage_now_pct": mapped_products / total_products * 100 if total_products else 0,
            "coverage_after_pct": (mapped_products + len(safe_candidates)) / total_products * 100
            if total_products
            else 0,
        },
        "estimated_generated_coverage_improvement_potential": {
            "latest_price_list": dict(latest_price_list) if latest_price_list else None,
            "generated_total": generated_total,
            "generated_with_competitor_now": generated_with_competitor,
            "safe_candidate_upper_bound_new_rows": len(safe_candidates),
            "generated_with_competitor_after_upper_bound": generated_with_competitor + len(safe_candidates)
            if generated_with_competitor is not None
            else None,
            "coverage_now_pct": generated_with_competitor / generated_total * 100
            if generated_total
            else None,
            "coverage_after_upper_bound_pct": (generated_with_competitor + len(safe_candidates)) / generated_total * 100
            if generated_total and generated_with_competitor is not None
            else None,
        },
        "source_bucket_counts": {
            "safe_candidates_found_only_in_average_price_lists": source_bucket_counter["average_price_only"],
            "safe_candidates_found_only_in_real_distributor_lists": source_bucket_counter["real_distributor_only"],
            "safe_candidates_found_in_both": source_bucket_counter["both_average_and_real"],
        },
        "source_breakdown": {
            "all_accountIds_used": sorted(v for v in all_account_ids if v),
            "all_accountLogins_used": sorted(v for v in all_account_logins if v),
            "all_filialIds_used": sorted((v for v in all_filial_ids if v), key=lambda x: int(x) if x.isdigit() else 0),
            "all_filialNames_used": sorted(v for v in all_filial_names if v),
            "top_accounts_by_matched_count": [
                {"accountId": aid, "accountLogin": login, "matchedCount": count}
                for (aid, login), count in account_candidate_counter.most_common(30)
            ],
            "top_filials_by_matched_count": [
                {"filialId": fid, "filialName": name, "matchedCount": count}
                for (fid, name), count in filial_candidate_counter.most_common(30)
            ],
        },
        "top_50_safe_candidates": safe_candidates[:50],
        "conflict_examples": conflicts[:50],
    }

    safe_csv = DIAGNOSTICS_DIR / "pharmcenter_provisor_global_name_bridge_safe_candidates.csv"
    conflicts_csv = DIAGNOSTICS_DIR / "pharmcenter_provisor_global_name_bridge_conflicts.csv"
    summary_json = DIAGNOSTICS_DIR / "pharmcenter_provisor_global_name_bridge_summary.json"
    write_csv(
        safe_csv,
        safe_candidates,
        [
            "product_id",
            "product_code",
            "product_name",
            "pharmcenter_name",
            "goodsId",
            "provisor_fullName",
            "source_bucket",
            "matched_rows_count",
            "matched_price_list_count",
            "average_price_source_count",
            "real_distributor_source_count",
            "source_price_list_ids",
            "source_filialIds",
            "source_filialNames",
            "source_accountIds",
            "source_accountLogins",
        ],
    )
    write_csv(
        conflicts_csv,
        conflicts,
        [
            "product_code",
            "product_name",
            "pharmcenter_name",
            "conflicting_goodsIds",
            "provisor_fullNames",
            "source_examples",
            "matched_rows_count",
        ],
    )
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nWrote:\n- {safe_csv}\n- {conflicts_csv}\n- {summary_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
