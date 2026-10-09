from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import get_settings
from .provisor import ProvisorAuthError, get_access_token, invalidate_access_token, provisor_http_limits

logger = logging.getLogger(__name__)

GOODS_SEARCH_PATH = "/Goods/GetAllSearchPagingAsync"


class ProvisorGoodsError(RuntimeError):
    """A goods-catalog failure for which local candidate fallback is safe."""

    fallback_allowed = True


class ProvisorGoodsRequestError(ProvisorGoodsError):
    """A non-transient API rejection which must not trigger a broad local scan."""

    fallback_allowed = False


@dataclass(frozen=True)
class ProvisorGoodsCandidate:
    goods_id: int
    name: str
    brand: str
    dose: str
    dosage_form: str
    package_count: int | None
    producer: str
    corporation: str
    inn: str
    atc: str
    full_name: str
    barcodes: tuple[str, ...]


def _name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    return str(value or "").strip()


def _package_count(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _barcodes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value:
        barcode = _name(item) if isinstance(item, dict) else str(item or "").strip()
        if barcode and barcode not in result:
            result.append(barcode)
    return tuple(result)


def parse_goods_response(data: Any, *, limit: int) -> list[ProvisorGoodsCandidate]:
    if not isinstance(data, list):
        raise ProvisorGoodsError("Provisor goods search returned an unexpected response")
    result: list[ProvisorGoodsCandidate] = []
    seen: set[int] = set()
    for raw in data:
        if not isinstance(raw, dict):
            continue
        try:
            goods_id = int(raw.get("id"))
        except (TypeError, ValueError):
            continue
        if goods_id <= 0 or goods_id in seen:
            continue
        seen.add(goods_id)
        result.append(
            ProvisorGoodsCandidate(
                goods_id=goods_id,
                name=_name(raw.get("name")),
                brand=_name(raw.get("brand")),
                dose=_name(raw.get("dose")),
                dosage_form=_name(raw.get("dosageForm")),
                package_count=_package_count(raw.get("number")),
                producer=_name(raw.get("producer")),
                corporation=_name(raw.get("corporation")),
                inn=_name(raw.get("inn")),
                atc=_name(raw.get("atc")),
                full_name=str(raw.get("fullName") or "").strip(),
                barcodes=_barcodes(raw.get("barcodes")),
            )
        )
        if len(result) >= limit:
            break
    return result


class ProvisorGoodsClient:
    def __init__(
        self,
        *,
        base_url: str,
        login: str | None,
        password: str | None,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 12.0,
        total_timeout_seconds: float = 15.0,
        max_results: int = 100,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.login = login
        self.password = password
        self.max_results = max(1, min(int(max_results), 100))
        self.total_timeout_seconds = max(0.1, float(total_timeout_seconds))
        self.timeout = httpx.Timeout(
            connect=max(0.1, float(connect_timeout_seconds)),
            read=max(0.1, float(read_timeout_seconds)),
            write=max(0.1, float(connect_timeout_seconds)),
            pool=max(0.1, float(connect_timeout_seconds)),
        )

    async def search_goods(
        self,
        *,
        full_name: str = "",
        goods_id: int | None = None,
        limit: int | None = None,
    ) -> list[ProvisorGoodsCandidate]:
        take = max(1, min(int(limit or self.max_results), self.max_results, 100))
        query = str(full_name or "").strip()
        if goods_id is None and not query:
            return []
        body_search = {
            "GoodsId": int(goods_id) if goods_id is not None else None,
            "FullName": query,
            "Brand": "",
            "INN": "",
            "ATC": "",
            "Producer": "",
            "BeInThePrices": "false",
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                limits=provisor_http_limits(1),
            ) as client:
                token = await get_access_token(
                    base_url=self.base_url,
                    login=self.login,
                    password=self.password,
                    timeout_seconds=self.timeout.read,
                    client=client,
                )

                async def call(access_token: str) -> httpx.Response:
                    return await client.post(
                        GOODS_SEARCH_PATH,
                        json={"Pagination": {"Take": str(take), "Skip": "0"}, "SearchProperty": body_search},
                        headers={"Authorization": f"Bearer {access_token}"},
                    )

                response = await call(token)
                if response.status_code in {401, 403}:
                    await invalidate_access_token(base_url=self.base_url, login=self.login)
                    token = await get_access_token(
                        base_url=self.base_url,
                        login=self.login,
                        password=self.password,
                        timeout_seconds=self.timeout.read,
                        client=client,
                    )
                    response = await call(token)
                    if response.status_code in {401, 403}:
                        raise ProvisorGoodsError("Provisor goods authentication failed after retry")
                if response.status_code >= 500:
                    raise ProvisorGoodsError(f"Provisor goods search unavailable: HTTP {response.status_code}")
                if response.status_code >= 400:
                    raise ProvisorGoodsRequestError(f"Provisor goods search rejected: HTTP {response.status_code}")
                try:
                    data = response.json()
                except Exception as exc:
                    raise ProvisorGoodsError("Provisor goods search returned invalid JSON") from exc
                return parse_goods_response(data, limit=take)
        except ProvisorGoodsError:
            raise
        except ProvisorAuthError as exc:
            raise ProvisorGoodsError("Provisor goods authentication failed") from exc
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            raise ProvisorGoodsError("Provisor goods search timed out") from exc
        except httpx.HTTPError as exc:
            raise ProvisorGoodsError("Provisor goods search network failure") from exc


def configured_provisor_goods_client() -> ProvisorGoodsClient:
    settings = get_settings()
    return ProvisorGoodsClient(
        base_url=settings.provisor_base_url,
        login=settings.provisor_login,
        password=settings.provisor_password,
        connect_timeout_seconds=settings.provisor_goods_connect_timeout_seconds,
        read_timeout_seconds=settings.provisor_goods_read_timeout_seconds,
        total_timeout_seconds=settings.provisor_goods_total_timeout_seconds,
        max_results=settings.provisor_goods_search_max_results,
    )
