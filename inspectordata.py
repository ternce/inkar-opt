# export_single_goods_full_debug_with_raw_json.py
import csv
import json
import os
import time
from datetime import datetime
from decimal import Decimal

import ijson


INPUT_JSON = r"exports/provisor_filial_1106.json"
TARGET_GOODS_ID = "47033"

OUT_DIR = "exports_single_goods_debug"

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


def normalize_datetime(value):
    if not value or value == "0001-01-01T00:00:00":
        return ""
    return str(value).replace("T", " ")[:19]


def normalize_number(value):
    if value is None:
        return ""
    if isinstance(value, Decimal):
        value = float(value)
    try:
        return str(round(float(value), 2)).replace(".", ",")
    except Exception:
        return str(value)


def raw_json(item: dict) -> str:
    return json.dumps(item, ensure_ascii=False, default=str)


def row_from_item(item: dict) -> dict:
    goods = item.get("goods") or {}
    filial = item.get("filial") or {}

    return {
        "goods_id": item.get("goodsId"),
        "row_id": item.get("id"),
        "filial_id": item.get("filialId"),
        "Филиал": clean(filial.get("name")),

        "АЙДИ У Дистрибьютера": clean(item.get("distributorGoodsId")),
        "Название": clean(goods.get("fullName")),
        "Название дистрибьютора": clean(item.get("distributorGoodsName")),
        "Производитель": clean(item.get("distributorProducer")),

        "categoryId": goods.get("categoryId"),
        "nameId": goods.get("nameId"),
        "dosageFormId": goods.get("dosageFormId"),
        "doseId": goods.get("doseId"),
        "genericId": goods.get("genericId"),
        "atcId": goods.get("atcId"),
        "euroATCId": goods.get("euroATCId"),
        "brandId": goods.get("brandId"),
        "producerId": goods.get("producerId"),
        "corporationId": goods.get("corporationId"),
        "countryId": goods.get("countryId"),
        "innId": goods.get("innId"),
        "rxOTCId": goods.get("rxOTCId"),
        "number": goods.get("number"),
        "regNumber": clean(goods.get("regNumber")),
        "regDateStart": normalize_date(goods.get("regDateStart")),
        "regDateEnd": normalize_date(goods.get("regDateEnd")),

        "Цена": normalize_number(item.get("goodsPrice")),
        "Цена со скидкой": normalize_number(item.get("goodsPriceWithUserDiscount")),
        "Скидка пользователя": normalize_number(item.get("userDiscount")),
        "Остаток": normalize_number(item.get("stored")),

        "Дата прайса": normalize_datetime(item.get("insertedDate")),
        "Срок годности": normalize_date(item.get("shelfLife")),
        "Партия": clean(item.get("batch")),

        "Статус цены": clean(item.get("priceStatus")),
        "isPromotion": item.get("isPromotion"),
        "isExclusive": item.get("isExclusive"),
        "isntDiscount": item.get("isntDiscount"),
        "isMarked": item.get("isMarked"),

        "pack": item.get("pack"),
        "box": item.get("box"),
        "multiplicity": item.get("multiplicity"),
        "min_order": item.get("minOrder"),

        "distributorContractId": item.get("distributorContractId"),
        "priceScales": clean(item.get("priceScales")),
        "priceFilialPayments": clean(item.get("priceFilialPayments")),
        "priceFilialPromos": clean(item.get("priceFilialPromos")),
        "priceFilialWorkloads": clean(item.get("priceFilialWorkloads")),
        "maxOrderDetails": clean(item.get("maxOrderDetails")),

        "RAW_JSON": raw_json(item),
    }


def main():
    if not os.path.exists(INPUT_JSON):
        raise FileNotFoundError(f"JSON не найден: {INPUT_JSON}")

    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_csv = os.path.join(OUT_DIR, f"goods_debug_{TARGET_GOODS_ID}_{now}.csv")

    fieldnames = [
        "goods_id", "row_id", "filial_id", "Филиал",
        "АЙДИ У Дистрибьютера", "Название", "Название дистрибьютора", "Производитель",

        "categoryId", "nameId", "dosageFormId", "doseId", "genericId",
        "atcId", "euroATCId", "brandId", "producerId", "corporationId",
        "countryId", "innId", "rxOTCId", "number",
        "regNumber", "regDateStart", "regDateEnd",

        "Цена", "Цена со скидкой", "Скидка пользователя", "Остаток",
        "Дата прайса", "Срок годности", "Партия",

        "Статус цены", "isPromotion", "isExclusive", "isntDiscount", "isMarked",
        "pack", "box", "multiplicity", "min_order",

        "distributorContractId",
        "priceScales", "priceFilialPayments", "priceFilialPromos",
        "priceFilialWorkloads", "maxOrderDetails",

        "RAW_JSON",
    ]

    started = time.perf_counter()
    scanned = 0
    matched = 0

    print(f"[START] goods_id={TARGET_GOODS_ID}")
    print(f"[OUT] {out_csv}")

    with open(INPUT_JSON, "rb") as f_in, open(
        out_csv, "w", newline="", encoding="utf-8-sig"
    ) as f_out:
        writer = csv.DictWriter(f_out, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()

        for item in ijson.items(f_in, "item"):
            scanned += 1

            if str(item.get("goodsId")) != str(TARGET_GOODS_ID):
                continue

            writer.writerow(row_from_item(item))
            matched += 1

            if matched % 50 == 0:
                print(f"[MATCHED] {matched}")

            if scanned % 50000 == 0:
                elapsed = time.perf_counter() - started
                speed = scanned / elapsed if elapsed > 0 else 0
                print(
                    f"[PROGRESS] scanned={scanned:,} "
                    f"matched={matched:,} "
                    f"speed={speed:.0f} rows/s"
                )

    elapsed = time.perf_counter() - started

    print("[DONE]")
    print(f"[SCANNED] {scanned:,}")
    print(f"[MATCHED] {matched:,}")
    print(f"[TIME] {elapsed/60:.1f} min")
    print(f"[CSV] {out_csv}")


if __name__ == "__main__":
    main()