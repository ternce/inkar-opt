from __future__ import annotations

import json
from urllib.parse import urlparse
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from ...config import Settings
from ..regions import canonical_supported_city_name
from .types import canonical_branch_id


SAP_LGORT = "1000"


def _validate_endpoint_authorization(settings: Settings) -> None:
    """Fail closed before network I/O when the configured SAP tier is ambiguous."""
    target = settings.sap_balance_target_environment.strip().lower()
    if target not in {"piq", "pip"}:
        raise ValueError("SAP_BALANCE_TARGET_ENVIRONMENT must be either 'piq' or 'pip'")

    parsed = urlparse(settings.sap_balance_base_url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname:
        raise ValueError("SAP_BALANCE_BASE_URL must be an HTTPS URL with a hostname")

    endpoint_label = hostname.split(".")[0].split("-")
    is_piq_host = "piq" in endpoint_label
    is_pip_host = "pip" in endpoint_label
    if target == "piq":
        if not is_piq_host:
            raise ValueError("SAP target environment is PIQ but SAP_BALANCE_BASE_URL is not a PIQ endpoint")
        if not settings.sap_balance_allow_piq_requests:
            raise ValueError("PIQ SAP requests require SAP_BALANCE_ALLOW_PIQ_REQUESTS=true")
    elif is_piq_host or not is_pip_host:
        raise ValueError("SAP target environment is PIP but SAP_BALANCE_BASE_URL is not a PIP endpoint")


SAP_BALANCE_ROW_LIMIT = 100_000


@dataclass(frozen=True)
class SapPlant:
    plant: str
    name: str


SAP_PLANTS: tuple[SapPlant, ...] = (
    SapPlant("1001", "Алматы"),
    SapPlant("1002", "Астана"),
    SapPlant("1003", "Атырау"),
    SapPlant("1004", "Есик"),
    SapPlant("1005", "Караганда"),
    SapPlant("1006", "Костанай"),
    SapPlant("1007", "Семей"),
    SapPlant("1008", "Усть-Каменогорск"),
    SapPlant("1009", "Павлодар"),
    SapPlant("1010", "Актау"),
    SapPlant("1011", "Актобе"),
    SapPlant("1013", "Шымкент"),
)
SAP_PLANT_BY_CODE = {row.plant: row for row in SAP_PLANTS}


@dataclass(frozen=True)
class SapBalanceItem:
    material: str
    plant: str
    lgort: str
    charge: str
    quantity: Decimal
    raw: dict[str, Any]


@dataclass(frozen=True)
class SapBalancePayload:
    plant: str
    effective_date: date
    items: tuple[SapBalanceItem, ...]
    raw_row_count: int


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    folded = {str(key).casefold(): value for key, value in mapping.items()}
    for key in keys:
        if key in mapping:
            return mapping[key]
        if key.casefold() in folded:
            return folded[key.casefold()]
    return None


def _response_body(payload: dict[str, Any]) -> dict[str, Any]:
    body: Any = payload
    for key in ("MT_BalanceResponse", "mt_BalanceResponse", "balanceResponse"):
        nested = body.get(key) if isinstance(body, dict) else None
        if isinstance(nested, dict):
            body = nested
            break
    if not isinstance(body, dict):
        raise ValueError("SAP MT_BalanceResponse must be an object")
    return body


def parse_balance_response(payload: dict[str, Any], *, requested_plant: str, effective_date: date) -> SapBalancePayload:
    if requested_plant not in SAP_PLANT_BY_CODE:
        raise ValueError(f"SAP plant is not approved: {requested_plant}")
    body = _response_body(payload)
    status = str(_first(body, "status", "Status") or "").strip().upper()
    if status != "SUCCESS":
        message = str(_first(body, "message", "errorMessage", "description") or "SAP business error")
        raise ValueError(f"SAP GetBalance rejected response: status={status or 'missing'}; {message}")
    raw_items = _first(body, "items", "item", "balanceItems")
    if isinstance(raw_items, dict):
        raw_items = _first(raw_items, "item", "items")
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise ValueError("SAP GetBalance items must be an array")
    row_count_raw = _first(body, "rowCount", "row_count", "count")
    try:
        row_count = int(row_count_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("SAP GetBalance rowCount is missing or invalid") from exc
    if row_count != len(raw_items):
        raise ValueError(f"SAP GetBalance is incomplete: rowCount={row_count}, items={len(raw_items)}")
    if row_count >= SAP_BALANCE_ROW_LIMIT:
        raise ValueError(f"SAP GetBalance reached the {SAP_BALANCE_ROW_LIMIT}-row safety limit")

    parsed: list[SapBalanceItem] = []
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"SAP item {index} must be an object")
        material_value = _first(raw, "material", "materialId", "matnr")
        if material_value is None:
            raise ValueError(f"SAP item {index} has no material")
        if not isinstance(material_value, str):
            raise ValueError(f"SAP item {index} material must be a string to preserve leading zeros")
        material = str(material_value).strip()
        if not material:
            raise ValueError(f"SAP item {index} has an empty material")
        plant = str(_first(raw, "plant", "werks") or "").strip()
        lgort = str(_first(raw, "lgort", "storageLocation", "warehouse") or "").strip()
        charge = str(_first(raw, "charge", "batch", "charg") or "").strip()
        if plant != requested_plant:
            raise ValueError(f"SAP item {index} plant mismatch: expected {requested_plant}, got {plant or 'missing'}")
        if lgort != SAP_LGORT:
            raise ValueError(f"SAP item {index} LGORT mismatch: expected {SAP_LGORT}, got {lgort or 'missing'}")
        quantity_raw = _first(raw, "endingStockQuantity", "ending_stock_quantity")
        try:
            quantity = Decimal(str(quantity_raw))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"SAP item {index} has invalid endingStockQuantity") from exc
        if not quantity.is_finite():
            raise ValueError(f"SAP item {index} has invalid endingStockQuantity")
        parsed.append(SapBalanceItem(material, plant, lgort, charge, quantity, dict(raw)))
    return SapBalancePayload(requested_plant, effective_date, tuple(parsed), row_count)


