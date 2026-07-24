from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import PriceSourceAccount
from app.services.price_source_accounts import credentials_from_row
from app.services.price_sources import PriceSourceAccountCredentials
from app.services.provisor import get_filials_by_context


SOURCE_TYPE = "provisor"


@dataclass
class AccountVisibility:
    account_id: str
    account_login: str


@dataclass
class PlkDiscoveryRow:
    external_price_list_id: str
    source_key: str
    plk_name: str
    competitor_name: str
    filial_name: str
    city: str
    region: str
    address: str
    accounts: list[AccountVisibility] = field(default_factory=list)
    region_match_method: str = ""
    region_match_confidence: str = ""
    raw_region_candidates: list[str] = field(default_factory=list)


def _text(value: object) -> str:
    return str(value or "").strip()


def _first_text(row: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, dict):
            nested = _first_text(value, ["name", "title", "value", "caption"])
            if nested:
                return nested
            continue
        if isinstance(value, list):
            joined = " ".join(_text(item) for item in value if _text(item))
            if joined:
                return joined
            continue
        text = _text(value)
        if text:
            return text
    return ""


def _nested(row: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    for key in keys:
        value = row.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _normalize(value: object) -> str:
    text = _text(value).casefold().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _contains_region(haystack: object, region: str) -> bool:
    needle = _normalize(region)
    if not needle:
        return False
    normalized = _normalize(haystack)
    return bool(normalized and needle in normalized.split()) or needle in normalized


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = _text(item)
        key = _normalize(text)
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _external_id(row: dict[str, Any]) -> str:
    return _first_text(
        row,
        [
            "external_price_list_id",
            "externalPriceListId",
            "priceListId",
            "price_list_id",
            "filialId",
            "filial_id",
            "id",
        ],
    )


def _normalize_plk(row: dict[str, Any], account: PriceSourceAccountCredentials) -> PlkDiscoveryRow | None:
    external_id = _external_id(row)
    if not external_id:
        return None

    city_obj = _nested(row, ["city", "City"])
    region_obj = _nested(row, ["region", "Region", "area", "Area"])
    filial_obj = _nested(row, ["filial", "Filial", "branch", "Branch"])
    distributor_obj = _nested(row, ["distributor", "Distributor", "supplier", "Supplier"])

    city = _first_text(row, ["cityName", "city_name", "city", "City"]) or _first_text(city_obj, ["name", "title"])
    region = (
        _first_text(row, ["regionName", "region_name", "region", "Region", "areaName", "area_name"])
        or _first_text(region_obj, ["name", "title"])
    )
    address = _first_text(
        row,
        ["address", "Address", "legalAddress", "legal_address", "filialAddress", "filial_address"],
    ) or _first_text(filial_obj, ["address", "legalAddress", "name"])
    filial_name = (
        _first_text(row, ["filialName", "filial_name", "branchName", "branch_name"])
        or _first_text(filial_obj, ["name", "title"])
        or _first_text(row, ["name", "title"])
    )
    plk_name = _first_text(row, ["priceListName", "price_list_name", "name", "title"]) or filial_name or f"Provisor {external_id}"
    competitor = (
        _first_text(row, ["competitorName", "competitor_name", "supplierName", "supplier_name", "distributorName", "distributor_name"])
        or _first_text(distributor_obj, ["name", "title"])
        or plk_name
    )

    return PlkDiscoveryRow(
        external_price_list_id=external_id,
        source_key=f"plk:{external_id}",
        plk_name=plk_name,
        competitor_name=competitor,
        filial_name=filial_name,
        city=city,
        region=region,
        address=address,
        accounts=[AccountVisibility(account_id=str(account.id), account_login=account.login)],
        raw_region_candidates=_unique([city, region, filial_name, address, plk_name, competitor]),
    )


def _merge_plk(existing: PlkDiscoveryRow, incoming: PlkDiscoveryRow) -> None:
    account_keys = {(item.account_id, item.account_login) for item in existing.accounts}
    for account in incoming.accounts:
        key = (account.account_id, account.account_login)
        if key not in account_keys:
            existing.accounts.append(account)
            account_keys.add(key)

    for attr in ("plk_name", "competitor_name", "filial_name", "city", "region", "address"):
        if not getattr(existing, attr) and getattr(incoming, attr):
            setattr(existing, attr, getattr(incoming, attr))
    existing.raw_region_candidates = _unique(existing.raw_region_candidates + incoming.raw_region_candidates)


def _classify_region(row: PlkDiscoveryRow, requested_region: str | None) -> str:
    if not requested_region:
        row.region_match_method = "not_requested"
        row.region_match_confidence = "not_applicable"
        return "matched"

    structured = [
        ("structured_city", row.city, "high"),
        ("structured_region", row.region, "high"),
    ]
    structured_values = [value for _method, value, _confidence in structured if _text(value)]
    for method, value, confidence in structured:
        if _contains_region(value, requested_region):
            row.region_match_method = method
            row.region_match_confidence = confidence
            return "matched"

    fallback = [
        ("filial_metadata", row.filial_name, "medium"),
        ("normalized_address_name_fallback", " ".join([row.address, row.plk_name, row.competitor_name]), "low"),
    ]
    for method, value, confidence in fallback:
        if _contains_region(value, requested_region):
            row.region_match_method = method
            row.region_match_confidence = "medium" if structured_values else confidence
            return "ambiguous" if structured_values else "matched"

    if structured_values:
        row.region_match_method = "structured_region_mismatch"
        row.region_match_confidence = "high"
        return "excluded_region_mismatch"

    row.region_match_method = "missing_region_metadata"
    row.region_match_confidence = "unknown"
    return "unknown_region"


def _row_to_dict(row: PlkDiscoveryRow) -> dict[str, Any]:
    return {
        "external_price_list_id": row.external_price_list_id,
        "source_key": row.source_key,
        "plk_name": row.plk_name,
        "competitor_name": row.competitor_name,
        "supplier_name": row.competitor_name,
        "filial_name": row.filial_name,
        "city": row.city,
        "region": row.region,
        "address": row.address,
        "accounts": [
            {"account_id": account.account_id, "account_login": account.account_login}
            for account in sorted(row.accounts, key=lambda item: (item.account_login, item.account_id))
        ],
        "region_match_method": row.region_match_method,
        "region_match_confidence": row.region_match_confidence,
    }


def _csv_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section in ("matched", "ambiguous", "unknown_region"):
        for item in payload[section]:
            rows.append(
                {
                    "section": section,
                    **{key: value for key, value in item.items() if key != "accounts"},
                    "accounts": "; ".join(
                        f"{account.get('account_id')}:{account.get('account_login')}" for account in item.get("accounts", [])
                    ),
                }
            )
    for item in payload["duplicates"]:
        rows.append(
            {
                "section": "duplicates",
                "external_price_list_id": item.get("external_price_list_id", ""),
                "source_key": item.get("source_key", ""),
                "plk_name": item.get("plk_name", ""),
                "competitor_name": item.get("competitor_name", ""),
                "supplier_name": item.get("supplier_name", ""),
                "filial_name": item.get("filial_name", ""),
                "city": item.get("city", ""),
                "region": item.get("region", ""),
                "address": item.get("address", ""),
                "accounts": "; ".join(
                    f"{account.get('account_id')}:{account.get('account_login')}" for account in item.get("accounts", [])
                ),
                "region_match_method": item.get("region_match_method", ""),
                "region_match_confidence": item.get("region_match_confidence", ""),
                "duplicate_visibility_count": item.get("duplicate_visibility_count", 0),
            }
        )
    return rows


def _load_accounts() -> list[PriceSourceAccountCredentials]:
    settings = get_settings()
    db = SessionLocal()
    try:
        rows = (
            db.execute(
                select(PriceSourceAccount)
                .where(PriceSourceAccount.source_type == SOURCE_TYPE)
                .where(PriceSourceAccount.is_active.is_(True))
                .order_by(PriceSourceAccount.id.asc())
            )
            .scalars()
            .all()
        )
        accounts = [credentials_from_row(row) for row in rows]
    finally:
        db.close()

    if accounts:
        return accounts
    if settings.provisor_login and settings.provisor_password:
        return [
            PriceSourceAccountCredentials(
                id=0,
                source_type=SOURCE_TYPE,
                login=settings.provisor_login,
                password=settings.provisor_password,
                config={},
            )
        ]
    return []


async def discover(*, region: str | None = None, timeout_seconds: float = 60.0) -> dict[str, Any]:
    settings = get_settings()
    accounts = _load_accounts()
    unique: dict[str, PlkDiscoveryRow] = {}
    account_errors: list[dict[str, str]] = []
    raw_candidates = 0

    for account in accounts:
        try:
            rows = await get_filials_by_context(
                base_url=settings.provisor_base_url,
                login=account.login,
                password=account.password,
                timeout_seconds=timeout_seconds,
                force_refresh=True,
            )
        except Exception as exc:
            account_errors.append({"account_id": str(account.id), "account_login": account.login, "error": str(exc)})
            continue
        raw_candidates += len(rows)
        for raw in rows:
            plk = _normalize_plk(raw, account)
            if plk is None:
                continue
            existing = unique.get(plk.source_key)
            if existing is None:
                unique[plk.source_key] = plk
            else:
                _merge_plk(existing, plk)

    payload: dict[str, Any] = {
        "region_filter": region or "",
        "raw_candidates": raw_candidates,
        "unique_plks": len(unique),
        "accounts_checked": len(accounts),
        "account_errors": account_errors,
        "matched": [],
        "ambiguous": [],
        "unknown_region": [],
        "duplicates": [],
        "excluded_region_mismatch": 0,
    }

    for row in sorted(unique.values(), key=lambda item: (item.competitor_name, item.plk_name, item.source_key)):
        section = _classify_region(row, region)
        if section == "excluded_region_mismatch":
            payload["excluded_region_mismatch"] += 1
            continue
        item = _row_to_dict(row)
        payload[section].append(item)
        if len(row.accounts) > 1:
            payload["duplicates"].append({**item, "duplicate_visibility_count": len(row.accounts)})

    return payload


def _write_json(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _write_csv(payload: dict[str, Any]) -> None:
    rows = _csv_rows(payload)
    fieldnames = [
        "section",
        "external_price_list_id",
        "source_key",
        "plk_name",
        "competitor_name",
        "supplier_name",
        "filial_name",
        "city",
        "region",
        "address",
        "accounts",
        "region_match_method",
        "region_match_confidence",
        "duplicate_visibility_count",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Provisor PLK discovery.")
    parser.add_argument("--region", default="", help="Optional dynamic region filter.")
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)

    payload = asyncio.run(discover(region=args.region.strip() or None, timeout_seconds=args.timeout_seconds))
    if args.format == "csv":
        _write_csv(payload)
    else:
        _write_json(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
