# export_single_goods_raw_json.py
import json
import os
import time
from decimal import Decimal

import ijson


INPUT_JSON = r"exports/provisor_filial_1052.json"
TARGET_GOODS_ID = "47033"

OUT_DIR = "exports_single_goods_json"

os.makedirs(OUT_DIR, exist_ok=True)


def json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


def main():
    if not os.path.exists(INPUT_JSON):
        raise FileNotFoundError(f"JSON не найден: {INPUT_JSON}")

    out_json = os.path.join(
        OUT_DIR,
        f"goods_{TARGET_GOODS_ID}.json"
    )

    scanned = 0
    matched = 0

    result = []

    started = time.perf_counter()

    print(f"[START] goods_id={TARGET_GOODS_ID}")

    with open(INPUT_JSON, "rb") as f:
        for item in ijson.items(f, "item"):
            scanned += 1

            goods_id = item.get("goodsId")

            if goods_id is None:
                continue

            if str(goods_id) != str(TARGET_GOODS_ID):
                continue

            result.append(item)
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

    with open(out_json, "w", encoding="utf-8") as f_out:
        json.dump(
            result,
            f_out,
            ensure_ascii=False,
            indent=2,
            default=json_default,
        )

    elapsed = time.perf_counter() - started

    print("[DONE]")
    print(f"[SCANNED] {scanned:,}")
    print(f"[MATCHED] {matched:,}")
    print(f"[TIME] {elapsed/60:.1f} min")
    print(f"[JSON] {out_json}")


if __name__ == "__main__":
    main()