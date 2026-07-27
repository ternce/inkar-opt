from __future__ import annotations

import asyncio
import base64
import json
from decimal import Decimal

import httpx
import pytest

from backend.app.services import provisor as provisor_module
from backend.app.services.price_sources import (
    PriceSourceAccountCredentials,
    ProvisorPriceService,
    UnifiedPriceItem,
    UnifiedPriceList,
    _as_decimal,
)


def _token(label: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 4_102_444_800, "sub": label}).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


class _FakeResponse:
    def __init__(self, status_code: int, data, *, content_available: bool = True):
        self.status_code = status_code
        self._data = data
        self._content_available = content_available
        self.text = json.dumps(data)

    @property
    def content(self):
        if not self._content_available:
            raise RuntimeError("content size unavailable")
        return self.text.encode()

    def json(self):
        return self._data


class _ReusableFakeClient:
    instances: list["_ReusableFakeClient"] = []
    price_statuses: list[int] = []
    content_available = True
    fail_with: Exception | None = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.closed = False
        self.posts: list[dict] = []
        self.gets: list[dict] = []
        self._token_counter = 0
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()

    async def aclose(self):
        self.closed = True

    async def post(self, path, *, json=None, **kwargs):
        self.posts.append({"path": path, "json": json or {}, "kwargs": kwargs})
        self._token_counter += 1
        login = (json or {}).get("login") or f"refresh-{self._token_counter}"
        return _FakeResponse(
            200,
            {
                "accessToken": _token(f"{login}-{self._token_counter}"),
                "refreshToken": f"refresh-{login}-{self._token_counter}",
            },
        )

    async def get(self, path, *, params=None, headers=None, **kwargs):
        self.gets.append({"path": path, "params": params or {}, "headers": headers or {}, "kwargs": kwargs})
        if self.fail_with is not None:
            raise self.fail_with
        if path == "/Distributor/GetFilialsByContext":
            return _FakeResponse(200, [{"id": 158, "name": "Filial 158"}])
        status = self.price_statuses.pop(0) if self.price_statuses else 200
        if status >= 400:
            return _FakeResponse(status, {"error": status})
        return _FakeResponse(
            200,
            _raw_items(),
            content_available=self.content_available,
        )


def _reset_reusable_fake(monkeypatch):
    _ReusableFakeClient.instances = []
    _ReusableFakeClient.price_statuses = []
    _ReusableFakeClient.content_available = True
    _ReusableFakeClient.fail_with = None
    provisor_module._tokens_by_key.clear()
    monkeypatch.setattr(provisor_module.httpx, "AsyncClient", _ReusableFakeClient)


def _raw_items() -> list[dict]:
    return [
        {
            "id": 1,
            "goodsId": 1001,
            "filialId": 158,
            "distributorGoodsId": "DG-1",
            "distributorGoodsName": "Medicine A",
            "distributorProducer": "BAYER FARMA",
            "goodsPriceWithUserDiscount": "10.50",
            "stored": "7",
            "box": "2",
            "shelfLife": "2027-01-31T00:00:00",
            "goods": {"fullName": "Medicine A Full", "regNumber": "REG-1", "producer": "Ignored"},
        },
        {
            "id": 2,
            "goodsId": 1002,
            "filialId": 158,
            "distributorGoodsId": "DG-2",
            "distributorGoodsName": "Medicine B",
            "distributorProducer": "BAYER FARMA",
            "goodsPrice": "11,25",
            "stored": "0",
            "pack": "3",
            "shelfLife": "",
            "goods": {"fullName": "Medicine B Full", "regNumber": "REG-2"},
        },
        {
            "id": 3,
            "goodsId": 1003,
            "filialId": 158,
            "distributorGoodsId": "DG-3",
            "distributorGoodsName": "",
            "manufacturer": "",
            "goodsPriceWithUserDiscount": "12",
            "stored": "1",
            "goods": {"fullName": "Medicine C Full", "regNumber": "REG-3", "producer": "NOVARTIS FARMA"},
        },
    ]


