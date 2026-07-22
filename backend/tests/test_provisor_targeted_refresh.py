from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import CompetitorPriceList, CompetitorPriceListItem, PriceFormat, PriceSourceAccount
from backend.app.services.price_sources import UnifiedPriceItem, UnifiedPriceList


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


class _FakeProvisorAdapter:
    source = "provisor"

    def __init__(self):
        self.fetched_item_ids: list[str] = []

    async def fetch_price_lists(self, account):
        return [
            UnifiedPriceList(
                source="provisor",
                account_id=str(account.id),
                account_login=account.login,
                price_list_id=str(fid),
                price_list_name=f"Filial {fid}",
                distributor_name=f"Filial {fid}",
                branch_id=str(fid),
                branch_code=str(fid),
                branch_name=f"Filial {fid}",
                competitor_name=f"Filial {fid}",
            )
            for fid in (128, 1397, 8322)
        ]

    async def fetch_price_list_items(self, account, price_list):
        self.fetched_item_ids.append(str(price_list.price_list_id))
        if str(price_list.price_list_id) == "1397":
            return []
        return [
            UnifiedPriceItem(
                source="provisor",
                account_id=str(account.id),
                price_list_id=str(price_list.price_list_id),
                price_list_name=price_list.price_list_name,
                distributor_name=price_list.distributor_name,
                product_name="Item",
                manufacturer="",
                registration_number="",
                distributor_product_name="Item",
                distributor_product_id=f"SKU-{price_list.price_list_id}",
                distributor_price=Decimal("10"),
                stock=Decimal("1"),
                pack_quantity=None,
                expiry_date=None,
                raw={"id": int(price_list.price_list_id), "goodsId": int(price_list.price_list_id) * 10},
            )
        ]


class _ManyPlkProvisorAdapter(_FakeProvisorAdapter):
    def __init__(self, ids: list[int], *, duplicate_name: str | None = None):
        super().__init__()
        self.ids = ids
        self.duplicate_name = duplicate_name

    async def fetch_price_lists(self, account):
        return [
            UnifiedPriceList(
                source="provisor",
                account_id=str(account.id),
                account_login=account.login,
                price_list_id=str(fid),
                price_list_name=self.duplicate_name or f"Filial {fid}",
                distributor_name=self.duplicate_name or f"Filial {fid}",
                branch_id=str(fid),
                branch_code=str(fid),
                branch_name=self.duplicate_name or f"Filial {fid}",
                competitor_name=self.duplicate_name or f"Filial {fid}",
            )
            for fid in self.ids
        ]

    async def fetch_price_list_items(self, account, price_list):
        self.fetched_item_ids.append(str(price_list.price_list_id))
        if str(price_list.price_list_id) == "104":
            raise RuntimeError("one PLK failed")
        return [
            UnifiedPriceItem(
                source="provisor",
                account_id=str(account.id),
                price_list_id=str(price_list.price_list_id),
                price_list_name=price_list.price_list_name,
                distributor_name=price_list.distributor_name,
                product_name=f"Item {price_list.price_list_id}",
                manufacturer="",
                registration_number="",
                distributor_product_name=f"Item {price_list.price_list_id}",
                distributor_product_id=f"SKU-{price_list.price_list_id}",
                distributor_price=Decimal("10"),
                stock=Decimal("1"),
                pack_quantity=None,
                expiry_date=None,
                raw={"id": int(price_list.price_list_id), "goodsId": int(price_list.price_list_id) * 10},
            )
        ]


class _TimeoutProvisorAdapter(_FakeProvisorAdapter):
    async def fetch_price_lists(self, account):
        return [
            UnifiedPriceList(
                source="provisor",
                account_id=str(account.id),
                account_login=account.login,
                price_list_id="128",
                price_list_name="Filial 128",
                distributor_name="Filial 128",
            )
        ]

    async def fetch_price_list_items(self, account, price_list):
        self.fetched_item_ids.append(str(price_list.price_list_id))
        await asyncio.sleep(0.05)
        return []


def _seed(db):
    pf = PriceFormat(code="FMT", name="Format")
    account = PriceSourceAccount(id=3, source_type="provisor", login="Aksai4/83", encrypted_password="x")
    db.add_all([pf, account])
    db.commit()
    return pf, account


def _add_account(db, account_id: int, login: str):
    account = PriceSourceAccount(id=account_id, source_type="provisor", login=login, encrypted_password="x")
    db.add(account)
    db.commit()
    return account


def _fake_credentials(row):
    return SimpleNamespace(id=row.id, source_type=row.source_type, login=row.login, password="", config={})


def test_targeted_provisor_refresh_only_processes_requested_filials(monkeypatch):
    import backend.app.main as main

    db = _session()
    _seed(db)
    adapter = _FakeProvisorAdapter()
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountId": 3, "provisorFilialIds": [128, 8322], "forceRefresh": True},
            db=db,
        )
    )

    assert adapter.fetched_item_ids == ["128", "8322"]
    assert result["progress"]["success_with_items"] == 2
    assert result["progress"]["success_zero_items"] == 0
    saved = db.execute(select(CompetitorPriceList.external_price_list_id)).scalars().all()
    assert sorted(saved) == ["128", "8322"]


