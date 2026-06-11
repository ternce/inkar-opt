from __future__ import annotations

import asyncio
from decimal import Decimal

from backend.app.services.price_sources import (
    PriceSourceAccountCredentials,
    ProvisorPriceService,
    UnifiedPriceItem,
    UnifiedPriceList,
    _as_decimal,
)


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
