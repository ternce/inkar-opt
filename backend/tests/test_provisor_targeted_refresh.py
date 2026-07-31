from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import (
    CompetitorPriceList,
    CompetitorPriceListItem,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    PriceSourceAccount,
)
from backend.app.services.provisor import ProvisorAuthError
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
        self.fetched_account_item_ids: list[str] = []

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
        self.fetched_account_item_ids.append(f"{account.id}:{price_list.price_list_id}")
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
        self.fetched_account_item_ids.append(f"{account.id}:{price_list.price_list_id}")
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


class _AccountScopedPlkAdapter(_FakeProvisorAdapter):
    def __init__(self, ids_by_account: dict[int, list[int]]):
        super().__init__()
        self.ids_by_account = ids_by_account

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
            for fid in self.ids_by_account.get(int(account.id), [])
        ]

    async def fetch_price_list_items(self, account, price_list):
        self.fetched_item_ids.append(str(price_list.price_list_id))
        self.fetched_account_item_ids.append(f"{account.id}:{price_list.price_list_id}")
        return [
            UnifiedPriceItem(
                source="provisor",
                account_id=str(account.id),
                price_list_id=str(price_list.price_list_id),
                price_list_name=price_list.price_list_name,
                distributor_name=price_list.distributor_name,
                product_name=f"Item account {account.id}",
                manufacturer="",
                registration_number="",
                distributor_product_name=f"Item account {account.id}",
                distributor_product_id=f"SKU-{account.id}-{price_list.price_list_id}",
                distributor_price=Decimal(str(10 + int(account.id))),
                stock=Decimal("1"),
                pack_quantity=None,
                expiry_date=None,
                raw={"id": int(f"{account.id}{price_list.price_list_id}"), "goodsId": int(price_list.price_list_id) * 10 + int(account.id)},
            )
        ]

    async def test_connection(self, account):
        return True, "ok"


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
        self.fetched_account_item_ids.append(f"{account.id}:{price_list.price_list_id}")
        await asyncio.sleep(0.05)
        return []


class _AuthErrorProvisorAdapter(_ManyPlkProvisorAdapter):
    async def fetch_price_list_items(self, account, price_list):
        self.fetched_item_ids.append(str(price_list.price_list_id))
        self.fetched_account_item_ids.append(f"{account.id}:{price_list.price_list_id}")
        raise ProvisorAuthError("Price/GetByFilialId failed: HTTP 401: unauthorized")


class _ContextManagedProvisorAdapter(_FakeProvisorAdapter):
    def __init__(self):
        super().__init__()
        self.entered = False
        self.closed = False
        self.configured_parallel: int | None = None

    def configure_http_pool(self, *, max_parallel_plk: int):
        self.configured_parallel = max_parallel_plk

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()

    async def aclose(self):
        self.closed = True


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


def _benchmark_payloads(caplog):
    out = []
    for record in caplog.records:
        message = record.getMessage()
        if "[PROVISOR_PLK_BENCHMARK]" not in message:
            continue
        out.append(json.loads(message.split("[PROVISOR_PLK_BENCHMARK]", 1)[1].strip()))
    return out


def _memory_payloads(caplog):
    out = []
    messages = [record.getMessage() for record in caplog.records]
    messages.extend(caplog.text.splitlines())
    seen: set[str] = set()
    for message in messages:
        if "[PROVISOR_MEMORY]" not in message and "[PROVISOR_PLK_MEMORY]" not in message:
            continue
        marker = "[PROVISOR_MEMORY]" if "[PROVISOR_MEMORY]" in message else "[PROVISOR_PLK_MEMORY]"
        raw = message.split(marker, 1)[1].strip()
        brace = raw.find("{")
        if brace >= 0:
            raw = raw[brace:]
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            key = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                out.append(parsed)
    return out


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


def test_provisor_refresh_enters_and_closes_account_scoped_adapter(monkeypatch):
    import backend.app.main as main

    db = _session()
    _seed(db)
    adapter = _ContextManagedProvisorAdapter()
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountId": 3, "forceRefresh": True, "maxParallelPlk": 2},
            db=db,
        )
    )

    assert result["progress"]["success_with_items"] == 2
    assert adapter.entered is True
    assert adapter.closed is True
    assert adapter.configured_parallel == 2


