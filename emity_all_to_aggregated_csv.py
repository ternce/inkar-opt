# emity_all_raw_to_csv.py
import csv
import os
import time
from datetime import datetime

import ijson

INPUT_JSON = r"exports/provisor_filial_1049.json"  # замени на свой путь
OUT_DIR = "exports_emity_raw_all"

os.makedirs(OUT_DIR, exist_ok=True)


def clean(value):
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ").strip()
    if text.startswith(("=", "+", "-", "@")):
        text = "'" + text
    return text


def normalize_date(value):
    if not value or value == "0001-01-01T00:00:00":
        return ""
    return str(value)[:10]


def row_from_item(item: dict) -> dict:
    goods = item.get("goods") or {}
    filial = item.get("filial") or {}

    return {
        "goods_id": item.get("goodsId"),
        "row_id": item.get("id"),
        "filial_id": item.get("filialId"),
        "filial_name": clean(filial.get("name")),
        "Остаток": item.get("stored"),
        "Цена": item.get("goodsPrice"),
        "shelf_life": normalize_date(item.get("shelfLife")),
        "batch": clean(item.get("batch")),
        "АЙДИ У Дистрибьютера": clean(item.get("distributorGoodsId")),
        "Название": clean(goods.get("fullName") or item.get("distributorGoodsName")),
        "Название дистрибьютора": clean(item.get("distributorGoodsName")),
        "Производитель": clean(item.get("distributorProducer")),
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
    out_csv = os.path.join(OUT_DIR, f"emity_all_raw_{now}.csv")

    fieldnames = [
        "goods_id",
        "row_id",
        "filial_id",
        "filial_name",
        "Остаток",
        "Цена",
        "shelf_life",
        "batch",
        "АЙДИ У Дистрибьютера",
        "Название",
        "Название дистрибьютора",
        "Производитель",
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
            writer.writerow(row_from_item(item))
            count += 1

            if count % 50000 == 0:
                elapsed = time.perf_counter() - started
                speed = count / elapsed if elapsed > 0 else 0
                print(
                    f"[PROGRESS] rows={count:,} "
                    f"speed={speed:.0f} rows/s "
                    f"elapsed={elapsed/60:.1f} min"
                )

    elapsed = time.perf_counter() - started
    print("[DONE]")
    print(f"[ROWS] {count:,}")
    print(f"[TIME] {elapsed/60:.1f} min")
    print(f"[CSV] {out_csv}")


if __name__ == "__main__":
    main()