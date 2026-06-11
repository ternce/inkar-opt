from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from .manufacturers import resolve_manufacturer
from .provisor import get_filials_by_context, get_prices_by_filial_id
from .widman_client import WidmanClient

logger = logging.getLogger(__name__)
PRICE_LIST_FETCH_TIMEOUT_SECONDS = 30


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _provisor_item_timeout_seconds() -> float:
    total_timeout = _env_float("PROVISOR_PRICE_TOTAL_TIMEOUT_SECONDS", 120.0)
    read_timeout = _env_float("PROVISOR_PRICE_READ_TIMEOUT_SECONDS", total_timeout)
    return max(read_timeout, total_timeout)


@dataclass(frozen=True)
class PriceSourceAccountCredentials:
    id: int
    source_type: str
    login: str
    password: str
    config: dict[str, Any]


@dataclass(frozen=True)
class UnifiedPriceList:
    source: str
    account_id: str
    price_list_id: str
    price_list_name: str
    distributor_name: str
    branch_id: str = ""
    branch_code: str = ""
    branch_name: str = "Без филиала"
    competitor_name: str = ""
    account_login: str = ""
    items_hint: int = 0
    enabled: bool | None = None
    source_updated_at: str = ""


@dataclass(frozen=True)
class UnifiedPriceItem:
    source: str
    account_id: str
    price_list_id: str
    price_list_name: str
    distributor_name: str
    product_name: str
    manufacturer: str
    registration_number: str
    distributor_product_name: str
    distributor_product_id: str
    distributor_price: Decimal | None
    stock: Decimal | None
    pack_quantity: Decimal | None
    expiry_date: str | None
    raw: dict[str, Any]


class PriceSourceAdapter(Protocol):
    source: str

    async def test_connection(self, account: PriceSourceAccountCredentials) -> tuple[bool, str]:
        ...

    async def fetch_price_lists(self, account: PriceSourceAccountCredentials) -> list[UnifiedPriceList]:
        ...

    async def fetch_price_list_items(
        self,
        account: PriceSourceAccountCredentials,
        price_list: UnifiedPriceList,
    ) -> list[UnifiedPriceItem]:
        ...


def _as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace(" ", "").replace(",", ".")
    s = re.sub(r"[^0-9.\-]", "", s)
    if not s:
        return None
    try:
        return Decimal(s)
    except Exception:
        return None