def test_all_discovered_provisor_plk_processed_beyond_concurrency(monkeypatch):
    import backend.app.main as main

    db = _session()
    _seed(db)
    adapter = _ManyPlkProvisorAdapter([100, 101, 102, 103, 105])
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountId": 3, "forceRefresh": True, "maxParallelPlk": 2},
            db=db,
        )
    )

    assert adapter.fetched_item_ids == ["100", "101", "102", "103", "105"]
    assert result["progress"]["success_with_items"] == 5
    assert result["inventory"]["unique_plk"] == 5


def test_one_provisor_plk_failure_does_not_cancel_remaining(monkeypatch):
    import backend.app.main as main

    db = _session()
    _seed(db)
    adapter = _ManyPlkProvisorAdapter([100, 104, 105])
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountId": 3, "forceRefresh": True, "maxParallelPlk": 2},
            db=db,
        )
    )

    assert sorted(adapter.fetched_item_ids) == ["100", "104", "105"]
    assert result["progress"]["success_with_items"] == 2
    assert result["progress"]["errors"] == 1
    saved = sorted(db.execute(select(CompetitorPriceList.external_price_list_id)).scalars().all())
    assert saved == ["100", "105"]


def test_duplicate_provisor_plk_external_id_refreshed_once_with_aliases(monkeypatch):
    import backend.app.main as main

    db = _session()
    _seed(db)
    _add_account(db, 4, "Second")
    adapter = _ManyPlkProvisorAdapter([128])
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountIds": [3, 4], "forceRefresh": True, "maxParallelAccounts": 2},
            db=db,
        )
    )

    assert adapter.fetched_item_ids == ["128"]
    assert result["inventory"]["raw_plk_candidates"] == 2
    assert result["inventory"]["unique_plk"] == 1
    assert result["inventory"]["duplicates"] == 1
    rows = db.execute(select(CompetitorPriceList)).scalars().all()
    assert len(rows) == 1
    assert rows[0].source_key == "plk:128"
    assert "aliasesJson" in rows[0].region


def test_provisor_refresh_reuses_legacy_account_scoped_row(monkeypatch):
    import backend.app.main as main

    db = _session()
    pf, _account = _seed(db)
    legacy = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="3:128",
        account_id="3",
        external_price_list_id="128",
        display_name="Legacy",
    )
    db.add(legacy)
    db.commit()
    legacy_id = int(legacy.id)

    adapter = _ManyPlkProvisorAdapter([128])
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountId": 3, "forceRefresh": True},
            db=db,
        )
    )

    rows = db.execute(select(CompetitorPriceList)).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == legacy_id
    assert rows[0].source_key == "plk:128"
    assert result["inventory"]["persisted_snapshots"] == 1


def test_same_name_different_provisor_plk_not_merged(monkeypatch):
    import backend.app.main as main

    db = _session()
    _seed(db)
    adapter = _ManyPlkProvisorAdapter([128, 129], duplicate_name="Amanat")
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountId": 3, "forceRefresh": True},
            db=db,
        )
    )

    assert result["inventory"]["unique_plk"] == 2
    assert result["inventory"]["duplicates"] == 0
    assert sorted(db.execute(select(CompetitorPriceList.source_key)).scalars().all()) == ["plk:128", "plk:129"]


def test_targeted_provisor_refresh_only_processes_requested_account(monkeypatch):
    import backend.app.main as main

    db = _session()
    _seed(db)
    _add_account(db, 4, "Second")
    adapter = _FakeProvisorAdapter()
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountIds": [4], "provisorFilialIds": [128], "forceRefresh": True},
            db=db,
        )
    )

    assert result["accounts_requested"] == [4]
    assert result["accounts_processed"] == [4]
    assert result["accounts_skipped"] == []
    assert db.execute(select(CompetitorPriceList.account_id)).scalars().all() == ["4"]


def test_targeted_provisor_refresh_processes_multiple_requested_accounts(monkeypatch):
    import backend.app.main as main

    db = _session()
    _seed(db)
    _add_account(db, 4, "Second")
    adapter = _FakeProvisorAdapter()
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountIds": [3, 4], "provisorFilialIds": [128], "forceRefresh": True},
            db=db,
        )
    )

    assert result["accounts_processed"] == [3, 4]
    assert db.execute(select(CompetitorPriceList)).scalar_one().source_key == "plk:128"
    assert result["inventory"]["duplicates"] == 1


def test_provisor_refresh_without_account_ids_keeps_refresh_all_behavior(monkeypatch):
    import backend.app.main as main

    db = _session()
    _seed(db)
    _add_account(db, 4, "Second")
    adapter = _FakeProvisorAdapter()
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "provisorFilialIds": [128], "forceRefresh": True},
            db=db,
        )
    )

    assert result["accounts_requested"] == []
    assert result["accounts_processed"] == [3, 4]
    assert db.execute(select(CompetitorPriceList)).scalar_one().source_key == "plk:128"
    assert result["inventory"]["duplicates"] == 1


