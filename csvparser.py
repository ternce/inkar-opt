import os
import csv
import time
import requests
from datetime import datetime
from typing import Any, Dict, List, Optional
from collections import OrderedDict


TOKEN = os.getenv("PROVISOR_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "Не найден PROVISOR_TOKEN. "
        "Установи переменную окружения PROVISOR_TOKEN перед запуском."
    )

BASE_URL = "https://api.provisor.kz"

FILIALS = {
    1108: "Эмити Kostanay",
}

FILIAL_IDS = list(FILIALS.keys())

EXPORT_DIR = "exports"
os.makedirs(EXPORT_DIR, exist_ok=True)

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0",
}

POSITION_LIMIT = 50


def fetch_price_data(session: requests.Session, filial_id: int, timeout: int = 120) -> List[Dict[str, Any]]:
    url = f"{BASE_URL}/Price/GetByFilialId?filialId={filial_id}"
    print(f"[LOAD] filial={filial_id} {FILIALS.get(filial_id, '')}")

    started = time.time()
    response = session.get(url, headers=HEADERS, timeout=timeout)
    duration = round(time.time() - started, 3)

    response.raise_for_status()
    data = response.json()

    if not isinstance(data, list):
        raise ValueError(f"Unexpected response for filial={filial_id}: {type(data)}")

    print(f"[OK] filial={filial_id}: {len(data)} rows, {duration}s")
    return data


def normalize_str(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def flatten_item(item: Dict[str, Any], filial_name: str) -> Dict[str, Any]:
    goods = item.get("goods") or {}

    return {
        "id": item.get("id"),
        "goodsId": item.get("goodsId"),
        "filialId": item.get("filialId"),
        "filialName": filial_name,

        "distributorGoodsId": normalize_str(item.get("distributorGoodsId")),
        "distributorGoodsName": normalize_str(item.get("distributorGoodsName")),
        "distributorProducer": normalize_str(item.get("distributorProducer")),

        "goodsFullName": normalize_str(goods.get("fullName")),
        "goodsNameId": goods.get("nameId"),
        "goodsBrandId": goods.get("brandId"),
        "goodsProducerId": goods.get("producerId"),
        "goodsInnId": goods.get("innId"),
        "goodsDoseId": goods.get("doseId"),
        "goodsDosageFormId": goods.get("dosageFormId"),
        "goodsNumber": goods.get("number"),

        "price": item.get("goodsPrice"),
        "stored": item.get("stored"),
        "shelfLife": item.get("shelfLife"),
        "priceStatus": item.get("priceStatus"),
    }


def make_position_key(row: Dict[str, Any]) -> str:
    if row.get("goodsId") is not None:
        return f"goodsId:{row['goodsId']}"

    if row.get("distributorGoodsId"):
        return f"distributorGoodsId:{row['distributorGoodsId']}"

    return f"name:{row.get('distributorGoodsName') or row.get('goodsFullName')}"


def select_all_prices_for_50_positions(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()

    for row in rows:
        key = make_position_key(row)

        if key not in grouped and len(grouped) >= POSITION_LIMIT:
            continue

        grouped.setdefault(key, []).append(row)

    selected_rows: List[Dict[str, Any]] = []

    for position_index, (position_key, position_rows) in enumerate(grouped.items(), start=1):
        for price_index, row in enumerate(position_rows, start=1):
            selected_rows.append({
                "positionIndex": position_index,
                "positionKey": position_key,
                "priceIndexForPosition": price_index,
                "pricesCountForPosition": len(position_rows),
                **row,
            })

    return selected_rows


def save_csv(rows: List[Dict[str, Any]], filename_prefix: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(EXPORT_DIR, f"{filename_prefix}_{now}.csv")

    columns = [
        "positionIndex",
        "positionKey",
        "priceIndexForPosition",
        "pricesCountForPosition",

        "id",
        "goodsId",
        "filialId",
        "filialName",

        "distributorGoodsId",
        "distributorGoodsName",
        "distributorProducer",

        "goodsFullName",
        "goodsNameId",
        "goodsBrandId",
        "goodsProducerId",
        "goodsInnId",
        "goodsDoseId",
        "goodsDosageFormId",
        "goodsNumber",

        "price",
        "stored",
        "shelfLife",
        "priceStatus",
    ]

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)

    return os.path.abspath(path)


def main() -> None:
    session = requests.Session()

    all_flattened: List[Dict[str, Any]] = []

    for filial_id in FILIAL_IDS:
        filial_name = FILIALS.get(filial_id, str(filial_id))
        data = fetch_price_data(session, filial_id, timeout=120)

        flattened = [
            flatten_item(item, filial_name)
            for item in data
        ]

        all_flattened.extend(flattened)

    selected_rows = select_all_prices_for_50_positions(all_flattened)
    unique_positions = len({row["positionKey"] for row in selected_rows})

    csv_path = save_csv(selected_rows, "provisor_50_positions_all_prices")

    print("\n[DONE]")
    print(f"CSV:              {csv_path}")
    print(f"Unique positions: {unique_positions}")
    print(f"CSV rows:         {len(selected_rows)}")


if __name__ == "__main__":
    main()