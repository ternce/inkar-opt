from __future__ import annotations

import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text


ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]
DIAGNOSTICS_DIR = BACKEND_DIR / "diagnostics"

PHARMCENTER_URL = "https://ph.center/api/Report/PricesAnalysis"
PHARMCENTER_TOKEN = "Bearer c5741fd869434cfb5a032b44c1cdcd8d"

INVALID_PROVISOR_NAMES = {"неизвестный товар"}


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
    if raw.startswith("postgres://"):
        candidates.append("postgresql://" + raw[len("postgres://") :])
    normalized: list[str] = []
    for url in candidates:
        if url.startswith("postgresql://") and "+psycopg" not in url:
            url = "postgresql+psycopg://" + url[len("postgresql://") :]
        if url not in normalized:
            normalized.append(url)
    return normalized


def connect_engine():
    last_error: Exception | None = None
    for url in database_url_candidates():
        try:
            connect_args = {"connect_timeout": 5} if url.startswith("postgresql") else {}
            engine = create_engine(url, connect_args=connect_args)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine, url
        except Exception as exc:  # pragma: no cover - diagnostic script
            last_error = exc
    raise RuntimeError(f"Could not connect to DB: {last_error}") from last_error


def normalize_name(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip().lower().replace("ё", "е")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*/\s*", "/", s)
    s = re.sub(r"/+", "/", s)
    return s.strip(" /")


def as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def as_int_or_text(value: Any) -> Any:
    if value is None or value == "":
        return ""
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def parse_raw_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def nested_get(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def first_nonempty(*values: Any) -> str:
    for value in values:
        text_value = as_text(value)
        if text_value:
            return text_value
    return ""


def fetch_pharmcenter() -> list[dict[str, Any]]:
    token = os.getenv("PHCENTER_TOKEN", PHARMCENTER_TOKEN)
    query = urllib.parse.urlencode({"region": 1, "price_mode": 0, "distributors": 1})
    request = urllib.request.Request(
        f"{PHARMCENTER_URL}?{query}",
        headers={"Authorization": token},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        data = json.loads(response.read().decode("utf-8"))
    if isinstance(data, dict):
        root_goods = nested_get(data, ("root", "goods", "good"))
        if isinstance(root_goods, list):
            return root_goods
        for key in ("data", "items", "result", "rows"):
            if isinstance(data.get(key), list):
                return data[key]
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected PharmCenter response shape: {type(data).__name__}")
    return data


def build_provisor_index(rows: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    invalid_rows: list[dict[str, Any]] = []

    for row in rows:
        raw = parse_raw_json(row.get("raw_json"))
        goods = raw.get("goods") if isinstance(raw.get("goods"), dict) else {}
        raw_full_name = first_nonempty(
            nested_get(raw, ("goods", "fullName")),
            raw.get("goodsFullName"),
            raw.get("fullName"),
            row.get("name"),
        )
        full_name_norm = normalize_name(raw_full_name)
        goods_id = first_nonempty(
            row.get("provisor_goods_id"),
            nested_get(raw, ("goods", "id")),
            raw.get("goodsId"),
            goods.get("goodsId") if isinstance(goods, dict) else None,
        )

        enriched = {
            **row,
            "goods_id": as_int_or_text(goods_id),
            "provisor_full_name": raw_full_name,
            "normalized_full_name": full_name_norm,
            "raw_goods_full_name": nested_get(raw, ("goods", "fullName")) or "",
            "raw_goods_id": raw.get("goodsId") or nested_get(raw, ("goods", "id")) or "",
        }

        if not full_name_norm or full_name_norm in INVALID_PROVISOR_NAMES:
            invalid_rows.append(enriched)
            continue
        if not goods_id:
            invalid_rows.append(enriched)
            continue
        index[full_name_norm].append(enriched)

    return dict(index), invalid_rows


def compact_examples(rows: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    examples = []
    seen = set()
    for row in rows:
        key = (
            row.get("goods_id"),
            row.get("provisor_full_name"),
            row.get("price_list_id"),
            row.get("filial_id"),
            row.get("account_id"),
            row.get("account_login"),
        )
        if key in seen:
            continue
        seen.add(key)
        examples.append(
            {
                "goodsId": row.get("goods_id"),
                "fullName": row.get("provisor_full_name"),
                "price_list_id": row.get("price_list_id"),
                "filialId": row.get("filial_id"),
                "account_id": row.get("account_id"),
                "account_login": row.get("account_login"),
                "distributorGoodsId": row.get("distributor_goods_id"),
                "distributorGoodsName": row.get("distributor_goods_name"),
            }
        )
        if len(examples) >= limit:
            break
    return examples


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


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
                    l.source_key,
                    l.display_name,
                    l.branch_name,
                    l.competitor_name
                FROM competitor_price_list_items i
                JOIN competitor_price_lists l ON l.id = i.price_list_id
                WHERE l.source_type = 'provisor'
                """
            )
        ).mappings().all()
        trans.rollback()

    pharm_rows = fetch_pharmcenter()
    pharm_by_id: dict[str, dict[str, Any]] = {}
    for row in pharm_rows:
        sku = as_text(row.get("id"))
        if sku and sku not in pharm_by_id:
            pharm_by_id[sku] = row

    provisor_index, invalid_provisor_rows = build_provisor_index([dict(row) for row in provisor_rows])

    safe_candidates: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    classifications: dict[str, int] = defaultdict(int)
    pharmcenter_found = 0

    for product in unresolved:
        code = as_text(product["code"])
        pharm = pharm_by_id.get(code)
        if not pharm:
            classifications["pharmcenter_not_found"] += 1
            continue
        pharmcenter_found += 1

        pharm_name = as_text(pharm.get("name"))
        pharm_norm = normalize_name(pharm_name)
        if not pharm_norm:
            classifications["pharmcenter_name_empty"] += 1
            continue

        matches = provisor_index.get(pharm_norm, [])
        if not matches:
            classifications["provisor_name_not_found"] += 1
            continue

        goods_ids = sorted({str(row["goods_id"]) for row in matches if row.get("goods_id")})
        if len(goods_ids) == 1:
            classifications["safe_unique_goodsId"] += 1
            example = matches[0]
            safe_candidates.append(
                {
                    "product_id": product["product_id"],
                    "Product.code": code,
                    "Product.name": product["name"],
                    "PharmCenter.name": pharm_name,
                    "matched goodsId": goods_ids[0],
                    "Provisor fullName": example.get("provisor_full_name"),
                    "source price_list_id": example.get("price_list_id"),
                    "source filialId": example.get("filial_id"),
                    "source account_id": example.get("account_id"),
                    "source account_login": example.get("account_login"),
                    "matched_rows_count": len(matches),
                    "normalized_name": pharm_norm,
                }
            )
        else:
            classifications["conflict_multiple_goodsIds"] += 1
            conflicts.append(
                {
                    "Product.code": code,
                    "Product.name": product["name"],
                    "PharmCenter.name": pharm_name,
                    "conflicting goodsIds": "; ".join(goods_ids),
                    "Provisor fullNames": " | ".join(sorted({as_text(r.get("provisor_full_name")) for r in matches})),
                    "filials/accounts/list ids": json.dumps(compact_examples(matches), ensure_ascii=False),
                    "matched_rows_count": len(matches),
                    "normalized_name": pharm_norm,
                }
            )

    generated_total = conn_comp_total = None
    generated_with_competitor = None
    with engine.connect() as conn:
        trans = conn.begin()
        conn.execute(text("SET TRANSACTION READ ONLY"))
        latest_price_list = conn.execute(
            text("SELECT id, number FROM price_lists ORDER BY id DESC LIMIT 1")
        ).mappings().first()
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
            conn_comp_total = generated_total
        trans.rollback()

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "read_only": True,
        "rule": "Product.code -> PharmCenter.id -> normalized PharmCenter.name == normalized Provisor goods.fullName; no fuzzy, no distributorGoodsId equality.",
        "database_url_used": db_url.replace("+psycopg", ""),
        "total_products": total_products,
        "products_with_provisor_goods_id": mapped_products,
        "total_unresolved_products": len(unresolved),
        "pharmcenter_fetched_rows_count": len(pharm_rows),
        "pharmcenter_unique_id_count": len(pharm_by_id),
        "pharmcenter_found_for_unresolved_count": pharmcenter_found,
        "provisor_rows_scanned": len(provisor_rows),
        "provisor_valid_normalized_name_count": len(provisor_index),
        "invalid_provisor_name_rows": len(invalid_provisor_rows),
        "classification_counts": {
            "safe_unique_goodsId": classifications["safe_unique_goodsId"],
            "conflict_multiple_goodsIds": classifications["conflict_multiple_goodsIds"],
            "pharmcenter_not_found": classifications["pharmcenter_not_found"],
            "pharmcenter_name_empty": classifications["pharmcenter_name_empty"],
            "provisor_name_not_found": classifications["provisor_name_not_found"],
            "invalid_provisor_name": len(invalid_provisor_rows),
        },
        "estimated_Product_provisor_goods_id_coverage_after_safe_candidates": {
            "mapped_now": mapped_products,
            "safe_candidates": len(safe_candidates),
            "mapped_after": mapped_products + len(safe_candidates),
            "total_products": total_products,
            "coverage_now_pct": (mapped_products / total_products * 100) if total_products else 0,
            "coverage_after_pct": ((mapped_products + len(safe_candidates)) / total_products * 100)
            if total_products
            else 0,
        },
        "estimated_generated_coverage_improvement_potential": {
            "latest_price_list": dict(latest_price_list) if latest_price_list else None,
            "generated_total": conn_comp_total,
            "generated_with_competitor_now": generated_with_competitor,
            "safe_candidate_upper_bound_new_rows": len(safe_candidates),
            "generated_with_competitor_after_upper_bound": (generated_with_competitor + len(safe_candidates))
            if generated_with_competitor is not None
            else None,
            "coverage_now_pct": (generated_with_competitor / generated_total * 100)
            if generated_total
            else None,
            "coverage_after_upper_bound_pct": ((generated_with_competitor + len(safe_candidates)) / generated_total * 100)
            if generated_total and generated_with_competitor is not None
            else None,
        },
        "top_50_safe_candidates": safe_candidates[:50],
        "conflict_examples": conflicts[:50],
        "notes": [
            "The invalid_provisor_name count is a global Provisor row-quality diagnostic, not an unresolved-product classification bucket.",
            "Generated coverage potential is an upper bound; actual generated competitor coverage depends on active source availability at pricing time.",
        ],
    }

    safe_csv = DIAGNOSTICS_DIR / "pharmcenter_provisor_name_bridge_safe_candidates.csv"
    conflicts_csv = DIAGNOSTICS_DIR / "pharmcenter_provisor_name_bridge_conflicts.csv"
    summary_json = DIAGNOSTICS_DIR / "pharmcenter_provisor_name_bridge_summary.json"

    write_csv(
        safe_csv,
        safe_candidates,
        [
            "product_id",
            "Product.code",
            "Product.name",
            "PharmCenter.name",
            "matched goodsId",
            "Provisor fullName",
            "source price_list_id",
            "source filialId",
            "source account_id",
            "source account_login",
            "matched_rows_count",
            "normalized_name",
        ],
    )
    write_csv(
        conflicts_csv,
        conflicts,
        [
            "Product.code",
            "Product.name",
            "PharmCenter.name",
            "conflicting goodsIds",
            "Provisor fullNames",
            "filials/accounts/list ids",
            "matched_rows_count",
            "normalized_name",
        ],
    )
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nWrote:\n- {safe_csv}\n- {conflicts_csv}\n- {summary_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
