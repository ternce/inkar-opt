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
BACKEND_DIR = Path(__file__).resolve().parents[1]
DIAGNOSTICS_DIR = BACKEND_DIR / "diagnostics"
PHARMCENTER_URL = "https://ph.center/api/Report/PricesAnalysis"
PHARMCENTER_TOKEN = "Bearer c5741fd869434cfb5a032b44c1cdcd8d"

BASELINE_SAFE = 9
BASELINE_REVIEW = 61
BASELINE_CONFLICT = 179
BASELINE_NOT_FOUND = 260
FULL_NAME_PATHS = [
    "raw.goods.fullName",
    "raw.goodsFullName",
    "raw.fullName",
    "raw.raw.goods.fullName",
    "row.name fallback",
]
INVALID_PROVISOR_NAMES = {"неизвестный товар"}

STOP_TOKENS = {
    "для", "при", "под", "без", "с", "со", "и", "в", "во", "на", "от", "из",
    "таб", "табл", "таблетки", "капс", "капсулы", "р-р", "раствор", "сироп",
    "спрей", "крем", "мазь", "гель", "сусп", "суспензия", "порошок", "пор",
    "супп", "суппозитории", "рект", "ваг", "амп", "фл", "флакон", "мл", "мг",
    "г", "шт", "n", "no", "номер",
}

FORM_ALIASES = {
    "таблетки": ("таблет", "табл", "таб "),
    "капсулы": ("капсул", "капс"),
    "раствор": ("раствор", "р-р", "р р"),
    "сироп": ("сироп",),
    "спрей": ("спрей",),
    "крем": ("крем",),
    "мазь": ("мазь",),
    "гель": ("гель",),
    "суспензия": ("суспенз", "сусп"),
    "порошок": ("порош", "пор."),
    "суппозитории": ("супп", "суппоз"),
    "капли": ("капли", "кап."),
    "пластыри": ("пластыр",),
    "шампунь": ("шампун",),
}


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


def normalize(value: Any) -> str:
    s = txt(value).lower().replace("ё", "е")
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*/\s*", "/", s)
    s = re.sub(r"/+", "/", s)
    return s.strip(" /")


