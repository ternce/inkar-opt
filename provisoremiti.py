# probe_provisor_params.py
import os
import time
import requests
from urllib.parse import urlencode

TOKEN = os.getenv("PROVISOR_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiI1NzZkMTkxMi1kMjhmLTQzMmItODQxYi1hZTExZmMwMzA4ZDMiLCJodHRwOi8vc2NoZW1hcy54bWxzb2FwLm9yZy93cy8yMDA1LzA1L2lkZW50aXR5L2NsYWltcy9uYW1laWRlbnRpZmllciI6ImJjMjgxM2MzLWVhY2EtNDBhNy1hMTI4LTkzY2NjN2NiMzdjNyIsImh0dHA6Ly9zY2hlbWFzLnhtbHNvYXAub3JnL3dzLzIwMDUvMDUvaWRlbnRpdHkvY2xhaW1zL25hbWUiOiLQltCw0YHRg9C70LDQvS3QpNCw0YDQvCIsImh0dHA6Ly9zY2hlbWFzLnhtbHNvYXAub3JnL3dzLzIwMDUvMDUvaWRlbnRpdHkvY2xhaW1zL2hhc2giOiJBUUFBQUFFQUFDY1FBQUFBRU9COElzYjVwOEFMR2RGK1JuQWhOUHpCWVVwcU9GTThPVk8ySHdYK1dlSTU4aXhLNGVqNlJXc1BtSU5lYklxYXNnPT0iLCJDb21wYW55VHlwZSI6ItCQ0L_RgtC10LrQsCIsImh0dHA6Ly9zY2hlbWFzLm1pY3Jvc29mdC5jb20vd3MvMjAwOC8wNi9pZGVudGl0eS9jbGFpbXMvcm9sZSI6IlBoYXJtYWN5IiwiZXhwIjoxNzc4NjAwODE3LCJpc3MiOiJodHRwczovL1BoYXJtY2VudGVyLmt6IiwiYXVkIjoiUGhhcm1hY2V1dGljYWwgbWFya2V0In0.5pfSUBz1aROQe0yKaccT8OVzGFmm53XDGeRRmofvNro").strip()
BASE_URL = "https://api.provisor.kz"
FILIAL_ID = 1106

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://new.provisor.kz",
    "Referer": "https://new.provisor.kz/Price",
}

PARAM_SETS = [
    {"filialId": FILIAL_ID},

    # pagination variants
    {"filialId": FILIAL_ID, "page": 1, "pageSize": 100},
    {"filialId": FILIAL_ID, "page": 1, "pageSize": 1000},
    {"filialId": FILIAL_ID, "Page": 1, "PageSize": 100},
    {"filialId": FILIAL_ID, "skip": 0, "take": 100},
    {"filialId": FILIAL_ID, "offset": 0, "limit": 100},
    {"filialId": FILIAL_ID, "start": 0, "length": 100},

    # availability / stock filters
    {"filialId": FILIAL_ID, "onlyAvailable": "true"},
    {"filialId": FILIAL_ID, "availableOnly": "true"},
    {"filialId": FILIAL_ID, "withStock": "true"},
    {"filialId": FILIAL_ID, "storedOnly": "true"},
    {"filialId": FILIAL_ID, "inStock": "true"},

    # common status filters
    {"filialId": FILIAL_ID, "priceStatus": 1},
    {"filialId": FILIAL_ID, "status": 1},

    # date/update filters
    {"filialId": FILIAL_ID, "modifiedSince": "2026-05-01"},
    {"filialId": FILIAL_ID, "updatedSince": "2026-05-01"},
    {"filialId": FILIAL_ID, "dateFrom": "2026-05-01"},

    # field limiting variants
    {"filialId": FILIAL_ID, "fields": "id,goodsId,distributorGoodsId,goodsPrice,stored,shelfLife"},
    {"filialId": FILIAL_ID, "select": "id,goodsId,distributorGoodsId,goodsPrice,stored,shelfLife"},
]

ENDPOINTS = [
    "/Price/GetByFilialId",
    "/Price/GetByFilial",
    "/Price/GetByFilialIdPaged",
    "/Price/GetByFilialPaged",
    "/Price/GetPricesByFilialId",
    "/Price/GetByFilialIdV2",
]


def human_size(value):
    if value is None:
        return "unknown"
    try:
        n = int(value)
    except Exception:
        return str(value)
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def probe(endpoint, params):
    url = BASE_URL + endpoint
    started = time.perf_counter()

    try:
        with requests.get(
            url,
            headers=HEADERS,
            params=params,
            stream=True,
            timeout=(10, 15),
        ) as r:
            elapsed = time.perf_counter() - started
            content_length = r.headers.get("content-length")
            content_type = r.headers.get("content-type")

            first_bytes = b""
            try:
                first_bytes = next(r.iter_content(chunk_size=1024), b"")
            except Exception:
                pass

            preview = first_bytes[:300].decode("utf-8", errors="replace").replace("\n", " ")

            print("=" * 120)
            print(f"GET {endpoint}?{urlencode(params)}")
            print(f"STATUS: {r.status_code}")
            print(f"TIME_TO_FIRST_BYTE: {elapsed:.2f}s")
            print(f"CONTENT_LENGTH: {content_length} ({human_size(content_length)})")
            print(f"CONTENT_TYPE: {content_type}")
            print(f"PREVIEW: {preview}")

            return {
                "endpoint": endpoint,
                "params": params,
                "status": r.status_code,
                "content_length": content_length,
                "elapsed": elapsed,
                "preview": preview,
            }

    except Exception as e:
        elapsed = time.perf_counter() - started
        print("=" * 120)
        print(f"GET {endpoint}?{urlencode(params)}")
        print(f"ERROR after {elapsed:.2f}s: {repr(e)}")
        return None


def main():
    if not TOKEN or TOKEN == "PASTE_TOKEN_HERE":
        raise RuntimeError("Укажи PROVISOR_TOKEN или вставь токен в TOKEN")

    results = []

    for endpoint in ENDPOINTS:
        for params in PARAM_SETS:
            result = probe(endpoint, params)
            if result:
                results.append(result)
            time.sleep(0.5)

    print("\n\nBEST CANDIDATES:")
    good = []
    for r in results:
        if r["status"] == 200:
            cl = r["content_length"]
            size = int(cl) if cl and str(cl).isdigit() else None
            good.append((size if size is not None else 10**18, r))

    for size, r in sorted(good, key=lambda x: x[0])[:20]:
        print(
            f"{r['endpoint']} params={r['params']} "
            f"content_length={human_size(r['content_length'])} "
            f"time={r['elapsed']:.2f}s"
        )


if __name__ == "__main__":
    main()