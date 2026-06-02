# emity_aggregate_all_goods.py
import csv
import os
import time
from datetime import datetime, timedelta

import ijson


INPUT_JSON = r"exports/provisor_filial_1049.json"  # путь к Emity JSON
OUT_DIR = "exports_emity_all_aggregated"

MIN_SHELF_LIFE_DAYS = 90

os.makedirs(OUT_DIR, exist_ok=True)


def clean(value):
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ").strip()

    # чтобы Excel не воспринимал как формулу
    if text.startswith(("=", "+", "-", "@")):
        text = "'" + text

    return text


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


def update_aggregate(agg: dict, item: dict):
    goods_id = item.get("goodsId")
    if goods_id is None:
        return False

    goods_id = str(goods_id)

    goods = item.get("goods") or {}

    price = parse_float(item.get("goodsPrice"))
    stock = parse_float(item.get("stored")) or 0
    shelf_life = normalize_date(item.get("shelfLife"))
    shelf_date = parse_date(shelf_life)

    name = clean(goods.get("fullName") or item.get("distributorGoodsName"))
    manufacturer = clean(item.get("distributorProducer"))
    distributor_goods_id = clean(item.get("distributorGoodsId"))

    if goods_id not in agg:
        agg[goods_id] = {
            "goods_id": goods_id,
            "rows_count": 0,
            "total_stock": 0.0,
            "min_price_any": None,
            "min_price_valid_shelf": None,
            "best_shelf_life": "",
            "sample_distributor_goods_id": distributor_goods_id,
            "sample_name": name,
            "sample_manufacturer": manufacturer,
        }

    a = agg[goods_id]
    a["rows_count"] += 1
    a["total_stock"] += stock

    if not a["sample_name"] and name:
        a["sample_name"] = name

    if not a["sample_manufacturer"] and manufacturer:
        a["sample_manufacturer"] = manufacturer

    if not a["sample_distributor_goods_id"] and distributor_goods_id:
        a["sample_distributor_goods_id"] = distributor_goods_id

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

    return True


def main():
    if not os.path.exists(INPUT_JSON):
        raise FileNotFoundError(f"JSON не найден: {INPUT_JSON}")

    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_csv = os.path.join(OUT_DIR, f"emity_all_unique_goods_{now}.csv")

    agg = {}
    scanned = 0
    with_goods_id = 0
    started = time.perf_counter()

    print(f"[START] JSON: {INPUT_JSON}")
    print(f"[OUT] CSV: {out_csv}")

    with open(INPUT_JSON, "rb") as f:
        for item in ijson.items(f, "item"):
            scanned += 1

            if update_aggregate(agg, item):
                with_goods_id += 1

            if scanned % 50000 == 0:
                elapsed = time.perf_counter() - started
                speed = scanned / elapsed if elapsed > 0 else 0
                print(
                    f"[PROGRESS] scanned={scanned:,} "
                    f"with_goods_id={with_goods_id:,} "
                    f"unique_goods={len(agg):,} "
                    f"speed={speed:.0f} rows/s "
                    f"elapsed={elapsed/60:.1f} min"
                )

    print(f"[WRITE] Пишу CSV... unique_goods={len(agg):,}")

    fieldnames = [
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

    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()

        for row in agg.values():
            writer.writerow(row)

    elapsed = time.perf_counter() - started

    print("[DONE]")
    print(f"[SCANNED] {scanned:,}")
    print(f"[WITH GOODS ID] {with_goods_id:,}")
    print(f"[UNIQUE GOODS] {len(agg):,}")
    print(f"[TIME] {elapsed/60:.1f} min")
    print(f"[CSV] {out_csv}")


if __name__ == "__main__":
    main()