def token_text(value: Any) -> str:
    s = normalize(value)
    s = re.sub(r"[№#]", " n ", s)
    s = re.sub(r"[^0-9a-zа-я%.,/+-]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(value: Any) -> set[str]:
    out = set()
    for raw in re.split(r"[\s/,+()-]+", token_text(value)):
        item = raw.strip(".")
        if len(item) < 2 or item in STOP_TOKENS:
            continue
        if re.fullmatch(r"\d+", item):
            continue
        if re.search(r"\d", item):
            continue
        out.add(item)
    return out


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
        result = txt(value)
        if result:
            return result
    return ""


def normalize_unit(unit: str) -> str:
    unit = unit.lower().replace("мкг", "mcg").replace("мг", "mg").replace("мл", "ml")
    if unit in {"гр", "г"}:
        return "g"
    return unit


def number_norm(value: str) -> str:
    value = value.replace(",", ".")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value


def dosage_tokens(value: str) -> set[str]:
    found = set()
    for num, unit in re.findall(r"(\d+(?:[.,]\d+)?)\s*(мкг|mcg|мг|mg|мл|ml|гр|г|g|%)", normalize(value)):
        found.add(f"{number_norm(num)}{normalize_unit(unit)}")
    return found


def package_count(value: str) -> str:
    s = normalize(value)
    m = re.search(r"(?:№|n|no|номер)\s*([0-9]{1,4})\b", s)
    if m:
        return m.group(1)
    parts = [p.strip() for p in s.split("/") if p.strip()]
    if len(parts) >= 2 and re.fullmatch(r"\d{1,4}", parts[-2]):
        return parts[-2]
    return ""


def forms(value: str) -> set[str]:
    s = normalize(value)
    result = set()
    for canonical, variants in FORM_ALIASES.items():
        if any(v in s for v in variants):
            result.add(canonical)
    return result


def volume_tokens(value: str) -> set[str]:
    return {x for x in dosage_tokens(value) if x.endswith(("ml", "g"))}


def signature(product_name: str, pharm_name: str = "") -> dict[str, Any]:
    text = f"{product_name} {pharm_name}"
    pharm_first = normalize(pharm_name).split("/", 1)[0] if pharm_name else ""
    brand = tokens(pharm_first) or set(list(tokens(product_name))[:4])
    return {
        "brand": brand,
        "dosage": dosage_tokens(text),
        "package_count": package_count(text),
        "forms": forms(text),
        "volume": volume_tokens(text),
    }


def candidate_signature(row: dict[str, Any]) -> dict[str, Any]:
    return signature(
        first_nonempty(row.get("distributor_goods_name"), row.get("name")),
        row.get("provisor_full_name") or "",
    )


def is_exact_average_price_list(row: dict[str, Any]) -> bool:
    haystack = " ".join(txt(row.get(k)) for k in ("branch_name", "display_name", "competitor_name", "source_key")).lower()
    return "средняя цена" in haystack or "average" in haystack


def fetch_pharmcenter() -> list[dict[str, Any]]:
    token = os.getenv("PHCENTER_TOKEN", PHARMCENTER_TOKEN)
    query = urllib.parse.urlencode({"region": 1, "price_mode": 0, "distributors": 1})
    request = urllib.request.Request(f"{PHARMCENTER_URL}?{query}", headers={"Authorization": token})
    with urllib.request.urlopen(request, timeout=90) as response:
        data = json.loads(response.read().decode("utf-8"))
    if isinstance(data, dict):
        root_goods = nested_get(data, ("root", "goods", "good"))
        if isinstance(root_goods, list):
            return root_goods
        for key in ("data", "items", "result", "rows"):
            if isinstance(data.get(key), list):
                return data[key]
    if isinstance(data, list):
        return data
    raise RuntimeError(f"Unexpected PharmCenter response shape: {type(data).__name__}")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def compact_sources(rows: list[dict[str, Any]], newly_refreshed_ids: set[str], limit: int = 20) -> list[dict[str, Any]]:
    out = []
    seen = set()
    for row in rows:
        key = (row.get("price_list_id"), row.get("filial_id"), row.get("account_id"))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "price_list_id": row.get("price_list_id"),
                "filialId": row.get("filial_id"),
                "filialName": row.get("branch_name"),
                "accountId": row.get("account_id"),
                "accountLogin": row.get("account_login"),
                "isSelected": row.get("is_selected"),
                "fromNewlyRefreshedAccount": txt(row.get("account_id")) in newly_refreshed_ids,
            }
        )
        if len(out) >= limit:
            break
    return out


def load_baseline_sets() -> dict[str, set[str]]:
    result = {"product_ids": set(), "goods_ids": set(), "full_names": set(), "account_ids": set()}
    fixed_summary_path = DIAGNOSTICS_DIR / "fixed_extraction_summary.json"
    if fixed_summary_path.exists():
        try:
            fixed_summary = json.loads(fixed_summary_path.read_text(encoding="utf-8"))
            for row in fixed_summary.get("source_breakdown", {}).get("top_accounts_by_matched_row_count", []):
                account_id = txt(row.get("accountId") or row.get("account_id"))
                if account_id:
                    result["account_ids"].add(account_id)
        except Exception:
            pass
    for name in ("advanced_signature_safe.csv", "advanced_signature_review.csv", "advanced_signature_conflicts.csv"):
        path = DIAGNOSTICS_DIR / name
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("product_id"):
                    result["product_ids"].add(row["product_id"])
                for key in ("goodsId", "goodsIds"):
                    for item in re.split(r"[|;]\s*", row.get(key) or ""):
                        if item.strip():
                            result["goods_ids"].add(item.strip())
                for key in ("provisor_fullNames", "provisor_fullName"):
                    for item in re.split(r"\s+\|\s+|\s+\|\|\s+", row.get(key) or ""):
                        if item.strip():
                            result["full_names"].add(normalize(item))
                for item in re.split(r"[;|]\s*", row.get("source_accountIds") or ""):
                    if item.strip():
                        result["account_ids"].add(item.strip())
                examples = row.get("source_examples") or row.get("examples") or ""
                if examples.strip().startswith("["):
                    try:
                        for example in json.loads(examples):
                            account_id = txt(example.get("accountId") or example.get("account_id"))
                            if account_id:
                                result["account_ids"].add(account_id)
                    except Exception:
                        pass
    return result