def test_provisor_unchanged_updates_checked_status_and_preserves_items(monkeypatch):
    import backend.app.main as main

    db = _session()
    pf, _account = _seed(db)
    existing = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="plk:128",
        display_name="Existing",
        account_id="3",
        account_login="Aksai4/83",
        external_price_list_id="128",
        source_updated_at="2026-05-01T10:00:00",
    )
    db.add(existing)
    db.flush()
    db.add(
        CompetitorPriceListItem(
            price_list_id=existing.id,
            name="Old Item",
            distributor_goods_id="OLD",
            distributor_price=10,
        )
    )
    db.commit()

    adapter = _FakeProvisorAdapter()
    async def _unchanged_items(account, price_list):
        adapter.fetched_item_ids.append(str(price_list.price_list_id))
        return [
            UnifiedPriceItem(
                source="provisor",
                account_id=str(account.id),
                price_list_id=str(price_list.price_list_id),
                price_list_name=price_list.price_list_name,
                distributor_name=price_list.distributor_name,
                product_name="New Item",
                manufacturer="",
                registration_number="",
                distributor_product_name="New Item",
                distributor_product_id="NEW",
                distributor_price=Decimal("11"),
                stock=Decimal("2"),
                pack_quantity=None,
                expiry_date=None,
                raw={"id": 128, "goodsId": 1280, "insertedDate": "2026-05-01T10:00:00"},
            )
        ]

    adapter.fetch_price_list_items = _unchanged_items
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountId": 3, "provisorFilialIds": [128], "forceRefresh": True},
            db=db,
        )
    )

    row = db.execute(select(CompetitorPriceList).where(CompetitorPriceList.source_key == "plk:128")).scalar_one()
    items = db.execute(select(CompetitorPriceListItem).where(CompetitorPriceListItem.price_list_id == row.id)).scalars().all()
    assert result["skipped_unchanged"] == 1
    assert row.last_refresh_status == "checked_unchanged"
    assert row.last_checked_at is not None
    assert row.last_success_at is not None
    assert [item.name for item in items] == ["Old Item"]


def test_provisor_timeout_does_not_wipe_existing_items(monkeypatch):
    import backend.app.main as main

    db = _session()
    pf, _account = _seed(db)
    existing = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="plk:128",
        display_name="Existing",
        account_id="3",
        external_price_list_id="128",
    )
    db.add(existing)
    db.flush()
    db.add(
        CompetitorPriceListItem(
            price_list_id=existing.id,
            name="Old Item",
            distributor_goods_id="OLD",
            distributor_price=10,
        )
    )
    db.commit()

    adapter = _TimeoutProvisorAdapter()
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)
    monkeypatch.setattr(main, "PROVISOR_PRICE_TOTAL_TIMEOUT_SECONDS", 0.01)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountId": 3, "provisorFilialIds": [128], "forceRefresh": True},
            db=db,
        )
    )

    assert result["progress"]["timeout"] == 1
    assert result["progress"]["skipped_timeout"] == 1
    assert db.execute(select(CompetitorPriceListItem).where(CompetitorPriceListItem.price_list_id == existing.id)).scalar_one().name == "Old Item"


def test_provisor_zero_response_does_not_wipe_existing_items(monkeypatch):
    import backend.app.main as main

    db = _session()
    pf, _account = _seed(db)
    existing = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="plk:1397",
        display_name="Existing",
        account_id="3",
        external_price_list_id="1397",
    )
    db.add(existing)
    db.flush()
    db.add(
        CompetitorPriceListItem(
            price_list_id=existing.id,
            name="Old Item",
            distributor_goods_id="OLD",
            distributor_price=10,
        )
    )
    db.commit()

    adapter = _FakeProvisorAdapter()
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountId": 3, "provisorFilialIds": [1397], "forceRefresh": True},
            db=db,
        )
    )

    row = db.execute(select(CompetitorPriceList).where(CompetitorPriceList.source_key == "plk:1397")).scalar_one()
    items = db.execute(select(CompetitorPriceListItem).where(CompetitorPriceListItem.price_list_id == row.id)).scalars().all()
    assert result["progress"]["success_zero_items"] == 1
    assert row.last_refresh_status == "success_zero_items"
    assert [item.name for item in items] == ["Old Item"]


def test_provisor_timeout_config_default_is_120(monkeypatch):
    import importlib
    import backend.app.main as main
    import backend.app.services.price_sources as price_sources

    monkeypatch.delenv("PROVISOR_PRICE_TOTAL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("PROVISOR_PRICE_READ_TIMEOUT_SECONDS", raising=False)

    main = importlib.reload(main)
    price_sources = importlib.reload(price_sources)

    assert main.PROVISOR_PRICE_TOTAL_TIMEOUT_SECONDS == 120
    assert price_sources._provisor_item_timeout_seconds() == 120.0

    monkeypatch.setenv("PROVISOR_PRICE_TOTAL_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("PROVISOR_PRICE_READ_TIMEOUT_SECONDS", "30")
    assert price_sources._provisor_item_timeout_seconds() == 120.0