def test_provisor_benchmark_logs_success_without_changing_refresh_behavior(monkeypatch, caplog):
    import backend.app.main as main

    db = _session()
    _seed(db)
    adapter = _ManyPlkProvisorAdapter([100])
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    with caplog.at_level(logging.INFO):
        result = asyncio.run(
            main._run_refresh_price_lists_logic(
                format_code="FMT",
                payload={"source": "provisor", "accountId": 3, "forceRefresh": True, "maxParallelPlk": 2},
                db=db,
            )
        )

    payloads = _benchmark_payloads(caplog)
    assert adapter.fetched_item_ids == ["100"]
    assert result["progress"]["success_with_items"] == 1
    assert result["provisorBenchmark"]["refreshed"] == 1
    assert len(payloads) == 1
    assert payloads[0]["account_id"] == 3
    assert payloads[0]["filial_id"] == "100"
    assert payloads[0]["outcome"] == "refreshed"
    assert payloads[0]["response_bytes"] is None
    assert "Aksai4/83" not in json.dumps(payloads, ensure_ascii=False)
    assert "password" not in json.dumps(payloads, ensure_ascii=False).lower()
    assert "token" not in json.dumps(payloads, ensure_ascii=False).lower()


def test_provisor_memory_logs_are_scalar_and_do_not_expose_credentials(monkeypatch, caplog):
    import backend.app.main as main

    db = _session()
    _seed(db)
    adapter = _ManyPlkProvisorAdapter([100])
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)
    monkeypatch.setattr(main, "process_memory_snapshot", lambda: {"rss_mb": 123.45})

    with caplog.at_level(logging.INFO):
        result = asyncio.run(
            main._run_refresh_price_lists_logic(
                format_code="FMT",
                payload={"source": "provisor", "accountId": 3, "forceRefresh": True},
                db=db,
            )
        )

    direct_memory_payload = main._record_provisor_plk_memory(
        {"account_id": 3, "filial_id": "100", "price_list_id": "100"},
        stage="test_stage",
        db=db,
        rows=1,
    )
    serialized_direct_memory = json.dumps(direct_memory_payload, ensure_ascii=False).lower()

    assert result["progress"]["success_with_items"] == 1
    assert direct_memory_payload["stage"] == "test_stage"
    assert direct_memory_payload["rss_mb"] == 123.45
    assert direct_memory_payload["identity_map_size"] is not None
    assert "password" not in serialized_direct_memory
    assert "token" not in serialized_direct_memory
    assert result["provisorBenchmark"]["peak_rss_mb"] == 123.45
    assert result["provisorBenchmark"]["last_cleanup_rss_mb"] == 123.45


def test_provisor_benchmark_skipped_plk_has_skip_reason(monkeypatch, caplog):
    import backend.app.main as main

    db = _session()
    _seed(db)
    adapter = _ManyPlkProvisorAdapter([1052])
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    with caplog.at_level(logging.INFO):
        result = asyncio.run(
            main._run_refresh_price_lists_logic(
                format_code="FMT",
                payload={"source": "provisor", "accountId": 3, "forceRefresh": True},
                db=db,
            )
        )

    payloads = _benchmark_payloads(caplog)
    assert adapter.fetched_item_ids == []
    assert result["progress"]["skipped_heavy"] == 1
    assert payloads
    assert payloads[0]["outcome"] == "skipped"
    assert payloads[0]["skip_reason"] == "excluded_emit_or_heavy_filial"
    account = db.get(PriceSourceAccount, 3)
    assert account is not None
    assert account.status == "connected"


def test_provisor_successful_auth_with_all_skipped_is_not_auth_error(monkeypatch):
    import backend.app.main as main

    db = _session()
    _seed(db)
    adapter = _ManyPlkProvisorAdapter([1052])
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountId": 3, "forceRefresh": True},
            db=db,
        )
    )

    assert result["progress"]["skipped_heavy"] == 1
    account = db.get(PriceSourceAccount, 3)
    assert account is not None
    assert account.status == "connected"