def summarize_match(
    classification: str,
    product: dict[str, Any],
    pharm: dict[str, Any] | None,
    rows: list[dict[str, Any]],
    goods_ids: list[str],
    sig: dict[str, Any],
    newly_refreshed_ids: set[str],
    reason: str = "",
) -> dict[str, Any]:
    new_rows = [r for r in rows if txt(r.get("account_id")) in newly_refreshed_ids]
    full_names = sorted({txt(r.get("provisor_full_name")) for r in rows if txt(r.get("provisor_full_name"))})
    distributor_names = sorted({txt(r.get("distributor_goods_name")) for r in rows if txt(r.get("distributor_goods_name"))})[:8]
    accounts = sorted({(txt(r.get("account_id")), txt(r.get("account_login"))) for r in rows})
    filials = sorted({(txt(r.get("filial_id")), txt(r.get("branch_name"))) for r in rows})
    return {
        "classification": classification,
        "product_id": product["product_id"],
        "product_code": product["code"],
        "product_name": product["name"],
        "pharmcenter_id": txt(pharm.get("id")) if pharm else "",
        "pharmcenter_name": txt(pharm.get("name")) if pharm else "",
        "goodsId": goods_ids[0] if len(goods_ids) == 1 else "",
        "goodsIds": "|".join(goods_ids),
        "matching_row_count": len(rows),
        "unique_filials": len(filials),
        "unique_accounts": len(accounts),
        "brand": "|".join(sorted(sig["brand"])),
        "dosage": "|".join(sorted(sig["dosage"])),
        "package_count": sig["package_count"],
        "dosage_form": "|".join(sorted(sig["forms"])),
        "volume": "|".join(sorted(sig["volume"])),
        "provisor_fullNames": " | ".join(full_names[:20]),
        "provisor_distributorGoodsNames": " | ".join(distributor_names),
        "source_price_list_ids": "; ".join(sorted({txt(r.get("price_list_id")) for r in rows}, key=lambda x: int(x) if x.isdigit() else 0)),
        "source_filialIds": "; ".join(sorted({txt(r.get("filial_id")) for r in rows}, key=lambda x: int(x) if x.isdigit() else 0)),
        "source_filialNames": " | ".join(name for _, name in filials if name),
        "source_accountIds": "; ".join(aid for aid, _ in accounts if aid),
        "source_accountLogins": " | ".join(login for _, login in accounts if login),
        "from_newly_refreshed_accounts": bool(new_rows),
        "newly_refreshed_accountIds": "; ".join(sorted({txt(r.get("account_id")) for r in new_rows}, key=lambda x: int(x) if x.isdigit() else 0)),
        "newly_refreshed_accountLogins": " | ".join(sorted({txt(r.get("account_login")) for r in new_rows if txt(r.get("account_login"))})),
        "source_examples": json.dumps(compact_sources(rows, newly_refreshed_ids), ensure_ascii=False),
        "reason": reason,
    }


