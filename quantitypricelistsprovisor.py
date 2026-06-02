from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

import requests


PROVISOR_BASE_URL = "https://api.provisor.kz"
LOGIN = "9mkrd1"
PASSWORD = "2mo55j"

TIMEOUT_SECONDS = 60


class ProvisorError(RuntimeError):
    pass


def pretty_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


def response_details(resp: requests.Response) -> str:
    headers = {
        k: v
        for k, v in resp.headers.items()
        if k.lower() not in {"authorization", "set-cookie"}
    }
    try:
        body: Any = resp.json()
    except Exception:
        body = resp.text
    return (
        f"HTTP {resp.status_code}\n"
        f"URL: {resp.url}\n"
        f"Headers: {pretty_json(headers)}\n"
        f"Body: {pretty_json(body)}"
    )


def create_tokens(session: requests.Session, base_url: str, login: str, password: str) -> dict[str, str]:
    url = f"{base_url.rstrip('/')}/Token/CreateAll"
    payload = {"login": login, "password": password}

    resp = session.post(url, json=payload, timeout=TIMEOUT_SECONDS)
    if not resp.ok:
        raise ProvisorError(f"Authentication failed: Token/CreateAll\n{response_details(resp)}")

    try:
        data = resp.json()
    except Exception as exc:
        raise ProvisorError(f"Authentication returned invalid JSON:\n{resp.text}") from exc

    access = data.get("access") or data.get("accessToken") or data.get("token")
    refresh = data.get("refresh") or data.get("refreshToken")

    if not access:
        raise ProvisorError(f"Authentication returned no access token:\n{pretty_json(data)}")

    return {"access": str(access), "refresh": str(refresh or "")}


def get_filials(session: requests.Session, base_url: str, access_token: str) -> list[dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/Distributor/GetFilialsByContext"
    headers = {"Authorization": f"Bearer {access_token}"}

    resp = session.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
    if not resp.ok:
        raise ProvisorError(f"Filial request failed: Distributor/GetFilialsByContext\n{response_details(resp)}")

    try:
        data = resp.json()
    except Exception as exc:
        raise ProvisorError(f"Filial request returned invalid JSON:\n{resp.text}") from exc

    if not isinstance(data, list):
        raise ProvisorError(f"Filial request returned non-list JSON:\n{pretty_json(data)}")

    return data


def first_nonempty(row: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_filial(row: dict[str, Any]) -> dict[str, str]:
    return {
        "filialId": first_nonempty(row, ["id", "filialId", "filialID", "priceListId"]),
        "filialName": first_nonempty(row, ["name", "filialName", "priceListName", "displayName"]),
        "city": first_nonempty(row, ["city", "cityName", "town", "region"]),
        "distributor": first_nonempty(row, ["distributor", "distributorName", "supplier", "companyName"]),
    }


def safe_filename_part(value: str) -> str:
    value = value.strip() or "account"
    value = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE)
    return value.strip("_") or "account"


def print_report(login: str, filials: list[dict[str, str]]) -> None:
    unique_cities = {x["city"] for x in filials if x["city"]}
    unique_distributors = {x["distributor"] for x in filials if x["distributor"]}

    print("=" * 50)
    print(f"Account: {login}")
    print("=" * 18)
    print()
    print(f"Total filials: {len(filials)}")
    print()
    print("## ID      Name                         City                         Distributor")
    print("-" * 90)

    for row in filials:
        print(
            f"{row['filialId']:<8}"
            f"{row['filialName'][:28]:<29}"
            f"{row['city'][:28]:<29}"
            f"{row['distributor'][:28]}"
        )

    print()
    print(f"Unique cities: {len(unique_cities)}")
    print(f"Unique distributors: {len(unique_distributors)}")


def export_csv(login: str, filials: list[dict[str, str]]) -> Path:
    path = Path(f"filials_{safe_filename_part(login)}.csv")
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["filialId", "filialName", "city", "distributor"],
        )
        writer.writeheader()
        writer.writerows(filials)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show available Provisor filials/PLK for one account.")
    parser.add_argument("--base-url", default=PROVISOR_BASE_URL)
    parser.add_argument("--login", default=LOGIN)
    parser.add_argument("--password", default=PASSWORD)
    parser.add_argument("--export-csv", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    login = (args.login or "").strip()
    password = (args.password or "").strip()
    base_url = (args.base_url or "").strip().rstrip("/")

    if not login or not password:
        print("ERROR: LOGIN/PASSWORD are empty. Fill constants or pass --login and --password.", file=sys.stderr)
        return 2

    try:
        with requests.Session() as session:
            tokens = create_tokens(session, base_url, login, password)
            raw_filials = get_filials(session, base_url, tokens["access"])

        filials = [normalize_filial(row) for row in raw_filials if isinstance(row, dict)]
        filials.sort(key=lambda x: (x["filialName"], x["filialId"]))

        print_report(login, filials)

        if args.export_csv:
            path = export_csv(login, filials)
            print()
            print(f"CSV saved: {path}")

        return 0

    except ProvisorError as exc:
        print("ERROR:", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print("REQUEST ERROR:", file=sys.stderr)
        print(repr(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