def test_provisor_actual_401_sets_auth_error(monkeypatch):
    import backend.app.main as main

    db = _session()
    _seed(db)
    adapter = _AuthErrorProvisorAdapter([128])
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountId": 3, "forceRefresh": True},
            db=db,
        )
    )

    assert adapter.fetched_account_item_ids == ["3:128"]
    assert result["progress"]["skipped_auth_error_count"] == 1
    account = db.get(PriceSourceAccount, 3)
    assert account is not None
    assert account.status == "auth_error"


def test_provisor_benchmark_failed_plk_has_failed_outcome(monkeypatch, caplog):
    import backend.app.main as main

    db = _session()
    _seed(db)
    adapter = _ManyPlkProvisorAdapter([104])
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    with caplog.at_level(logging.INFO):
        result = asyncio.run(
            main._run_refresh_price_lists_logic(
                format_code="FMT",
                payload={"source": "provisor", "accountId": 3, "forceRefresh": True},
                db=db,
            )
        )

    payloads = _benchmark_payloads(caplog)
    assert result["progress"]["errors"] == 1
    assert payloads
    assert payloads[0]["outcome"] == "failed"
    assert payloads[0]["skip_reason"] == "RuntimeError"


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
    assert result["provisorAudit"]["success_nonzero"] == 2
    assert result["provisorAudit"]["failed"] == 1
    assert result["provisorAudit"]["invariant_ok"] is True
    saved = sorted(db.execute(select(CompetitorPriceList.external_price_list_id)).scalars().all())
    assert saved == ["100", "105"]


def test_same_provisor_plk_external_id_is_account_scoped(monkeypatch):
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

    assert sorted(adapter.fetched_account_item_ids) == ["3:128", "4:128"]
    assert result["inventory"]["raw_plk_candidates"] == 2
    assert result["inventory"]["unique_plk"] == 2
    assert result["inventory"]["duplicates"] == 0
    assert result["provisorAudit"]["skipped"] == 0
    rows = db.execute(select(CompetitorPriceList).order_by(CompetitorPriceList.account_id.asc())).scalars().all()
    assert len(rows) == 2
    assert [(row.account_id, row.external_price_list_id, row.source_key) for row in rows] == [
        ("3", "128", "account:3:plk:128"),
        ("4", "128", "account:4:plk:128"),
    ]


def test_duplicate_provisor_plk_external_id_within_same_account_is_deduplicated(monkeypatch):
    import backend.app.main as main

    db = _session()
    _seed(db)
    adapter = _ManyPlkProvisorAdapter([128, 128])
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountId": 3, "forceRefresh": True},
            db=db,
        )
    )

    assert adapter.fetched_account_item_ids == ["3:128"]
    assert result["inventory"]["raw_plk_candidates"] == 2
    assert result["inventory"]["unique_plk"] == 1
    assert result["inventory"]["duplicates"] == 1
    assert result["provisorAudit"]["failure_skip_reasons"]["duplicate_external_plk_id"] == 1
    row = db.execute(select(CompetitorPriceList)).scalar_one()
    assert row.source_key == "account:3:plk:128"


def test_zhasulan_farm_regression_all_discovered_plks_are_attempted(monkeypatch):
    import backend.app.main as main

    db = _session()
    pf = PriceFormat(code="FMT", name="Format")
    account = PriceSourceAccount(id=1, source_type="provisor", login="Жасулан-Фарм", encrypted_password="x")
    db.add_all([pf, account])
    db.commit()
    adapter = _ManyPlkProvisorAdapter(list(range(1000, 1044)))
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountId": 1, "forceRefresh": True, "maxParallelPlk": 4},
            db=db,
        )
    )

    assert result["inventory"]["raw_plk_candidates"] == 44
    assert result["inventory"]["unique_plk"] == 44
    assert result["inventory"]["duplicates"] == 0
    assert len(adapter.fetched_account_item_ids) == 44
    assert all(item.startswith("1:") for item in adapter.fetched_account_item_ids)


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
    assert rows[0].source_key == "account:3:plk:128"
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
    assert sorted(db.execute(select(CompetitorPriceList.source_key)).scalars().all()) == [
        "account:3:plk:128",
        "account:3:plk:129",
    ]


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
    rows = db.execute(select(CompetitorPriceList).order_by(CompetitorPriceList.account_id.asc())).scalars().all()
    assert [(row.account_id, row.source_key) for row in rows] == [
        ("3", "account:3:plk:128"),
        ("4", "account:4:plk:128"),
    ]
    assert result["inventory"]["duplicates"] == 0


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
    rows = db.execute(select(CompetitorPriceList).order_by(CompetitorPriceList.account_id.asc())).scalars().all()
    assert [(row.account_id, row.source_key) for row in rows] == [
        ("3", "account:3:plk:128"),
        ("4", "account:4:plk:128"),
    ]
    assert result["inventory"]["duplicates"] == 0


