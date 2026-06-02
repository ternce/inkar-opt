# provisor_json_to_csv.py
import csv
import os
import sys
import time
from datetime import datetime

import ijson


INPUT_JSON = r"exports/provisor_filial_1052_2026-05-14_04-21-14.json"  # путь к твоему JSON
OUT_DIR = "exports"

os.makedirs(OUT_DIR, exist_ok=True)


def clean(value):
    if value is None:
        return ""
    return str(value).replace("\xa0", " ").strip()


def normalize_date(value):
    if not value or value == "0001-01-01T00:00:00":
        return ""
    return str(value)[:10]


def flatten_item(item: dict) -> dict:
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


def main():
    if not os.path.exists(INPUT_JSON):
        raise FileNotFoundError(f"JSON не найден: {INPUT_JSON}")

    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_csv = os.path.join(OUT_DIR, f"provisor_emity_{now}.csv")

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

    started = time.perf_counter()
    count = 0

    print(f"[START] JSON: {INPUT_JSON}")
    print(f"[OUT] CSV: {out_csv}")

    with open(INPUT_JSON, "rb") as f_in, open(out_csv, "w", newline="", encoding="utf-8-sig") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()

        for item in ijson.items(f_in, "item"):
            writer.writerow(flatten_item(item))
            count += 1

            if count % 10000 == 0:
                elapsed = time.perf_counter() - started
                speed = count / elapsed if elapsed > 0 else 0
                print(f"[PROGRESS] rows={count:,} speed={speed:.0f} rows/s elapsed={elapsed/60:.1f} min")

    elapsed = time.perf_counter() - started

    print(f"[DONE] CSV готов: {out_csv}")
    print(f"[ROWS] {count:,}")
    print(f"[TIME] {elapsed/60:.1f} min")


if __name__ == "__main__":
    main()