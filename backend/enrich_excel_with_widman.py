import asyncio
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from app.services.widman_client import WidmanClient


INPUT_FILE = "ЦЕНЫ АКТАУ.xlsx"
OUTPUT_DIR = Path("exports")

ACCOUNTS = [
    {
        "name": '10008211 ИП "БИСЕКЕШОВА Ж.С." SuperVIP - Видман',
        "login": "Zhadi-90@mail.ru",
        "password": "Ss123456789",
    },
    {
        "name": '10011825 ИП "СУЛЕЙМАНОВА Р.Д." SuperVIP - Видман',
        "login": "dovulbaevaamangul@gmail.com",
        "password": "85560353",
    },
]


def norm_text(value):
    if pd.isna(value):
        return ""
    value = str(value).lower().replace("ё", "е")
    value = re.sub(r"[^а-яa-z0-9]+", " ", value)
    return " ".join(value.split())


def norm_date(value):
    if pd.isna(value) or value in ("", "01.01.0001", "0001-01-01"):
        return ""
    dt = pd.to_datetime(value, errors="coerce", dayfirst=True)
    if pd.isna(dt):
        return ""
    return dt.strftime("%Y-%m-%d")


def similarity(a, b):
    try:
        from rapidfuzz import fuzz
        return fuzz.token_set_ratio(a, b)
    except Exception:
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a, b).ratio() * 100


def build_product_keys(df):
    df["_name_key"] = df["Название"].apply(norm_text)
    df["_manufacturer_key"] = df["Производитель дистрибьютора"].apply(norm_text)
    df["_expiry_key"] = df["Срок годности"].apply(norm_date)
    return df


def find_match(item, products):
    item_name = norm_text(item.name)
    item_manufacturer = norm_text(item.manufacturer)
    item_expiry = norm_date(item.expiry_date)

    exact = products[
        (products["_name_key"] == item_name)
        & (products["_manufacturer_key"] == item_manufacturer)
        & ((products["_expiry_key"] == item_expiry) | (products["_expiry_key"] == "") | (item_expiry == ""))
    ]

    if not exact.empty:
        return exact.index[0], 100

    best_idx = None
    best_score = 0

    for idx, row in products.iterrows():
        name_score = similarity(item_name, row["_name_key"])
        manufacturer_score = similarity(item_manufacturer, row["_manufacturer_key"])

        score = (name_score * 0.75) + (manufacturer_score * 0.25)

        if item_expiry and row["_expiry_key"] and item_expiry != row["_expiry_key"]:
            score -= 10

        if score > best_score:
            best_score = score
            best_idx = idx

    if best_score >= 88:
        return best_idx, best_score

    return None, best_score


async def load_widman_prices():
    all_price_lists = []

    for account in ACCOUNTS:
        async with WidmanClient(
            login=account["login"],
            password=account["password"],
            auth_mode="playwright",
            timeout=120,
        ) as client:
            price_lists = await client.get_price_lists()
            print(f"[OK] {account['login']} | прайсов: {len(price_lists)}")

            for price_list in price_lists:
                try:
                    items = await client.get_price_items(price_list.id)
                    print(f"[LOAD] {price_list.name} | товаров: {len(items)}")

                    all_price_lists.append({
                        "account": account,
                        "price_list": price_list,
                        "items": items,
                    })

                except Exception as e:
                    print(f"[ERROR] {price_list.id} | {price_list.name}: {e}")

    return all_price_lists


async def main():
    input_path = Path(INPUT_FILE)

    if not input_path.exists():
        raise FileNotFoundError(f"Не найден файл: {input_path.resolve()}")

    df = pd.read_excel(input_path)
    df = build_product_keys(df)

    widman_price_lists = await load_widman_prices()

    for block in widman_price_lists:
        account = block["account"]
        price_list = block["price_list"]
        items = block["items"]

        safe_name = f"{price_list.name} — {account['login']}"
        price_col = f"Цена {safe_name}"
        stock_col = f"Остаток {safe_name}"

        df[price_col] = None
        df[stock_col] = None

        matched = 0

        for item in items:
            if item.price is None:
                continue

            idx, score = find_match(item, df)

            if idx is None:
                continue

            current_price = df.at[idx, price_col]

            if pd.isna(current_price) or float(item.price) < float(current_price):
                df.at[idx, price_col] = float(item.price)
                df.at[idx, stock_col] = float(item.stock) if item.stock is not None else None

            matched += 1

        print(f"[MATCH] {safe_name} | matched: {matched}")

    df = df.drop(columns=["_name_key", "_manufacturer_key", "_expiry_key"], errors="ignore")

    OUTPUT_DIR.mkdir(exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_path = OUTPUT_DIR / f"prices_with_widman_{now}.xlsx"

    df.to_excel(output_path, index=False)

    print(f"[DONE] Готово: {output_path.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())