def test_sequential_selected_provisor_refresh_preserves_unselected_account_rows_and_assignments(monkeypatch):
    import backend.app.main as main
    from backend.app.services.competitor_price_lists import list_competitor_price_lists

    db = _session()
    pf = PriceFormat(code="0001", name="Format 0001")
    db.add(pf)
    db.add_all(
        [
            PriceSourceAccount(id=1, source_type="provisor", login="Жасулан-Фарм", encrypted_password="x"),
            PriceSourceAccount(id=12, source_type="provisor", login="arai2/3", encrypted_password="x"),
            PriceSourceAccount(id=15, source_type="provisor", login="Есмамбетова", encrypted_password="x"),
        ]
    )
    db.commit()

    adapter = _AccountScopedPlkAdapter({1: [159], 12: [159], 15: [159]})
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    first = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="0001",
            payload={"source": "provisor", "accountIds": [12, 15], "forceRefresh": True, "maxParallelAccounts": 2},
            db=db,
        )
    )
    assert sorted(adapter.fetched_account_item_ids) == ["12:159", "15:159"]
    assert first["accounts_processed"] == [12, 15]

    first_rows = db.execute(select(CompetitorPriceList).order_by(CompetitorPriceList.account_id.asc())).scalars().all()
    assert [(row.account_id, row.external_price_list_id, row.source_key) for row in first_rows] == [
        ("12", "159", "account:12:plk:159"),
        ("15", "159", "account:15:plk:159"),
    ]
    db.add_all(
        [
            PriceFormatCompetitorAssignment(price_format_id=pf.id, competitor_price_list_id=row.id, is_active=True)
            for row in first_rows
        ]
    )
    db.commit()

    adapter.fetched_account_item_ids.clear()
    second = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="0001",
            payload={
                "source": "provisor",
                "accountIds": [1],
                "forceRefresh": True,
                "runRebuildAfterRefresh": True,
            },
            db=db,
        )
    )

    assert adapter.fetched_account_item_ids == ["1:159"]
    assert second["accounts_processed"] == [1]
    rows = db.execute(select(CompetitorPriceList).order_by(CompetitorPriceList.account_id.asc())).scalars().all()
    assert [(row.account_id, row.external_price_list_id, row.source_key) for row in rows] == [
        ("1", "159", "account:1:plk:159"),
        ("12", "159", "account:12:plk:159"),
        ("15", "159", "account:15:plk:159"),
    ]
    item_counts = dict(
        db.execute(
            select(CompetitorPriceList.account_id, func.count(CompetitorPriceListItem.id))
            .join(CompetitorPriceListItem, CompetitorPriceListItem.price_list_id == CompetitorPriceList.id)
            .group_by(CompetitorPriceList.account_id)
        ).all()
    )
    assert item_counts == {"1": 1, "12": 1, "15": 1}
    saved_items = {
        row.account_id: item.distributor_goods_id
        for row, item in db.execute(
            select(CompetitorPriceList, CompetitorPriceListItem)
            .join(CompetitorPriceListItem, CompetitorPriceListItem.price_list_id == CompetitorPriceList.id)
        ).all()
    }
    assert saved_items == {
        "1": "SKU-1-159",
        "12": "SKU-12-159",
        "15": "SKU-15-159",
    }
    active_assignments = {
        row.account_id
        for row in (
            db.execute(
                select(CompetitorPriceList)
                .join(PriceFormatCompetitorAssignment, PriceFormatCompetitorAssignment.competitor_price_list_id == CompetitorPriceList.id)
                .where(PriceFormatCompetitorAssignment.price_format_id == pf.id)
                .where(PriceFormatCompetitorAssignment.is_active.is_(True))
            )
            .scalars()
            .all()
        )
    }
    assert active_assignments == {"12", "15"}
    visible = list_competitor_price_lists(db=db, price_format_code="0001")
    assert sorted((str(row["accountId"]), str(row["filialId"])) for row in visible) == [
        ("1", "159"),
        ("12", "159"),
        ("15", "159"),
    ]
    result_summaries = second["accounts"][0]["results"]
    assert result_summaries == [
        {
            "ok": True,
            "sourceType": "provisor",
            "accountId": 1,
            "priceListId": "159",
            "external_price_list_id": "159",
            "rows": 1,
            "itemsCount": 1,
            "elapsed_ms": result_summaries[0]["elapsed_ms"],
            "status": "ok",
            "http_status": None,
            "timeout": False,
            "skipped": False,
            "skipped_unchanged": False,
            "skipped_heavy": False,
            "duplicate": False,
            "error": "",
            "localItemsCount": 0,
            "skippedInfo": None,
        }
    ]
    assert "items" not in result_summaries[0]
    assert "priceList" not in result_summaries[0]


