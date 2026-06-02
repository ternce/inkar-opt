# emity_filter_by_our_sku.py
import csv
import os
import time
from datetime import datetime, timedelta

import ijson
import pandas as pd
import requests


# =========================
# CONFIG
# =========================

BASE_URL = "https://api.provisor.kz"

LOGIN = "Aksai4/83"
PASSWORD = "va5y4f"

# Excel с нашим прайсом
OUR_PRICE_XLSX = r"ПРАЙС V2.xlsx"

# Колонка с нашей SKU. Если не знаешь название — оставь None, скрипт попробует найти сам.
SKU_COLUMN = None
# Например можно вручную:
# SKU_COLUMN = "SKU"

# Филиал-эталон, где наш SKU == distributorGoodsId
REFERENCE_FILIAL_ID = 128  # Инкар Алматы

# Огромный JSON Emity, который уже скачан
EMITY_JSON = r"exports/provisor_filial_1052.json"

OUT_DIR = "exports_emity_filtered"

# Для агрегированной цены
MIN_SHELF_LIFE_DAYS = 90

os.makedirs(OUT_DIR, exist_ok=True)


# =========================
# HELPERS
# =========================

def clean(value):
    if value is None:
        return ""
    return str(value).replace("\xa0", " ").strip()


def normalize_sku(value):
    text = clean(value)
    if not text:
        return ""

    # Excel иногда делает 1000636.0
    if text.endswith(".0"):
        text = text[:-2]

    text = text.replace(" ", "").replace("-", "")
    return text


def normalize_sku_loose(value):
    text = normalize_sku(value)
    return text.lstrip("0") or text


def parse_float(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return None


def normalize_date(value):
    if not value or value == "0001-01-01T00:00:00":
        return ""
    return str(value)[:10]


def parse_date(value):
    value = normalize_date(value)
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def human_time(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f} min"
    return f"{minutes / 60:.1f} h"


# =========================
# PROVISOR
# =========================

def get_token():
    resp = requests.post(
        f"{BASE_URL}/Token/CreateAll",
        json={"login": LOGIN, "password": PASSWORD},
        timeout=(15, 60),
    )
    resp.raise_for_status()

    data = resp.json()
    token = data.get("accessToken")
    if not token:
        raise RuntimeError(f"Не получил accessToken: {data}")

    return token


def get_reference_items(token):
    print(f"[REF] Загружаю reference filial={REFERENCE_FILIAL_ID}")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Origin": "https://new.provisor.kz",
        "Referer": "https://new.provisor.kz/Price",
        "User-Agent": "Mozilla/5.0",
    }

    resp = requests.get(
        f"{BASE_URL}/Price/GetByFilialId",
        headers=headers,
        params={"filialId": REFERENCE_FILIAL_ID},
        timeout=(20, 300),
    )
    resp.raise_for_status()

    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError("Reference filial вернул не список")

    print(f"[REF] Строк получено: {len(data):,}")
    return data


# =========================
# STEP 1: LOAD OUR SKU
# =========================

def load_our_skus():
    print(f"[SKU] Читаю Excel: {OUR_PRICE_XLSX}")

    df = pd.read_excel(OUR_PRICE_XLSX)

    if df.empty:
        raise RuntimeError("Excel пустой")

    print("[SKU] Колонки Excel:")
    for col in df.columns:
        print(f"  - {col}")

    sku_col = SKU_COLUMN

    if sku_col is None:
        possible = [
            "SKU",
            "sku",
            "Код",
            "Код товара",
            "Артикул",
            "Номенклатура",
            "Код позиции",
            "product_code",
            "code",
        ]

        for col in df.columns:
            if str(col).strip() in possible:
                sku_col = col
                break

    if sku_col is None:
        raise RuntimeError(
            "Не смог найти колонку SKU. Укажи вручную SKU_COLUMN = 'название колонки'"
        )

    print(f"[SKU] Использую колонку: {sku_col}")

    skus_exact = set()
    skus_loose = set()

    for value in df[sku_col].dropna():
        sku = normalize_sku(value)
        if sku:
            skus_exact.add(sku)
            skus_loose.add(normalize_sku_loose(sku))

    print(f"[SKU] Наших SKU: exact={len(skus_exact):,}, loose={len(skus_loose):,}")

    return skus_exact, skus_loose