def _baseline_convert(raw_items: list[dict]) -> list[UnifiedPriceItem]:
    from backend.app.services.manufacturers import resolve_manufacturer

    out: list[UnifiedPriceItem] = []
    distributor_name = "Filial 158"
    for item in raw_items:
        goods = item.get("goods") if isinstance(item.get("goods"), dict) else {}
        product_name = str(item.get("distributorGoodsName") or goods.get("fullName") or "").strip()
        price = _as_decimal(item.get("goodsPriceWithUserDiscount"))
        if price is None or price <= 0:
            price = _as_decimal(item.get("goodsPrice"))
        stock = _as_decimal(item.get("stored"))
        box = _as_decimal(item.get("box"))
        pack = _as_decimal(item.get("pack"))
        package_count = box if box is not None and box > 0 else (pack if pack is not None and pack > 0 else None)
        out.append(
            UnifiedPriceItem(
                source="provisor",
                account_id="4",
                price_list_id="158",
                price_list_name=distributor_name,
                distributor_name=distributor_name,
                product_name=product_name,
                manufacturer=resolve_manufacturer(
                    item.get("distributorProducer")
                    or item.get("manufacturer")
                    or item.get("producer")
                    or goods.get("producer"),
                    product_name,
                    default="",
                ),
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
    return out


def test_provisor_normalized_output_matches_uncached_baseline(monkeypatch):
    raw_items = _raw_items()

    async def fake_get_prices_by_filial_id(**kwargs):
        return raw_items

    monkeypatch.setattr("backend.app.services.price_sources.get_prices_by_filial_id", fake_get_prices_by_filial_id)

    service = ProvisorPriceService(base_url="https://example.test")
    account = PriceSourceAccountCredentials(id=4, source_type="provisor", login="Aksai4/83", password="x", config={})
    price_list = UnifiedPriceList(
        source="provisor",
        account_id="4",
        price_list_id="158",
        price_list_name="Filial 158",
        distributor_name="Filial 158",
    )

    actual = asyncio.run(service.fetch_price_list_items(account, price_list))
    expected = _baseline_convert(raw_items)

    assert actual == expected
    assert [item.raw["goodsId"] for item in actual] == [1001, 1002, 1003]
    assert [item.distributor_product_id for item in actual] == ["DG-1", "DG-2", "DG-3"]
    assert actual[0].manufacturer == expected[0].manufacturer
    assert actual[2].product_name == "Medicine C Full"
    assert actual[0].distributor_price == Decimal("10.50")
    assert actual[1].distributor_price == Decimal("11.25")


def test_provisor_normalization_caches_duplicate_manufacturer_inputs(monkeypatch):
    raw_items = [_raw_items()[0], dict(_raw_items()[0], id=4, goodsId=1004)]
    calls: list[tuple[object, object]] = []

    async def fake_get_prices_by_filial_id(**kwargs):
        return raw_items

    def fake_resolve(raw, name, *, default):
        calls.append((raw, name))
        return "MFR"

    monkeypatch.setattr("backend.app.services.price_sources.get_prices_by_filial_id", fake_get_prices_by_filial_id)
    monkeypatch.setattr("backend.app.services.price_sources.resolve_manufacturer", fake_resolve)

    service = ProvisorPriceService(base_url="https://example.test")
    account = PriceSourceAccountCredentials(id=4, source_type="provisor", login="Aksai4/83", password="x", config={})
    price_list = UnifiedPriceList(source="provisor", account_id="4", price_list_id="158", price_list_name="F", distributor_name="F")

    items = asyncio.run(service.fetch_price_list_items(account, price_list))

    assert [item.manufacturer for item in items] == ["MFR", "MFR"]
    assert calls == [("BAYER FARMA", "Medicine A")]


def test_provisor_service_context_reuses_one_client_for_multiple_plk_calls(monkeypatch):
    _reset_reusable_fake(monkeypatch)

    async def run():
        service = ProvisorPriceService(base_url="https://example.test")
        service.configure_http_pool(max_parallel_plk=1)
        account = PriceSourceAccountCredentials(id=4, source_type="provisor", login="login-a", password="x", config={})
        price_lists = [
            UnifiedPriceList(source="provisor", account_id="4", price_list_id="158", price_list_name="F1", distributor_name="F1"),
            UnifiedPriceList(source="provisor", account_id="4", price_list_id="159", price_list_name="F2", distributor_name="F2"),
        ]
        async with service:
            first = await service.fetch_price_list_items(account, price_lists[0])
            second = await service.fetch_price_list_items(account, price_lists[1])
        return first, second

    first, second = asyncio.run(run())

    assert len(_ReusableFakeClient.instances) == 1
    client = _ReusableFakeClient.instances[0]
    assert client.closed is True
    assert [row["params"]["filialId"] for row in client.gets if row["path"] == "/Price/GetByFilialId"] == [158, 159]
    assert first.benchmark["connection_reuse_scope"] == "account_refresh"
    assert second.benchmark["connection_reuse_scope"] == "account_refresh"
    assert first.benchmark["http_attempt_count"] == 1


def test_provisor_account_scoped_clients_do_not_share_authorization_headers(monkeypatch):
    _reset_reusable_fake(monkeypatch)

    async def run_one(account_id: int, login: str):
        service = ProvisorPriceService(base_url="https://example.test")
        account = PriceSourceAccountCredentials(id=account_id, source_type="provisor", login=login, password="x", config={})
        price_list = UnifiedPriceList(
            source="provisor",
            account_id=str(account_id),
            price_list_id="158",
            price_list_name="F",
            distributor_name="F",
        )
        async with service:
            await service.fetch_price_list_items(account, price_list)

    asyncio.run(run_one(1, "login-a"))
    asyncio.run(run_one(2, "login-b"))

    assert len(_ReusableFakeClient.instances) == 2
    first_auth = [
        row["headers"].get("Authorization")
        for row in _ReusableFakeClient.instances[0].gets
        if row["path"] == "/Price/GetByFilialId"
    ]
    second_auth = [
        row["headers"].get("Authorization")
        for row in _ReusableFakeClient.instances[1].gets
        if row["path"] == "/Price/GetByFilialId"
    ]
    assert first_auth and "login-a" not in first_auth[0]
    assert second_auth and "login-b" not in second_auth[0]
    assert first_auth != second_auth


def test_provisor_unauthorized_response_refreshes_auth_and_preserves_metrics(monkeypatch):
    _reset_reusable_fake(monkeypatch)
    _ReusableFakeClient.price_statuses = [401, 200]

    async def run():
        service = ProvisorPriceService(base_url="https://example.test")
        account = PriceSourceAccountCredentials(id=4, source_type="provisor", login="login-a", password="x", config={})
        price_list = UnifiedPriceList(source="provisor", account_id="4", price_list_id="158", price_list_name="F", distributor_name="F")
        async with service:
            return await service.fetch_price_list_items(account, price_list)

    items = asyncio.run(run())

    client = _ReusableFakeClient.instances[0]
    price_gets = [row for row in client.gets if row["path"] == "/Price/GetByFilialId"]
    assert len(price_gets) == 2
    assert len(client.posts) == 2
    assert items.benchmark["http_attempt_count"] == 2
    assert items.benchmark["auth_retry_count"] == 1
    assert items.benchmark["connection_reuse_scope"] == "account_refresh"


def test_provisor_timeout_exception_behavior_stays_timeout(monkeypatch):
    _reset_reusable_fake(monkeypatch)
    _ReusableFakeClient.fail_with = httpx.ReadTimeout("read timed out")

    async def run():
        return await provisor_module.get_prices_by_filial_id(
            base_url="https://example.test",
            login="login-a",
            password="x",
            filial_id=158,
            timeout_seconds=1,
            force_refresh=True,
        )

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run())
    assert _ReusableFakeClient.instances[0].closed is True


def test_provisor_response_size_can_be_unavailable_without_losing_metrics(monkeypatch):
    _reset_reusable_fake(monkeypatch)
    _ReusableFakeClient.content_available = False

    async def run():
        service = ProvisorPriceService(base_url="https://example.test")
        account = PriceSourceAccountCredentials(id=4, source_type="provisor", login="login-a", password="x", config={})
        price_list = UnifiedPriceList(source="provisor", account_id="4", price_list_id="158", price_list_name="F", distributor_name="F")
        async with service:
            return await service.fetch_price_list_items(account, price_list)

    items = asyncio.run(run())

    assert items.benchmark["response_bytes"] is None
    assert items.benchmark["http_attempt_count"] == 1
    assert items.benchmark["json_decode_sec"] >= 0


def test_provisor_context_closes_client_when_item_fetch_is_cancelled(monkeypatch):
    _reset_reusable_fake(monkeypatch)

    original_get = _ReusableFakeClient.get
    started = asyncio.Event()

    async def slow_get(self, path, *, params=None, headers=None, **kwargs):
        if path == "/Price/GetByFilialId":
            started.set()
            await asyncio.sleep(60)
        return await original_get(self, path, params=params, headers=headers, **kwargs)

    monkeypatch.setattr(_ReusableFakeClient, "get", slow_get)

    async def run():
        service = ProvisorPriceService(base_url="https://example.test")
        account = PriceSourceAccountCredentials(id=4, source_type="provisor", login="login-a", password="x", config={})
        price_list = UnifiedPriceList(source="provisor", account_id="4", price_list_id="158", price_list_name="F", distributor_name="F")
        async with service:
            task = asyncio.create_task(service.fetch_price_list_items(account, price_list))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())

    assert _ReusableFakeClient.instances[0].closed is True