class ProvisorPriceService:
    source = "provisor"

    def __init__(self, *, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def _filials(self, account: PriceSourceAccountCredentials, *, timeout_seconds: float = 60.0) -> list[dict[str, Any]]:
        filials = await get_filials_by_context(
            base_url=self.base_url,
            login=account.login,
            password=account.password,
            timeout_seconds=timeout_seconds,
            force_refresh=True,
        )
        configured_ids = set(self._configured_filial_ids(account))
        if configured_ids and bool(account.config.get("limitToConfiguredFilialIds")):
            filials = [row for row in filials if int(row.get("id") or 0) in configured_ids]
        return filials

    def _configured_filial_ids(self, account: PriceSourceAccountCredentials) -> list[int]:
        raw = account.config.get("filialIds") or account.config.get("filial_ids")
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.split(",") if x.strip()]
        if isinstance(raw, list):
            out: list[int] = []
            for item in raw:
                try:
                    out.append(int(item))
                except Exception:
                    continue
            if out:
                return list(dict.fromkeys(out))

        return []

    async def test_connection(self, account: PriceSourceAccountCredentials) -> tuple[bool, str]:
        try:
            filials = await self._filials(account, timeout_seconds=30.0)
            if not filials:
                return False, "Авторизация успешна, но прайсы Провизора не найдены"
            return True, f"Подключено, найдено прайсов: {len(filials)}"
        except Exception as e:
            return False, str(e)

    async def _legacy_test_connection(self, account: PriceSourceAccountCredentials) -> tuple[bool, str]:
        filial_ids = self._configured_filial_ids(account)
        if not filial_ids:
            return False, "Не настроены filialIds"
        try:
            await get_prices_by_filial_id(
                base_url=self.base_url,
                login=account.login,
                password=account.password,
                filial_id=filial_ids[0],
                timeout_seconds=30.0,
                force_refresh=True,
            )
            return True, "Подключено"
        except Exception as e:
            return False, str(e)

    async def fetch_price_lists(self, account: PriceSourceAccountCredentials) -> list[UnifiedPriceList]:
        filials = await self._filials(account, timeout_seconds=60.0)
        out = [
            UnifiedPriceList(
                source=self.source,
                account_id=str(account.id),
                price_list_id=str(row.get("id")),
                price_list_name=str(row.get("name") or f"Provisor {row.get('id')}").strip(),
                distributor_name=str(row.get("name") or f"Provisor {row.get('id')}").strip(),
                branch_id=str(row.get("id") or ""),
                branch_code=str(row.get("id") or ""),
                branch_name=str(row.get("name") or "Без филиала").strip() or "Без филиала",
                competitor_name=str(row.get("name") or f"Provisor {row.get('id')}").strip(),
                account_login=account.login,
            )
            for row in filials
            if row.get("id")
        ]
        logger.info("Provisor fetch_price_lists returned %s items for account_id=%s", len(out), account.id)
        return out

    async def _legacy_fetch_price_lists(self, account: PriceSourceAccountCredentials) -> list[UnifiedPriceList]:
        return [
            UnifiedPriceList(
                source=self.source,
                account_id=str(account.id),
                price_list_id=str(fid),
                price_list_name=f"Provisor {fid}",
                distributor_name=f"Provisor {fid}",
            )
            for fid in self._configured_filial_ids(account)
        ]

    async def fetch_price_list_items(
        self,
        account: PriceSourceAccountCredentials,
        price_list: UnifiedPriceList,
    ) -> list[UnifiedPriceItem]:
        filial_id = int(price_list.price_list_id)
        fetch_started_at = time.perf_counter()
        raw_items = await get_prices_by_filial_id(
            base_url=self.base_url,
            login=account.login,
            password=account.password,
            filial_id=filial_id,
            timeout_seconds=_provisor_item_timeout_seconds(),
            force_refresh=True,
        )
        fetch_elapsed_ms = round((time.perf_counter() - fetch_started_at) * 1000, 2)
        normalize_started_at = time.perf_counter()
        first = next((x for x in raw_items if isinstance(x, dict)), {})
        filial = first.get("filial") if isinstance(first.get("filial"), dict) else {}
        distributor_name = str(filial.get("name") or price_list.distributor_name or f"Provisor {filial_id}").strip()

        out: list[UnifiedPriceItem] = []
        manufacturer_cache: dict[tuple[object, str], str] = {}
        source = self.source
        account_id = str(account.id)
        price_list_id = str(filial_id)
        as_decimal = _as_decimal
        resolve = resolve_manufacturer
        unified_item = UnifiedPriceItem
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            goods = item.get("goods") if isinstance(item.get("goods"), dict) else {}
            price = as_decimal(item.get("goodsPriceWithUserDiscount"))
            if price is None or price <= 0:
                price = as_decimal(item.get("goodsPrice"))
            stock = as_decimal(item.get("stored"))
            box = as_decimal(item.get("box"))
            pack = as_decimal(item.get("pack"))
            if box is not None and box > 0:
                package_count = box
            elif pack is not None and pack > 0:
                package_count = pack
            else:
                package_count = None
            product_name = str(item.get("distributorGoodsName") or goods.get("fullName") or "").strip()
            manufacturer_raw = item.get("distributorProducer") or item.get("manufacturer") or item.get("producer") or goods.get("producer")
            manufacturer_key = (manufacturer_raw, product_name)
            manufacturer = manufacturer_cache.get(manufacturer_key)
            if manufacturer is None:
                manufacturer = resolve(manufacturer_raw, product_name, default="")
                manufacturer_cache[manufacturer_key] = manufacturer

            out.append(
                unified_item(
                    source=source,
                    account_id=account_id,
                    price_list_id=price_list_id,
                    price_list_name=distributor_name,
                    distributor_name=distributor_name,
                    product_name=product_name,
                    manufacturer=manufacturer,
                    registration_number=str(goods.get("regNumber") or "").strip(),
                    distributor_product_name=str(item.get("distributorGoodsName") or "").strip(),
                    distributor_product_id=str(item.get("distributorGoodsId") or "").strip(),
                    distributor_price=price,
                    stock=stock,
                    pack_quantity=package_count,
                    expiry_date=str(item.get("shelfLife") or "").strip() or None,
                    raw=item,
                )
            )
        normalize_elapsed_ms = round((time.perf_counter() - normalize_started_at) * 1000, 2)
        logger.info(
            "[PROVISOR_PLK_NORMALIZATION_TIMING] account_id=%s filial_id=%s rows=%s fetch_elapsed_ms=%s normalization_elapsed_ms=%s manufacturer_cache_size=%s",
            account.id,
            filial_id,
            len(out),
            fetch_elapsed_ms,
            normalize_elapsed_ms,
            len(manufacturer_cache),
        )
        return out


