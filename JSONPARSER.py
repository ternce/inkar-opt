import os
import json
import time
import requests
from datetime import datetime
from typing import Any, Dict, List, Optional


# =========================
# НАСТРОЙКИ
# =========================

TOKEN = os.getenv("PROVISOR_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "Не найден PROVISOR_TOKEN. "
        "Установи переменную окружения PROVISOR_TOKEN перед запуском."
    )

BASE_URL = "https://api.provisor.kz"

FILIALS = {
    149: "Medservice Астана",

}

FILIAL_IDS = list(FILIALS.keys())

EXPORT_DIR = "exports"
os.makedirs(EXPORT_DIR, exist_ok=True)

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0",
}


# =========================
# API
# =========================

def fetch_price_data(
    session: requests.Session,
    filial_id: int,
    timeout: int = 120,
) -> List[Dict[str, Any]]:
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


# =========================
# NORMALIZATION
# =========================

def normalize_str(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    return text


def flatten_item(item: Dict[str, Any], filial_name: str) -> Dict[str, Any]:
    goods = item.get("goods") or {}
    filial = item.get("filial") or {}

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

        "raw": item,
    }


# =========================
# EXPORT
# =========================

def save_json(data: Any, filename_prefix: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(EXPORT_DIR, f"{filename_prefix}_{now}.json")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return os.path.abspath(path)


# =========================
# MAIN
# =========================

def main() -> None:
    session = requests.Session()

    all_raw: List[Dict[str, Any]] = []
    all_flattened: List[Dict[str, Any]] = []

    summary = {
        "startedAt": datetime.now().isoformat(),
        "filials": [],
        "totalRows": 0,
        "successCount": 0,
        "errorCount": 0,
    }

    for filial_id in FILIAL_IDS:
        filial_name = FILIALS.get(filial_id, str(filial_id))

        try:
            data = fetch_price_data(session, filial_id, timeout=120)

            all_raw.append({
                "filialId": filial_id,
                "filialName": filial_name,
                "itemsCount": len(data),
                "items": data,
            })

            flattened = [
                flatten_item(item, filial_name)
                for item in data
            ]

            all_flattened.extend(flattened)

            summary["filials"].append({
                "filialId": filial_id,
                "filialName": filial_name,
                "status": "success",
                "itemsCount": len(data),
            })

            summary["totalRows"] += len(data)
            summary["successCount"] += 1

        except Exception as e:
            print(f"[ERROR] filial={filial_id}: {e}")

            summary["filials"].append({
                "filialId": filial_id,
                "filialName": filial_name,
                "status": "error",
                "error": str(e),
            })

            summary["errorCount"] += 1

    summary["finishedAt"] = datetime.now().isoformat()

    export_payload = {
        "summary": summary,
        "items": all_flattened,
    }

    raw_payload = {
        "summary": summary,
        "filials": all_raw,
    }

    flattened_path = save_json(export_payload, "provisor_flattened")
    raw_path = save_json(raw_payload, "provisor_raw")

    print("\n[DONE]")
    print(f"Flattened JSON: {flattened_path}")
    print(f"Raw JSON:       {raw_path}")
    print(f"Total rows:     {summary['totalRows']}")


if __name__ == "__main__":
    main()