def test_provisor_memory_logs_include_session_close_and_summary_payloads(monkeypatch, caplog):
    import backend.app.main as main

    db = _session()
    pf = PriceFormat(code="0001", name="Format 0001")
    account = PriceSourceAccount(id=1, source_type="provisor", login="Жасулан-Фарм", encrypted_password="x")
    db.add_all([pf, account])
    db.commit()
    adapter = _AccountScopedPlkAdapter({1: [159]})
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)
    captured_memory = []
    original_record_memory = main._record_provisor_plk_memory

    def record_memory_wrapper(*args, **kwargs):
        payload = original_record_memory(*args, **kwargs)
        captured_memory.append(payload)
        return payload

    monkeypatch.setattr(main, "_record_provisor_plk_memory", record_memory_wrapper)

    with caplog.at_level(logging.INFO):
        result = asyncio.run(
            main._run_refresh_price_lists_logic(
                format_code="0001",
                payload={"source": "provisor", "accountId": 1, "forceRefresh": True},
                db=db,
            )
        )

    memory = captured_memory + _memory_payloads(caplog)
    stages = {row.get("stage") for row in memory}
    assert {"before_fetch", "before_db_replacement", "after_session_close", "after_cleanup"}.issubset(stages)
    after_close = [row for row in memory if row.get("stage") == "after_session_close"]
    assert after_close
    assert all(row.get("identity_map_size") == 0 for row in after_close)
    assert "items" not in result["accounts"][0]["results"][0]
    assert "priceList" not in result["accounts"][0]["results"][0]