class VidmanPriceService:
    source = "vidman"

    def __init__(
        self,
        *,
        base_url: str,
        login_path: str = "/pages/login",
        price_base_url: str = "https://prv.kz",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.login_path = login_path or "/pages/login"
        self.price_base_url = price_base_url.rstrip("/")

    async def test_connection(self, account: PriceSourceAccountCredentials) -> tuple[bool, str]:
        try:
            async with self._client(account, timeout=30.0) as client:
                await client.login()
            logger.info(
                "Vidman test_connection success: account_id=%s authMode=%s",
                account.id,
                self._auth_mode(account),
            )
            return True, "Авторизация успешна. Для загрузки прайсов нажмите Обновить данные."
        except Exception as e:
            logger.exception(
                "Vidman test_connection failed: account_id=%s authMode=%s",
                account.id,
                self._auth_mode(account),
            )
            msg = str(e)
            if "401" in msg or "403" in msg or "Неверный логин или пароль" in msg:
                return False, "Неверный логин или пароль"
            if "timeout" in msg.lower():
                return False, "Не удалось авторизоваться в Vidman: timeout при входе"
            if "Target page, context or browser has been closed" in msg:
                return False, "Не удалось авторизоваться в Vidman"
            return False, msg

    async def fetch_price_lists(self, account: PriceSourceAccountCredentials) -> list[UnifiedPriceList]:
        configured = account.config.get("priceListIds") or account.config.get("price_list_ids")
        if isinstance(configured, str):
            configured = [x.strip() for x in configured.split(",") if x.strip()]
        configured_set = {str(x).strip() for x in configured if str(x).strip()} if isinstance(configured, list) else set()

        async with self._client(account, timeout=60.0) as client:
            rows = await client.get_price_lists()
        return self._unified_price_lists_from_rows(account=account, rows=rows, configured_set=configured_set)

    async def fetch_price_lists_with_client(
        self,
        account: PriceSourceAccountCredentials,
        client: WidmanClient,
    ) -> list[UnifiedPriceList]:
        configured = account.config.get("priceListIds") or account.config.get("price_list_ids")
        if isinstance(configured, str):
            configured = [x.strip() for x in configured.split(",") if x.strip()]
        configured_set = {str(x).strip() for x in configured if str(x).strip()} if isinstance(configured, list) else set()
        rows = await client.get_price_lists()
        return self._unified_price_lists_from_rows(account=account, rows=rows, configured_set=configured_set)

    def _unified_price_lists_from_rows(
        self,
        *,
        account: PriceSourceAccountCredentials,
        rows,
        configured_set: set[str],
    ) -> list[UnifiedPriceList]:
        if configured_set:
            rows = [row for row in rows if str(row.id) in configured_set]

        out = [
            UnifiedPriceList(
                source=self.source,
                account_id=str(account.id),
                price_list_id=str(row.id),
                price_list_name=row.name or f"Vidman {row.id}",
                distributor_name=row.name or f"Vidman {row.id}",
                branch_id=row.city or "",
                branch_code=row.city or "",
                branch_name=row.city or "Без филиала",
                competitor_name=row.name or f"Vidman {row.id}",
                account_login=account.login,
                enabled=row.enabled,
                source_updated_at=row.updated_at,
            )
            for row in rows
        ]
        logger.info("Vidman fetch_price_lists returned %s items for account_id=%s", len(out), account.id)
        return out

    async def fetch_price_list_items(
        self,
        account: PriceSourceAccountCredentials,
        price_list: UnifiedPriceList,
    ) -> list[UnifiedPriceItem]:
        async with self._client(account, timeout=PRICE_LIST_FETCH_TIMEOUT_SECONDS) as client:
            rows = await self.fetch_price_list_items_with_client(client, price_list)

        return self._unified_items_from_rows(price_list=price_list, rows=rows)

    async def fetch_price_list_items_with_client(
        self,
        client: WidmanClient,
        price_list: UnifiedPriceList,
    ):
        logger.info(
            "[WIDMAN_FETCH] price_list_id=%s updated_at=%s action=fetch_items_reuse_session",
            price_list.price_list_id,
            price_list.source_updated_at,
        )
        rows = await client.get_price_items(price_list.price_list_id)
        logger.info(
            "[WIDMAN_FETCH] price_list_id=%s updated_at=%s items_count=%s",
            price_list.price_list_id,
            price_list.source_updated_at,
            len(rows),
        )
        if not rows:
            raise RuntimeError(
                f"Widman price list {price_list.price_list_id} returned 0 parsed items; keeping existing items unchanged"
            )
        return rows

    def _unified_items_from_rows(
        self,
        *,
        price_list: UnifiedPriceList,
        rows,
    ) -> list[UnifiedPriceItem]:
        return [
            UnifiedPriceItem(
                source=self.source,
                account_id=price_list.account_id,
                price_list_id=price_list.price_list_id,
                price_list_name=price_list.price_list_name,
                distributor_name=price_list.distributor_name,
                product_name=row.name,
                manufacturer=resolve_manufacturer(row.manufacturer, row.name, default=""),
                registration_number="",
                distributor_product_name=row.name,
                distributor_product_id="",
                distributor_price=row.price,
                stock=row.stock,
                pack_quantity=row.pack_quantity or row.min_order,
                expiry_date=row.expiry_date,
                raw={
                    **row.raw,
                    "source": self.source,
                    "priceListId": price_list.price_list_id,
                    "minOrder": str(row.min_order) if row.min_order is not None else "",
                },
            )
            for row in rows
        ]

    def _client(self, account: PriceSourceAccountCredentials, *, timeout: float) -> WidmanClient:
        auth_mode = self._auth_mode(account)
        logger.info(
            "Creating WidmanClient: account_id=%s authMode=%s auth_base_url=%s price_base_url=%s login_path=%s login=%s",
            account.id,
            auth_mode,
            self.base_url,
            self.price_base_url,
            self.login_path,
            account.login,
        )
        return WidmanClient(
            login=account.login,
            password=account.password,
            auth_base_url=self.base_url,
            price_base_url=self.price_base_url,
            login_path=self.login_path,
            timeout=timeout,
            max_retries=int(account.config.get("maxRetries") or 3),
            retry_delay=float(account.config.get("retryDelay") or 0.5),
            auth_mode=auth_mode,
        )

    def _auth_mode(self, account: PriceSourceAccountCredentials) -> str:
        raw = str(account.config.get("authMode") or "").strip().lower()
        if raw in {"auto", "httpx", "playwright"}:
            return raw
        return "playwright"


def account_config(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