def parse_saved_balance_payload(
    payload: dict[str, Any],
    *,
    requested_plant: str | None = None,
    effective_date: date | None = None,
) -> SapBalancePayload:
    """Load the deterministic, canonical payload format produced by diagnostics.

    Raw PIQ responses continue to use :func:`parse_balance_response`; accepting
    this shape is deliberately limited to explicit operator file uploads.
    """

    plant = str(requested_plant or payload.get("plant") or "").strip()
    raw_date = effective_date or date.fromisoformat(str(payload.get("effective_date") or ""))
    if plant not in SAP_PLANT_BY_CODE:
        raise ValueError(f"SAP plant is not approved: {plant}")
    if requested_plant and str(payload.get("plant") or requested_plant).strip() != requested_plant:
        raise ValueError("saved SAP payload plant does not match the selected plant")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("saved SAP payload items must be an array")
    rows: list[SapBalanceItem] = []
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"saved SAP item {index} must be an object")
        material = raw.get("material")
        if not isinstance(material, str):
            raise ValueError(f"saved SAP item {index} material must be a string")
        item_plant = str(raw.get("plant") or "").strip()
        lgort = str(raw.get("lgort") or "").strip()
        if item_plant != plant:
            raise ValueError(f"saved SAP item {index} plant mismatch: expected {plant}, got {item_plant or 'missing'}")
        if lgort != SAP_LGORT:
            raise ValueError(f"saved SAP item {index} LGORT mismatch: expected {SAP_LGORT}, got {lgort or 'missing'}")
        try:
            quantity = Decimal(str(raw.get("quantity")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"saved SAP item {index} has invalid quantity") from exc
        if not quantity.is_finite():
            raise ValueError(f"saved SAP item {index} has invalid quantity")
        evidence = raw.get("raw") if isinstance(raw.get("raw"), dict) else dict(raw)
        rows.append(
            SapBalanceItem(
                material.strip(),
                item_plant,
                lgort,
                str(raw.get("charge") or raw.get("charg") or "").strip(),
                quantity,
                dict(evidence),
            )
        )
    return SapBalancePayload(plant, raw_date, tuple(rows), len(rows))


def configured_branch_map(settings: Settings) -> dict[str, str]:
    try:
        overrides = json.loads(settings.sap_balance_branch_map_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("SAP_BALANCE_BRANCH_MAP_JSON must be a JSON object") from exc
    if not isinstance(overrides, dict):
        raise ValueError("SAP_BALANCE_BRANCH_MAP_JSON must be a JSON object")
    result: dict[str, str] = {}
    for plant in SAP_PLANTS:
        explicit = str(overrides.get(plant.plant) or "").strip()
        # canonical_branch_id is the existing reference-import authority.  It
        # intentionally returns the canonical city token for supported cities
        # that do not have a legacy Provisor numeric region id (Esik, Semey).
        branch_id = explicit or canonical_branch_id(canonical_supported_city_name(plant.name) or plant.name)
        if branch_id:
            result[plant.plant] = branch_id
    return result


def branch_id_for_plant(settings: Settings, plant: str) -> str:
    if plant not in SAP_PLANT_BY_CODE:
        raise ValueError(f"SAP plant is not approved: {plant}")
    branch_id = configured_branch_map(settings).get(plant, "")
    if not branch_id:
        raise ValueError(
            f"SAP plant {plant} ({SAP_PLANT_BY_CODE[plant].name}) has no verified internal reference branch; "
            "configure SAP_BALANCE_BRANCH_MAP_JSON"
        )
    return branch_id


def build_balance_request(settings: Settings, *, plant: str, date_from: date, date_to: date) -> dict[str, Any]:
    if plant not in SAP_PLANT_BY_CODE:
        raise ValueError(f"SAP plant is not approved: {plant}")
    if not settings.sap_balance_company_code:
        raise ValueError("SAP_BALANCE_COMPANY_CODE is not configured")
    return {
        "companyCode": settings.sap_balance_company_code,
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "plants": [plant],
        "lgort": [SAP_LGORT],
    }


async def fetch_balance(
    settings: Settings,
    *,
    plant: str,
    date_from: date,
    date_to: date,
    client: httpx.AsyncClient | None = None,
) -> SapBalancePayload:
    if not settings.sap_balance_enabled:
        raise ValueError("SAP GetBalance integration is disabled")
    _validate_endpoint_authorization(settings)
    if not settings.sap_balance_username or not settings.sap_balance_password:
        raise ValueError("SAP GetBalance credentials are not configured")
    request_json = build_balance_request(settings, plant=plant, date_from=date_from, date_to=date_to)
    owns_client = client is None
    timeout = httpx.Timeout(float(settings.sap_balance_timeout_seconds))
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        try:
            response = await client.post(
                settings.sap_balance_base_url,
                json=request_json,
                auth=(settings.sap_balance_username, settings.sap_balance_password),
                timeout=timeout,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"SAP GetBalance timed out after {settings.sap_balance_timeout_seconds} seconds"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ValueError(f"SAP GetBalance HTTP error: status={exc.response.status_code}") from exc
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("SAP GetBalance returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise ValueError("SAP GetBalance returned non-object JSON")
        return parse_balance_response(body, requested_plant=plant, effective_date=date_to)
    finally:
        if owns_client:
            await client.aclose()
