import requests
import json

BASE_URL = "https://ph.center/api/Report/PricesAnalysis"

TOKEN = "Bearer c5741fd869434cfb5a032b44c1cdcd8d"

REGION = 1
DISTRIBUTORS = 1
PRICE_MODE = 0


def get_prices_analysis(region, distributors, price_mode=0):
    params = {
        "region": region,
        "price_mode": price_mode,
        "distributors": distributors
    }

    headers = {
        "Authorization": TOKEN
    }

    response = requests.get(BASE_URL, headers=headers, params=params)

    if response.status_code == 200:
        return response.json()
    else:
        print("Ошибка:", response.status_code, response.text)
        return None


def save_to_txt(data, filename="prices.txt"):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✅ Сохранено в {filename}")


if __name__ == "__main__":
    data = get_prices_analysis(REGION, DISTRIBUTORS)

    if data:
        save_to_txt(data)