def test_provisor_connection_listing_does_not_wipe_populated_snapshot(monkeypatch):
    from backend.app.services.competitor_price_lists import upsert_unified_price_list
    import backend.app.services.price_source_accounts as account_service

    db = _session()
    pf = PriceFormat(code="0001", name="Format 0001")
    account = PriceSourceAccount(id=12, source_type="provisor", login="arai2/3", encrypted_password="x")
    db.add_all([pf, account])
    db.commit()
    adapter = _AccountScopedPlkAdapter({12: [159]})
    price_list = asyncio.run(adapter.fetch_price_lists(account))[0]
    upsert_unified_price_list(
        db=db,
        price_format_code="0001",
        price_list=price_list,
        items=asyncio.run(adapter.fetch_price_list_items(account, price_list)),
        status="updated",
        run_matching=False,
    )
    before_items = db.execute(select(CompetitorPriceListItem.distributor_goods_id)).scalars().all()
    assert before_items == ["SKU-12-159"]

    monkeypatch.setattr(account_service, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(account_service, "credentials_from_row", _fake_credentials)

    result_account = asyncio.run(account_service.test_account_connection(db=db, account_id=12, price_format_code="0001"))

    row = db.execute(select(CompetitorPriceList).where(CompetitorPriceList.account_id == "12")).scalar_one()
    after_items = db.execute(select(CompetitorPriceListItem.distributor_goods_id)).scalars().all()
    assert result_account.status == "connected"
    assert row.source_key == "account:12:plk:159"
    assert row.last_refresh_status == "updated"
    assert after_items == before_items


def test_full_provisor_refresh_keeps_same_external_plk_populated_for_all_accounts(monkeypatch):
    import backend.app.main as main
    from backend.app.services.competitor_price_lists import list_competitor_price_lists

    db = _session()
    pf = PriceFormat(code="0001", name="Format 0001")
    db.add(pf)
    db.add_all(
        [
            PriceSourceAccount(id=1, source_type="provisor", login="Жасулан-Фарм", encrypted_password="x"),
            PriceSourceAccount(id=12, source_type="provisor", login="arai2/3", encrypted_password="x"),
            PriceSourceAccount(id=15, source_type="provisor", login="Есмамбетова", encrypted_password="x"),
        ]
    )
    db.commit()
    adapter = _AccountScopedPlkAdapter({1: [159], 12: [159], 15: [159]})
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", _fake_credentials)

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="0001",
            payload={"source": "provisor", "forceRefresh": True, "maxParallelAccounts": 3},
            db=db,
        )
    )

    assert sorted(adapter.fetched_account_item_ids) == ["12:159", "15:159", "1:159"]
    assert result["inventory"]["unique_plk"] == 3
    item_counts = dict(
        db.execute(
            select(CompetitorPriceList.account_id, func.count(CompetitorPriceListItem.id))
            .join(CompetitorPriceListItem, CompetitorPriceListItem.price_list_id == CompetitorPriceList.id)
            .group_by(CompetitorPriceList.account_id)
        ).all()
    )
    assert item_counts == {"1": 1, "12": 1, "15": 1}
    visible = list_competitor_price_lists(db=db, price_format_code="0001")
    assert sorted((str(row["accountId"]), str(row["filialId"]), row["itemsCount"]) for row in visible) == [
        ("1", "159", 1),
        ("12", "159", 1),
        ("15", "159", 1),
    ]


def test_provisor_unchanged_updates_checked_status_and_preserves_items(monkeypatch):
    import backend.app.main as main

    db = _session()
    pf, _account = _seed(db)
    existing = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="account:3:plk:128",
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
        adapter.fetched_account_item_ids.append(f"{account.id}:{price_list.price_list_id}")
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

    row = db.execute(select(CompetitorPriceList).where(CompetitorPriceList.source_key == "account:3:plk:128")).scalar_one()
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
        source_key="account:3:plk:128",
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
    assert result["provisorAudit"]["timed_out"] == 1
    assert result["provisorAudit"]["failure_skip_reasons"]["timeout_0.01s"] == 1
    assert db.execute(select(CompetitorPriceListItem).where(CompetitorPriceListItem.price_list_id == existing.id)).scalar_one().name == "Old Item"


def test_provisor_zero_response_does_not_wipe_existing_items(monkeypatch, caplog):
    import backend.app.main as main

    db = _session()
    pf, _account = _seed(db)
    existing = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="account:3:plk:1397",
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

    with caplog.at_level(logging.INFO):
        result = asyncio.run(
            main._run_refresh_price_lists_logic(
                format_code="FMT",
                payload={"source": "provisor", "accountId": 3, "provisorFilialIds": [1397], "forceRefresh": True},
                db=db,
            )
        )

    row = db.execute(select(CompetitorPriceList).where(CompetitorPriceList.source_key == "account:3:plk:1397")).scalar_one()
    items = db.execute(select(CompetitorPriceListItem).where(CompetitorPriceListItem.price_list_id == row.id)).scalars().all()
    payloads = _benchmark_payloads(caplog)
    assert result["progress"]["success_zero_items"] == 1
    assert result["provisorAudit"]["success_zero"] == 1
    assert result["provisorAudit"]["failure_skip_reasons"]["empty_response_preserved_previous_rows"] == 1
    assert row.last_refresh_status == "success_zero_items"
    assert [item.name for item in items] == ["Old Item"]
    assert payloads
    assert payloads[0]["outcome"] == "unchanged"
    assert payloads[0]["skip_reason"] == "empty_response_preserved_previous_rows"


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