# =========================
# STEP 2: MAP SKU -> GOODS ID
# =========================

def build_needed_goods_ids(reference_items, skus_exact, skus_loose):
    needed_goods_ids = set()
    mapping_rows = []

    skipped_no_sku = 0
    skipped_no_goods_id = 0
    skipped_not_found = 0

    for item in reference_items:
        distributor_goods_id = normalize_sku(item.get("distributorGoodsId"))
        distributor_goods_id_loose = normalize_sku_loose(distributor_goods_id)

        goods_id = item.get("goodsId")

        if not distributor_goods_id:
            skipped_no_sku += 1
            continue

        if not goods_id:
            skipped_no_goods_id += 1
            continue

        matched = (
            distributor_goods_id in skus_exact
            or distributor_goods_id_loose in skus_loose
        )

        if not matched:
            skipped_not_found += 1
            continue

        needed_goods_ids.add(str(goods_id))

        goods = item.get("goods") or {}

        mapping_rows.append({
            "our_sku": distributor_goods_id,
            "provisor_goods_id": goods_id,
            "reference_distributor_goods_id": distributor_goods_id,
            "reference_name": clean(goods.get("fullName") or item.get("distributorGoodsName")),
            "reference_manufacturer": clean(item.get("distributorProducer")),
        })

    print(f"[MAP] goodsId найдено: {len(needed_goods_ids):,}")
    print(f"[MAP] rows mapping: {len(mapping_rows):,}")
    print(f"[MAP] skipped_no_sku={skipped_no_sku:,}")
    print(f"[MAP] skipped_no_goods_id={skipped_no_goods_id:,}")
    print(f"[MAP] skipped_product_not_found={skipped_not_found:,}")

    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    mapping_csv = os.path.join(OUT_DIR, f"sku_to_goods_id_mapping_{now}.csv")

    with open(mapping_csv, "w", newline="", encoding="utf-8-sig") as f:
        fieldnames = [
            "our_sku",
            "provisor_goods_id",
            "reference_distributor_goods_id",
            "reference_name",
            "reference_manufacturer",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(mapping_rows)

    print(f"[MAP] mapping CSV: {mapping_csv}")

    return needed_goods_ids


# =========================
# STEP 3: FILTER HUGE EMITY JSON
# =========================

def flatten_emity_item(item):
    goods = item.get("goods") or {}
    filial = item.get("filial") or {}

    return {
        "row_id": item.get("id"),
        "goods_id": item.get("goodsId"),
        "filial_id": item.get("filialId"),
        "filial_name": clean(filial.get("name")),

        "distributor_goods_id": clean(item.get("distributorGoodsId")),
        "goods_full_name": clean(goods.get("fullName")),
        "distributor_goods_name": clean(item.get("distributorGoodsName")),
        "distributor_producer": clean(item.get("distributorProducer")),

        "price": item.get("goodsPrice"),
        "stock": item.get("stored"),
        "shelf_life": normalize_date(item.get("shelfLife")),
        "batch": clean(item.get("batch")),

        "price_status": item.get("priceStatus"),
        "pack": item.get("pack"),
        "box": item.get("box"),
        "multiplicity": item.get("multiplicity"),
        "min_order": item.get("minOrder"),
    }


def update_aggregate(agg, row):
    goods_id = str(row["goods_id"])
    price = parse_float(row["price"])
    stock = parse_float(row["stock"]) or 0
    shelf_date = parse_date(row["shelf_life"])

    if goods_id not in agg:
        agg[goods_id] = {
            "goods_id": goods_id,
            "rows_count": 0,
            "total_stock": 0,
            "min_price_any": None,
            "min_price_valid_shelf": None,
            "best_shelf_life": "",
            "sample_name": row["goods_full_name"] or row["distributor_goods_name"],
            "sample_manufacturer": row["distributor_producer"],
            "sample_distributor_goods_id": row["distributor_goods_id"],
        }

    a = agg[goods_id]
    a["rows_count"] += 1
    a["total_stock"] += stock

    if price is not None and price > 0 and stock > 0:
        if a["min_price_any"] is None or price < a["min_price_any"]:
            a["min_price_any"] = price

        min_valid_date = datetime.now().date() + timedelta(days=MIN_SHELF_LIFE_DAYS)
        if shelf_date is None or shelf_date >= min_valid_date:
            if a["min_price_valid_shelf"] is None or price < a["min_price_valid_shelf"]:
                a["min_price_valid_shelf"] = price

    if shelf_date:
        current_best = parse_date(a["best_shelf_life"])
        if current_best is None or shelf_date > current_best:
            a["best_shelf_life"] = shelf_date.isoformat()


def filter_emity_json(needed_goods_ids):
    if not os.path.exists(EMITY_JSON):
        raise FileNotFoundError(f"Emity JSON не найден: {EMITY_JSON}")

    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    raw_csv = os.path.join(OUT_DIR, f"emity_filtered_raw_{now}.csv")
    agg_csv = os.path.join(OUT_DIR, f"emity_filtered_aggregated_{now}.csv")

    fieldnames = [
        "row_id",
        "goods_id",
        "filial_id",
        "filial_name",
        "distributor_goods_id",
        "goods_full_name",
        "distributor_goods_name",
        "distributor_producer",
        "price",
        "stock",
        "shelf_life",
        "batch",
        "price_status",
        "pack",
        "box",
        "multiplicity",
        "min_order",
    ]

    agg = {}

    total_rows = 0
    matched_rows = 0
    started = time.perf_counter()

    print(f"[FILTER] JSON: {EMITY_JSON}")
    print(f"[FILTER] needed goodsId: {len(needed_goods_ids):,}")
    print(f"[FILTER] RAW OUT: {raw_csv}")

    with open(EMITY_JSON, "rb") as f_in, open(
        raw_csv,
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as f_out:
        writer = csv.DictWriter(f_out, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()

        for item in ijson.items(f_in, "item"):
            total_rows += 1

            goods_id = item.get("goodsId")
            if goods_id is None:
                continue

            goods_id_str = str(goods_id)
            if goods_id_str not in needed_goods_ids:
                continue

            row = flatten_emity_item(item)
            writer.writerow(row)
            update_aggregate(agg, row)
            matched_rows += 1

            if total_rows % 50000 == 0:
                elapsed = time.perf_counter() - started
                speed = total_rows / elapsed if elapsed > 0 else 0
                print(
                    f"[PROGRESS] scanned={total_rows:,} "
                    f"matched={matched_rows:,} "
                    f"unique_goods={len(agg):,} "
                    f"speed={speed:.0f} rows/s "
                    f"elapsed={human_time(elapsed)}"
                )

    print(f"[FILTER] Пишу aggregated CSV: {agg_csv}")

    with open(agg_csv, "w", newline="", encoding="utf-8-sig") as f:
        fieldnames_agg = [
            "goods_id",
            "rows_count",
            "total_stock",
            "min_price_any",
            "min_price_valid_shelf",
            "best_shelf_life",
            "sample_distributor_goods_id",
            "sample_name",
            "sample_manufacturer",
        ]

        writer = csv.DictWriter(f, fieldnames=fieldnames_agg, delimiter=";")
        writer.writeheader()

        for row in agg.values():
            writer.writerow(row)

    elapsed = time.perf_counter() - started

    print("[DONE]")
    print(f"[SCANNED] {total_rows:,}")
    print(f"[MATCHED RAW ROWS] {matched_rows:,}")
    print(f"[UNIQUE GOODS] {len(agg):,}")
    print(f"[TIME] {human_time(elapsed)}")
    print(f"[RAW CSV] {raw_csv}")
    print(f"[AGG CSV] {agg_csv}")


# =========================
# MAIN
# =========================

def main():
    skus_exact, skus_loose = load_our_skus()

    token = get_token()
    reference_items = get_reference_items(token)

    needed_goods_ids = build_needed_goods_ids(
        reference_items=reference_items,
        skus_exact=skus_exact,
        skus_loose=skus_loose,
    )

    if not needed_goods_ids:
        raise RuntimeError("needed_goods_ids пустой. Проверь SKU_COLUMN и reference filial.")

    filter_emity_json(needed_goods_ids)


if __name__ == "__main__":
    main()