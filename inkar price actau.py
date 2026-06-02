# download_provisor_heavy.py
import os
import time
import requests
from datetime import datetime

BASE_URL = "https://api.provisor.kz"

LOGIN = "Жасулан-Фарм"
PASSWORD = "159352"

FILIAL_ID = 1049  # ID Emity Алматы

OUT_DIR = "exports"
CHUNK_SIZE = 1024 * 1024  # 1 MB

os.makedirs(OUT_DIR, exist_ok=True)


def get_token() -> str:
    url = f"{BASE_URL}/Token/CreateAll"

    resp = requests.post(
        url,
        json={"login": LOGIN, "password": PASSWORD},
        timeout=(15, 60),
    )
    resp.raise_for_status()

    data = resp.json()
    token = data.get("accessToken")

    if not token:
        raise RuntimeError(f"Не получил accessToken: {data}")

    return token


def human_mb(value: float | int) -> str:
    return f"{float(value) / 1024 / 1024:.2f} MB"


def human_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f} min"
    return f"{minutes / 60:.1f} h"


def download_price(token: str) -> str:
    url = f"{BASE_URL}/Price/GetByFilialId"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://new.provisor.kz",
        "Referer": "https://new.provisor.kz/Price",
    }

    params = {"filialId": FILIAL_ID}

    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_path = os.path.join(OUT_DIR, f"provisor_filial_{FILIAL_ID}_{now}.json")

    print(f"[START] filialId={FILIAL_ID}")
    print(f"[OUT] {out_path}")

    started = time.perf_counter()
    downloaded = 0

    with requests.get(
        url,
        headers=headers,
        params=params,
        stream=True,
        timeout=(20, 600),
    ) as resp:
        print(f"[STATUS] {resp.status_code}")
        print(f"[CONTENT_LENGTH] {resp.headers.get('content-length')}")
        print(f"[CONTENT_TYPE] {resp.headers.get('content-type')}")

        resp.raise_for_status()

        content_length = resp.headers.get("content-length")
        total_size = int(content_length) if content_length and content_length.isdigit() else None

        if total_size:
            print(f"[TOTAL_SIZE] {human_mb(total_size)}")

        with open(out_path, "wb") as f:
            last_log = time.perf_counter()

            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue

                f.write(chunk)
                downloaded += len(chunk)

                now_time = time.perf_counter()

                if now_time - last_log >= 5:
                    elapsed = now_time - started
                    speed = downloaded / elapsed if elapsed > 0 else 0

                    if total_size:
                        percent = downloaded / total_size * 100
                        remaining = max(total_size - downloaded, 0)
                        eta = remaining / speed if speed > 0 else 0

                        print(
                            f"[PROGRESS] {percent:.2f}% | "
                            f"{human_mb(downloaded)} / {human_mb(total_size)} | "
                            f"speed={human_mb(speed)}/s | "
                            f"elapsed={human_time(elapsed)} | "
                            f"eta={human_time(eta)}"
                        )
                    else:
                        print(
                            f"[PROGRESS] downloaded={human_mb(downloaded)} | "
                            f"speed={human_mb(speed)}/s | "
                            f"elapsed={human_time(elapsed)}"
                        )

                    last_log = now_time

    elapsed = time.perf_counter() - started

    print(f"[DONE] saved={out_path}")
    print(f"[SIZE] {human_mb(downloaded)}")
    print(f"[TIME] {human_time(elapsed)}")

    return out_path


def main():
    token = get_token()
    download_price(token)


if __name__ == "__main__":
    main()