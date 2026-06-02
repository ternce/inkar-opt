import asyncio
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from app.services.widman_client import WidmanClient


ACCOUNTS = [
    {
        "name": '10008211 ИП "БИСЕКЕШОВА Ж.С." SuperVIP - Видман',
        "login": os.getenv("WIDMAN_1_LOGIN"),
        "password": os.getenv("WIDMAN_1_PASSWORD"),
    },
    {
        "name": '10011825 ИП "СУЛЕЙМАНОВА Р.Д." SuperVIP - Видман',
        "login": os.getenv("WIDMAN_2_LOGIN"),
        "password": os.getenv("WIDMAN_2_PASSWORD"),
    },
]


async def export_account(account: dict) -> list[dict]:
    rows = []

    if not account["login"] or not account["password"]:
        print(f"[SKIP] Нет логина/пароля для {account['name']}")
        return rows

    async with WidmanClient(
        login=account["login"],
        password=account["password"],
        auth_mode="playwright",
        timeout=120,
    ) as client:
        price_lists = await client.get_price_lists()
        print(f"[OK] {account['name']} | прайсов: {len(price_lists)}")

        for price_list in price_lists:
            try:
                print(f"[LOAD] {price_list.id} | {price_list.name}")
                items = await client.get_price_items(price_list.id)

                for item in items:
                    rows.append({
                        "Аккаунт": account["name"],
                        "Логин": account["login"],
                        "ID прайса": price_list.id,
                        "Прайс": price_list.name,
                        "Наименование": item.name,
                        "Производитель": item.manufacturer,
                        "Срок годности": item.expiry_date,
                        "Цена": float(item.price) if item.price is not None else None,
                        "Кол. в уп.": float(item.pack_quantity) if item.pack_quantity is not None else None,
                        "Мин. заказ": float(item.min_order) if item.min_order is not None else None,
                        "Остаток": float(item.stock) if item.stock is not None else None,
                    })

                print(f"[OK] {price_list.name} | товаров: {len(items)}")

            except Exception as e:
                print(f"[ERROR] {price_list.id} | {price_list.name}: {e}")

    return rows


async def main():
    all_rows = []

    for account in ACCOUNTS:
        account_rows = await export_account(account)
        all_rows.extend(account_rows)

    if not all_rows:
        print("[EMPTY] Нет данных для выгрузки")
        return

    export_dir = Path("exports")
    export_dir.mkdir(exist_ok=True)

    now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    file_path = export_dir / f"widman_prices_{now}.xlsx"

    df = pd.DataFrame(all_rows)

    with pd.ExcelWriter(file_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Все товары", index=False)

        for account_name, group in df.groupby("Аккаунт"):
            sheet_name = account_name[:31]
            group.to_excel(writer, sheet_name=sheet_name, index=False)

    print(f"[DONE] Excel готов: {file_path.resolve()}")
    print(f"[INFO] Всего строк: {len(df)}")


if __name__ == "__main__":
    asyncio.run(main())