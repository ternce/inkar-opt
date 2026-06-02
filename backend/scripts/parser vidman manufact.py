import asyncio
import getpass
import re
import sys
from pathlib import Path

import pandas as pd

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.widman_client import WidmanClient


OUT_FILE = "widman_manufacturers.xlsx"


def clean_text(value: object) -> str:
    text = str(value or "").replace("\xa0", " ").strip()
    return re.sub(r"\s+", " ", text)


async def main_async() -> None:
    email = input("Widman email/login: ").strip()
    password = getpass.getpass("Widman password: ")

    all_rows: list[dict] = []

    async with WidmanClient(
        login=email,
        password=password,
        auth_base_url="https://1.provizor.kz",
        price_base_url="https://prv.kz",
        login_path="/pages/login",
        timeout=180.0,
        auth_mode="playwright",
    ) as client:
        price_lists = await client.get_price_lists()
        print(f"Найдено прайсов: {len(price_lists)}")

        for i, price in enumerate(price_lists, start=1):
            print(f"[{i}/{len(price_lists)}] {price.name} / {price.id}")
            try:
                items = await client.get_price_items(price.id)
            except Exception as e:
                print(f"  ERROR: {e}")
                continue

            rows = [
                {
                    "price_id": price.id,
                    "price_name": price.name,
                    "product_name": item.name,
                    "manufacturer_raw": clean_text(item.manufacturer),
                }
                for item in items
                if clean_text(item.manufacturer) and clean_text(item.manufacturer) != "-"
            ]
            all_rows.extend(rows)
            print(f"  производителей строк: {len(rows)}")
            await asyncio.sleep(0.3)

    df = pd.DataFrame(all_rows)
    if df.empty:
        print("Производители не найдены.")
        return

    unique = (
        df[["manufacturer_raw"]]
        .drop_duplicates()
        .sort_values("manufacturer_raw")
        .reset_index(drop=True)
    )

    with pd.ExcelWriter(OUT_FILE) as writer:
        unique.to_excel(writer, sheet_name="unique_manufacturers", index=False)
        df.to_excel(writer, sheet_name="all_rows", index=False)

    print(f"Готово: {OUT_FILE}")
    print(f"Уникальных производителей: {len(unique)}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