def signature_candidates(sig: dict[str, Any], token_index: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if not sig["brand"]:
        return []
    rows_by_id: dict[int, dict[str, Any]] = {}
    for brand_token in sig["brand"]:
        for row in token_index.get(brand_token, []):
            rows_by_id[int(row["item_id"])] = row
    out = []
    for row in rows_by_id.values():
        hay_tokens = row["search_tokens"]
        brand_overlap = sig["brand"] & hay_tokens
        if not brand_overlap:
            continue
        csig = row["signature"]
        if sig["package_count"] and csig["package_count"] and sig["package_count"] != csig["package_count"]:
            continue
        if sig["forms"] and csig["forms"] and not (sig["forms"] & csig["forms"]):
            continue
        if sig["dosage"] and csig["dosage"] and not sig["dosage"].issubset(csig["dosage"]):
            continue
        if sig["volume"] and csig["volume"] and not (sig["volume"] & csig["volume"]):
            continue
        score = 3 * len(brand_overlap)
        score += 2 * len(sig["dosage"] & csig["dosage"])
        score += 2 if sig["package_count"] and sig["package_count"] == csig["package_count"] else 0
        score += 2 * len(sig["forms"] & csig["forms"])
        score += len(sig["volume"] & csig["volume"])
        if score >= 5:
            row = {**row, "_score": score}
            out.append(row)
    out.sort(key=lambda r: r["_score"], reverse=True)
    return out[:500]


def main() -> int:
    load_env_file(ROOT_DIR / ".env")
    load_env_file(BACKEND_DIR / ".env")
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    baseline_sets = load_baseline_sets()
    baseline_summary_path = DIAGNOSTICS_DIR / "advanced_signature_summary.json"
    baseline_summary = json.loads(baseline_summary_path.read_text(encoding="utf-8")) if baseline_summary_path.exists() else {}
    baseline_at = baseline_summary.get("generated_at")
    baseline_dt = datetime.fromisoformat(baseline_at) if baseline_at else None

    engine, db_url = connect_engine()
    with engine.connect() as conn:
        trans = conn.begin()
        conn.execute(text("SET TRANSACTION READ ONLY"))
        total_products = conn.execute(text("SELECT count(*) FROM products")).scalar_one()
        mapped_products = conn.execute(text("SELECT count(*) FROM products WHERE provisor_goods_id IS NOT NULL")).scalar_one()
        unresolved = [dict(r) for r in conn.execute(text(
            """
            SELECT id AS product_id, code, name
            FROM products
            WHERE provisor_goods_id IS NULL
            ORDER BY id
            """
        )).mappings().all()]
        account_rows = [dict(r) for r in conn.execute(text(
            """
            WITH list_item_counts AS (
                SELECT l.id, l.account_id, count(i.id) AS item_rows
                FROM competitor_price_lists l
                LEFT JOIN competitor_price_list_items i ON i.price_list_id = l.id
                WHERE l.source_type = 'provisor'
                GROUP BY l.id, l.account_id
            )
            SELECT
                a.id AS account_id,
                a.login,
                a.price_lists_count AS account_price_lists_count,
                count(c.id) AS price_list_count,
                count(c.id) FILTER (WHERE c.item_rows > 0) AS non_empty_price_list_count,
                coalesce(sum(c.item_rows), 0) AS total_item_rows,
                a.status,
                a.last_success_at,
                a.updated_at
            FROM price_source_accounts a
            LEFT JOIN list_item_counts c ON c.account_id = a.id::text
            WHERE a.source_type = 'provisor' AND a.is_active IS TRUE
            GROUP BY a.id
            ORDER BY coalesce(a.last_success_at, a.updated_at) DESC NULLS LAST, a.id
            """
        )).mappings().all()]
        provisor_rows_raw = [dict(r) for r in conn.execute(text(
            """
            SELECT
                i.id AS item_id,
                i.price_list_id,
                i.provisor_goods_id,
                i.filial_id,
                i.name,
                i.distributor_goods_id,
                i.distributor_goods_name,
                i.raw_manufacturer,
                i.raw_json,
                l.account_id,
                l.account_login,
                l.branch_id,
                l.branch_code,
                l.branch_name,
                l.display_name,
                l.competitor_name,
                l.source_key,
                l.external_price_list_id,
                l.is_selected,
                l.updated_at AS price_list_updated_at,
                l.last_success_at AS price_list_last_success_at
            FROM competitor_price_list_items i
            JOIN competitor_price_lists l ON l.id = i.price_list_id
            WHERE l.source_type = 'provisor'
            """
        )).mappings().all()]
        trans.rollback()

    newly_refreshed_ids = set()
    for row in account_rows:
        marker = row.get("last_success_at") or row.get("updated_at")
        aid = txt(row["account_id"])
        has_items = int(row.get("total_item_rows") or 0) > 0
        if has_items and aid not in baseline_sets["account_ids"]:
            newly_refreshed_ids.add(aid)
        elif baseline_dt and marker and marker.replace(tzinfo=None) > baseline_dt:
            newly_refreshed_ids.add(txt(row["account_id"]))

    pool_account_ids = {txt(r.get("account_id")) for r in provisor_rows_raw if txt(r.get("account_id"))}
    account_verification = []
    for row in account_rows:
        aid = txt(row["account_id"])
        account_verification.append(
            {
                "account_id": row["account_id"],
                "login": row["login"],
                "price_list_count": int(row["price_list_count"] or row["account_price_lists_count"] or 0),
                "non_empty_price_list_count": int(row["non_empty_price_list_count"] or 0),
                "total_item_rows": int(row["total_item_rows"] or 0),
                "last_success_at": row["last_success_at"].isoformat() if row.get("last_success_at") else None,
                "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
                "newly_refreshed_after_baseline": aid in newly_refreshed_ids,
                "included_in_provisor_search_pool": aid in pool_account_ids,
            }
        )

    pharm_rows = fetch_pharmcenter()
    pharm_by_id = {}
    for row in pharm_rows:
        code = txt(row.get("id"))
        if code and code not in pharm_by_id:
            pharm_by_id[code] = row

    provisor_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_provisor_rows: list[dict[str, Any]] = []
    token_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    invalid_rows = 0
    nonempty_price_lists = set()
    for raw_row in provisor_rows_raw:
        raw = parse_raw_json(raw_row.get("raw_json"))
        goods = raw.get("goods") if isinstance(raw.get("goods"), dict) else {}
        full_name = first_nonempty(
            nested_get(raw, ("goods", "fullName")),
            raw.get("goodsFullName"),
            raw.get("fullName"),
            nested_get(raw, ("raw", "goods", "fullName")),
            raw_row.get("name"),
        )
        goods_id = first_nonempty(
            raw_row.get("provisor_goods_id"),
            nested_get(raw, ("goods", "id")),
            raw.get("goodsId"),
            goods.get("goodsId") if isinstance(goods, dict) else None,
        )
        norm = normalize(full_name)
        if raw_row.get("price_list_id"):
            nonempty_price_lists.add(raw_row["price_list_id"])
        if not norm or norm in INVALID_PROVISOR_NAMES or not goods_id:
            invalid_rows += 1
            continue
        row = {
            **raw_row,
            "goods_id": txt(goods_id),
            "provisor_full_name": full_name,
            "normalized_full_name": norm,
            "is_average_price_list": is_exact_average_price_list(raw_row),
        }
        row["signature"] = candidate_signature(row)
        row["search_tokens"] = tokens(f"{row.get('provisor_full_name') or ''} {row.get('distributor_goods_name') or ''}")
        provisor_index[norm].append(row)
        all_provisor_rows.append(row)
        for token in row["search_tokens"]:
            token_index[token].append(row)

    safe_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    conflict_rows: list[dict[str, Any]] = []
    not_found_rows: list[dict[str, Any]] = []
    counts = Counter()
    matched_account_counter = Counter()
    matched_filial_counter = Counter()
    newly_account_counter = Counter()
    newly_filial_counter = Counter()

    for product in unresolved:
        code = txt(product["code"])
        pharm = pharm_by_id.get(code)
        sig = signature(product["name"], txt(pharm.get("name")) if pharm else "")
        exact_rows: list[dict[str, Any]] = []
        if pharm:
            exact_rows = provisor_index.get(normalize(pharm.get("name")), [])
        rows = exact_rows
        reason = "exact_pharmcenter_name_to_provisor_fullName"
        if not rows:
            rows = signature_candidates(sig, token_index)
            reason = "signature_candidate_non_exact"
        if not rows:
            counts["NOT_FOUND"] += 1
            not_found_rows.append(
                {
                    "classification": "NOT_FOUND",
                    "product_id": product["product_id"],
                    "product_code": code,
                    "product_name": product["name"],
                    "pharmcenter_id": txt(pharm.get("id")) if pharm else "",
                    "pharmcenter_name": txt(pharm.get("name")) if pharm else "",
                    "reason": "pharmcenter_not_found" if not pharm else "no_provisor_candidate",
                    "brand": "|".join(sorted(sig["brand"])),
                    "dosage": "|".join(sorted(sig["dosage"])),
                    "package_count": sig["package_count"],
                    "dosage_form": "|".join(sorted(sig["forms"])),
                    "volume": "|".join(sorted(sig["volume"])),
                }
            )
            continue
        goods_ids = sorted({txt(r.get("goods_id")) for r in rows if txt(r.get("goods_id"))}, key=lambda x: int(x) if x.isdigit() else x)
        if len(goods_ids) == 1 and exact_rows:
            counts["SAFE"] += 1
            out = summarize_match("SAFE", product, pharm, rows, goods_ids, sig, newly_refreshed_ids, reason)
            safe_rows.append(out)
        elif len(goods_ids) == 1:
            counts["REVIEW"] += 1
            out = summarize_match("REVIEW", product, pharm, rows, goods_ids, sig, newly_refreshed_ids, reason)
            review_rows.append(out)
        else:
            counts["CONFLICT"] += 1
            out = summarize_match("CONFLICT", product, pharm, rows, goods_ids, sig, newly_refreshed_ids, reason)
            out["candidate_goodsId_counts"] = json.dumps(Counter(txt(r.get("goods_id")) for r in rows), ensure_ascii=False)
            conflict_rows.append(out)
        for r in rows:
            matched_account_counter[(txt(r.get("account_id")), txt(r.get("account_login")))] += 1
            matched_filial_counter[(txt(r.get("filial_id")), txt(r.get("branch_name")))] += 1
            if txt(r.get("account_id")) in newly_refreshed_ids:
                newly_account_counter[(txt(r.get("account_id")), txt(r.get("account_login")))] += 1
                newly_filial_counter[(txt(r.get("filial_id")), txt(r.get("branch_name")))] += 1

    current_matched = safe_rows + review_rows + conflict_rows
    current_goods = {r["goodsId"] for r in safe_rows + review_rows if r.get("goodsId")}
    current_full_names = {normalize(name) for r in safe_rows + review_rows for name in r.get("provisor_fullNames", "").split(" | ") if name}
    newly_discovered_goods = sorted(current_goods - baseline_sets["goods_ids"], key=lambda x: int(x) if x.isdigit() else x)
    newly_discovered_full_names = sorted(current_full_names - baseline_sets["full_names"])[:300]
    newly_matched_products = [r for r in current_matched if txt(r.get("product_id")) not in baseline_sets["product_ids"]]
    matches_from_newly_refreshed = [r for r in safe_rows + review_rows + conflict_rows if r.get("from_newly_refreshed_accounts")]

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "read_only": True,
        "database_url_used": db_url.replace("+psycopg", ""),
        "baseline": {
            "source": str(baseline_summary_path),
            "generated_at": baseline_at,
            "SAFE": BASELINE_SAFE,
            "REVIEW": BASELINE_REVIEW,
            "CONFLICT": BASELINE_CONFLICT,
            "NOT_FOUND": BASELINE_NOT_FOUND,
        },
        "db_counts": {
            "total_products": total_products,
            "current_mapped_count": mapped_products,
            "unresolved_products_count": len(unresolved),
        },
        "refreshed_accounts": account_verification,
        "newly_refreshed_account_count": len(newly_refreshed_ids),
        "newly_refreshed_account_ids": sorted(newly_refreshed_ids, key=lambda x: int(x) if x.isdigit() else x),
        "provisor_search_pool": {
            "all_accounts": True,
            "all_non_empty_price_lists": True,
            "all_filials": True,
            "selected_and_non_selected_plk": True,
            "pool_account_ids": sorted(pool_account_ids, key=lambda x: int(x) if x.isdigit() else x),
            "newly_refreshed_accounts_included": sorted(newly_refreshed_ids & pool_account_ids, key=lambda x: int(x) if x.isdigit() else x),
            "provisor_item_rows_scanned": len(provisor_rows_raw),
            "provisor_valid_rows_indexed": len(all_provisor_rows),
            "provisor_nonempty_price_lists_scanned": len(nonempty_price_lists),
            "invalid_provisor_rows": invalid_rows,
            "full_name_paths": FULL_NAME_PATHS,
        },
        "classification_counts": {
            "SAFE": counts["SAFE"],
            "REVIEW": counts["REVIEW"],
            "CONFLICT": counts["CONFLICT"],
            "NOT_FOUND": counts["NOT_FOUND"],
        },
        "baseline_delta": {
            "SAFE": counts["SAFE"] - BASELINE_SAFE,
            "REVIEW": counts["REVIEW"] - BASELINE_REVIEW,
            "CONFLICT": counts["CONFLICT"] - BASELINE_CONFLICT,
            "NOT_FOUND": counts["NOT_FOUND"] - BASELINE_NOT_FOUND,
        },
        "newly_discovered_goodsIds": newly_discovered_goods[:300],
        "newly_discovered_goodsIds_count": len(newly_discovered_goods),
        "newly_discovered_unique_provisor_fullName_values": newly_discovered_full_names,
        "newly_discovered_unique_provisor_fullName_count": len(newly_discovered_full_names),
        "newly_matched_unresolved_products_count": len(newly_matched_products),
        "newly_matched_unresolved_products_top100": newly_matched_products[:100],
        "matches_from_newly_refreshed_accounts_count": len(matches_from_newly_refreshed),
        "matches_from_newly_refreshed_accounts_top100": matches_from_newly_refreshed[:100],
        "source_accounts_responsible_for_matches": [
            {"account_id": aid, "login": login, "matched_row_count": count}
            for (aid, login), count in matched_account_counter.most_common(50)
        ],
        "source_filials_responsible_for_matches": [
            {"filial_id": fid, "filial_name": name, "matched_row_count": count}
            for (fid, name), count in matched_filial_counter.most_common(50)
        ],
        "newly_refreshed_source_accounts_responsible": [
            {"account_id": aid, "login": login, "matched_row_count": count}
            for (aid, login), count in newly_account_counter.most_common(50)
        ],
        "newly_refreshed_source_filials_responsible": [
            {"filial_id": fid, "filial_name": name, "matched_row_count": count}
            for (fid, name), count in newly_filial_counter.most_common(50)
        ],
        "coverage_after_safe_only": {
            "current_mapped_count": mapped_products,
            "safe_candidates": len(safe_rows),
            "estimated_mapped_after_safe": mapped_products + len(safe_rows),
            "total_products": total_products,
            "current_coverage_percent": round(mapped_products / total_products * 100, 2) if total_products else 0,
            "coverage_after_safe_percent": round((mapped_products + len(safe_rows)) / total_products * 100, 2) if total_products else 0,
        },
        "files": {
            "safe": str(DIAGNOSTICS_DIR / "new_accounts_bridge_safe.csv"),
            "review": str(DIAGNOSTICS_DIR / "new_accounts_bridge_review.csv"),
            "conflicts": str(DIAGNOSTICS_DIR / "new_accounts_bridge_conflicts.csv"),
            "summary": str(DIAGNOSTICS_DIR / "new_accounts_bridge_summary.json"),
        },
        "rule": "No DB writes. Product.provisor_goods_id IS NULL only. Exact PharmCenter.name -> Provisor goods.fullName first using all fixed extraction paths, then conservative token/signature review candidates across all Provisor rows/accounts/filials/lists.",
    }

    common_fields = [
        "classification", "product_id", "product_code", "product_name", "pharmcenter_id", "pharmcenter_name",
        "goodsId", "goodsIds", "matching_row_count", "unique_filials", "unique_accounts", "brand", "dosage",
        "package_count", "dosage_form", "volume", "provisor_fullNames", "provisor_distributorGoodsNames",
        "source_price_list_ids", "source_filialIds", "source_filialNames", "source_accountIds",
        "source_accountLogins", "from_newly_refreshed_accounts", "newly_refreshed_accountIds",
        "newly_refreshed_accountLogins", "reason", "source_examples",
    ]
    write_csv(DIAGNOSTICS_DIR / "new_accounts_bridge_safe.csv", safe_rows, common_fields)
    write_csv(DIAGNOSTICS_DIR / "new_accounts_bridge_review.csv", review_rows, common_fields)
    write_csv(DIAGNOSTICS_DIR / "new_accounts_bridge_conflicts.csv", conflict_rows, common_fields + ["candidate_goodsId_counts"])
    (DIAGNOSTICS_DIR / "new_accounts_bridge_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
