from __future__ import annotations

import asyncio
import inspect
import json
import os
import sqlite3
import time
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import CompetitorPriceList, CompetitorPriceListItem, PriceFormat, PriceSourceAccount
from backend.app.services.emit_worker import (
    EmitConfig,
    EmitStats,
    EmitWorker,
    cleanup_temp,
    deduplicate_emit_items,
    download_emit_filial,
    iter_stage_rows,
    is_emit_plk,
    normalize_emit_item,
    open_stage_db,
    parse_normalize_stage,
    replace_emit_price_list_from_staging,
    stage_row_count,
)
from backend.app.services.price_sources import UnifiedPriceItem, UnifiedPriceList


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _session_factory_static():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_format(db):
    pf = PriceFormat(code="FMT", name="Format")
    db.add(pf)
    db.commit()
    return pf


def test_emit_detection_by_name_and_configured_filial_ids():
    config = EmitConfig(filial_ids=[1106, 8371])

    assert is_emit_plk(filial_id=1106, name="Any", config=config)
    assert is_emit_plk(filial_id=999, name="Emit International Almaty", config=config)
    assert is_emit_plk(filial_id=999, name="Amity International", config=config)
    assert is_emit_plk(filial_id=999, name="Эмити Алматы", config=config)
    assert is_emit_plk(filial_id=999, name="Р­РјРёС‚Рё РРЅС‚РµСЂРЅРµС€РЅР»", config=config)
    assert not is_emit_plk(filial_id=999, name="Regular Provisor", config=config)


def test_normal_provisor_refresh_skips_emit_ids_and_keeps_old_data(monkeypatch):
    import backend.app.main as main

    db = _session()
    pf = _seed_format(db)
    account = PriceSourceAccount(id=3, source_type="provisor", login="login", encrypted_password="x")
    existing = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="3:1106",
        display_name="Old Emit",
        account_id="3",
        external_price_list_id="1106",
    )
    db.add_all([account, existing])
    db.flush()
    db.add(CompetitorPriceListItem(price_list_id=existing.id, name="Old Item", distributor_price=10))
    db.commit()

    class Adapter:
        source = "provisor"

        def __init__(self):
            self.fetched_item_ids = []

        async def fetch_price_lists(self, account):
            return [
                UnifiedPriceList(
                    source="provisor",
                    account_id=str(account.id),
                    account_login=account.login,
                    price_list_id="1106",
                    price_list_name="Emit International Almaty",
                    distributor_name="Emit International Almaty",
                )
            ]

        async def fetch_price_list_items(self, account, price_list):
            self.fetched_item_ids.append(str(price_list.price_list_id))
            return [
                UnifiedPriceItem(
                    source="provisor",
                    account_id=str(account.id),
                    price_list_id="1106",
                    price_list_name="Emit",
                    distributor_name="Emit",
                    product_name="New Item",
                    manufacturer="",
                    registration_number="",
                    distributor_product_name="New Item",
                    distributor_product_id="NEW",
                    distributor_price=Decimal("1"),
                    stock=None,
                    pack_quantity=None,
                    expiry_date=None,
                    raw={"goodsId": 1},
                )
            ]

    adapter = Adapter()
    monkeypatch.setattr(main, "adapter_for_source", lambda source: adapter)
    monkeypatch.setattr(main, "credentials_from_row", lambda row: SimpleNamespace(id=row.id, source_type=row.source_type, login=row.login, password="", config={}))

    result = asyncio.run(
        main._run_refresh_price_lists_logic(
            format_code="FMT",
            payload={"source": "provisor", "accountId": 3, "provisorFilialIds": [1106], "forceRefresh": True},
            db=db,
        )
    )

    assert adapter.fetched_item_ids == []
    assert result["progress"]["skipped_heavy"] == 1
    item = db.execute(select(CompetitorPriceListItem).where(CompetitorPriceListItem.price_list_id == existing.id)).scalar_one()
    assert item.name == "Old Item"


def test_streaming_parser_preserves_multiple_prices_per_goods_id(tmp_path):
    source = tmp_path / "emit.json"
    staging = tmp_path / "stage.sqlite"
    source.write_text(
        json.dumps(
            [
                {"id": 1, "goodsId": 10, "distributorGoodsName": "A", "distributorProducer": "P", "goodsPrice": 12},
                {"id": 2, "goodsId": 10, "distributorGoodsName": "A full", "distributorProducer": "P", "goodsPrice": 9},
                {"id": 3, "goodsId": None, "distributorGoodsName": "B", "distributorProducer": "P", "goodsPrice": 5},
                {"id": 4, "goodsId": 11, "distributorGoodsName": "Zero", "goodsPrice": 0},
            ]
        ),
        encoding="utf-8",
    )

    stats = parse_normalize_stage(
        source_path=source,
        stage_db_path=staging,
        filial_id=1106,
        filial_name="Emit",
        config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, min_final_rows=1),
    )
    rows = [row for batch in iter_stage_rows(staging, batch_size=10) for row in batch]

    assert stats.input_rows == 4
    assert stats.zero_price_rows_skipped == 1
    assert stats.duplicate_rows_removed == 0
    assert stats.rows_without_goodsId == 1
    assert len(rows) == 3
    assert [row["distributor_price"] for row in rows if row["provisor_goods_id"] == 10] == [12.0, 9.0]


def test_parse_100k_synthetic_duplicates_bounded_memory(tmp_path):
    source = tmp_path / "emit_100k.ndjson"
    stage = tmp_path / "emit_stage.sqlite"
    with source.open("w", encoding="utf-8") as fh:
        for index in range(100_000):
            goods_id = index % 1000
            price = 1000 - (index % 100)
            fh.write(
                json.dumps(
                    {
                        "id": index,
                        "goodsId": goods_id,
                        "distributorGoodsName": f"Item {goods_id}",
                        "distributorProducer": "P",
                        "goodsPrice": price,
                    }
                )
                + "\n"
            )

    started = time.perf_counter()
    stats = parse_normalize_stage(
        source_path=source,
        stage_db_path=stage,
        filial_id=1106,
        filial_name="Emit",
        config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, min_final_rows=100, batch_insert_size=1000),
    )

    assert stats.input_rows == 100_000
    assert stats.final_rows_saved == 100_000
    assert stats.duplicate_rows_removed == 0
    assert stats.stage_db_size_mb > 0
    assert time.perf_counter() - started < 60


def test_parse_normalize_stage_does_not_accumulate_normalized_list():
    source = inspect.getsource(parse_normalize_stage)

    assert "normalized: list" not in source
    assert ".append(item)" not in source
    assert "deduplicate_emit_items(" not in source


def test_deduplicate_prefers_goods_id_positive_price_and_full_name():
    from backend.app.services.emit_worker import EmitStats

    stats = EmitStats()
    rows = deduplicate_emit_items(
        [
            {"provisor_goods_id": 1, "name": "A", "raw_manufacturer": "P", "distributor_price": 20},
            {"provisor_goods_id": 1, "name": "A full name", "raw_manufacturer": "P", "distributor_price": 10},
            {"provisor_goods_id": None, "name": "B", "raw_manufacturer": "P", "distributor_price": 5},
        ],
        stats,
    )

    assert len(rows) == 2
    assert next(row for row in rows if row["provisor_goods_id"] == 1)["distributor_price"] == 10


def test_sqlite_staging_keeps_multiple_positive_prices_for_same_goods_id(tmp_path):
    from backend.app.services.emit_worker import _stage_upsert

    stage = tmp_path / "stage.sqlite"
    stats = EmitStats()
    conn = open_stage_db(stage)
    try:
        _stage_upsert(
            conn,
            {"provisor_goods_id": 10, "name": "Short", "raw_manufacturer": "P", "distributor_price": 20},
            stats,
        )
        _stage_upsert(
            conn,
            {"provisor_goods_id": 10, "name": "Long full name", "raw_manufacturer": "P", "distributor_price": 9},
            stats,
        )
        conn.commit()
    finally:
        conn.close()

    rows = [row for batch in iter_stage_rows(stage, batch_size=10) for row in batch]
    assert stage_row_count(stage) == 2
    assert stats.duplicate_rows_removed == 0
    assert [row["name"] for row in rows] == ["Short", "Long full name"]
    assert [row["distributor_price"] for row in rows] == [20, 9]


def _parse_rows_to_stage(tmp_path, rows):
    source = tmp_path / "emit.ndjson"
    stage = tmp_path / "stage.sqlite"
    with source.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    stats = parse_normalize_stage(
        source_path=source,
        stage_db_path=stage,
        filial_id=1106,
        filial_name="Emit",
        config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, min_final_rows=1),
    )
    staged = [row for batch in iter_stage_rows(stage, batch_size=100) for row in batch]
    return stats, staged


def _real_emit_sample_row():
    return {
        "id": 987654,
        "goodsId": 12345,
        "goodsPrice": 11885.77,
        "goodsPriceWithUserDiscount": 0,
        "filialId": 1106,
        "insertedDate": "2026-06-01T10:20:30",
        "shelfLife": "2027-12-31",
        "pack": 1,
        "box": 10,
        "multiplicity": 1,
        "stored": 42,
        "distributorGoodsId": "EMIT-SKU-1",
        "distributorGoodsName": "Distributor fallback name",
        "distributorProducer": "Real Producer",
        "priceStatus": "ok",
        "userDiscount": 0,
        "goods": {
            "id": 12345,
            "fullName": "Real Goods FullName N10",
            "number": "N10",
            "barcodes": ["4870000000011"],
            "producer": "Nested Producer",
            "country": "KZ",
            "name": "Nested fallback name",
            "price": 99999,
        },
    }


def test_real_provisor_emit_sample_normalizes_with_goods_price():
    item = normalize_emit_item(_real_emit_sample_row(), filial_id=1106, filial_name="Emit")

    assert item is not None
    assert item["distributor_price"] == 11885.77
    assert item["name"] == "Real Goods FullName N10"
    assert item["raw_manufacturer"] == "REAL PRODUCER"
    assert item["provisor_goods_id"] == 12345
    assert item["filial_id"] == 1106
    assert item["distributor_goods_id"] == "EMIT-SKU-1"
    assert item["stock"] == 42
    assert item["expiry_date"] == "2027-12-31"
    assert item["source_item_id"] == 987654
    assert item["variant_key"] == "sku:emit-sku-1"
    assert "number:n10" in item["pack_signature"]


def test_real_provisor_emit_sample_stages_one_valid_row(tmp_path):
    stats, rows = _parse_rows_to_stage(tmp_path, [_real_emit_sample_row()])

    assert stats.input_rows == 1
    assert stats.final_rows_saved == 1
    assert stats.skip_reasons == {}
    assert stats.key_type_counts["goodsId+variant"] == 1
    assert len(rows) == 1
    assert rows[0]["distributor_price"] == 11885.77


def test_real_emit_skip_reason_diagnostics(tmp_path):
    stats, rows = _parse_rows_to_stage(
        tmp_path,
        [
            {"id": 1, "goodsId": 1, "goods": {"fullName": "Missing price"}},
            {"id": 2, "goodsId": 2, "goodsPrice": 0, "goodsPriceWithUserDiscount": 10, "goods": {"fullName": "Zero price"}},
            {"id": 3, "goodsId": 3, "goodsPrice": 10},
            _real_emit_sample_row(),
        ],
    )

    assert len(rows) == 1
    assert stats.zero_price_rows_skipped == 2
    assert stats.skip_reasons["missing_price"] == 1
    assert stats.skip_reasons["invalid_price"] == 1
    assert stats.skip_reasons["missing_name"] == 1


def test_same_goods_id_different_barcode_keep_separate(tmp_path):
    stats, rows = _parse_rows_to_stage(
        tmp_path,
        [
            {"id": 1, "goodsId": 10, "barcode": "4870000000011", "distributorGoodsName": "A", "goodsPrice": 10},
            {"id": 2, "goodsId": 10, "barcode": "4870000000028", "distributorGoodsName": "A", "goodsPrice": 9},
        ],
    )

    assert len(rows) == 2
    assert stats.key_type_counts["goodsId+variant"] == 2


def test_same_goods_id_different_distributor_goods_id_keep_separate(tmp_path):
    stats, rows = _parse_rows_to_stage(
        tmp_path,
        [
            {"id": 1, "goodsId": 10, "distributorGoodsId": "SKU-A", "distributorGoodsName": "A", "goodsPrice": 10},
            {"id": 2, "goodsId": 10, "distributorGoodsId": "SKU-B", "distributorGoodsName": "A", "goodsPrice": 9},
        ],
    )

    assert len(rows) == 2
    assert stats.key_type_counts["goodsId+variant"] == 2


def test_same_goods_id_different_pack_size_keep_separate(tmp_path):
    stats, rows = _parse_rows_to_stage(
        tmp_path,
        [
            {"id": 1, "goodsId": 10, "distributorGoodsName": "A №10", "box": 10, "goodsPrice": 10},
            {"id": 2, "goodsId": 10, "distributorGoodsName": "A №20", "box": 20, "goodsPrice": 9},
        ],
    )

    assert len(rows) == 2
    assert stats.key_type_counts["goodsId+pack+producer"] == 2


def test_same_goods_id_exact_duplicates_different_prices_keep_all(tmp_path):
    stats, rows = _parse_rows_to_stage(
        tmp_path,
        [
            {"id": 1, "goodsId": 10, "distributorGoodsId": "SKU-A", "distributorGoodsName": "A", "goodsPrice": 12},
            {"id": 2, "goodsId": 10, "distributorGoodsId": "SKU-A", "distributorGoodsName": "A", "goodsPrice": 8},
        ],
    )

    assert len(rows) == 2
    assert [row["distributor_price"] for row in rows] == [12.0, 8.0]
    assert stats.duplicate_rows_removed == 0


def test_no_goods_id_same_name_manufacturer_different_prices_keep_all(tmp_path):
    stats, rows = _parse_rows_to_stage(
        tmp_path,
        [
            {"id": 1, "distributorGoodsName": "NoId A №10", "distributorProducer": "Maker", "goodsPrice": 12},
            {"id": 2, "distributorGoodsName": "NoId A №10", "distributorProducer": "Maker", "goodsPrice": 8},
        ],
    )

    assert len(rows) == 2
    assert [row["distributor_price"] for row in rows] == [12.0, 8.0]
    assert stats.key_type_counts["fallback"] == 2


def test_no_goods_id_different_pack_size_keep_separate(tmp_path):
    _stats, rows = _parse_rows_to_stage(
        tmp_path,
        [
            {"id": 1, "distributorGoodsName": "NoId A №10", "distributorProducer": "Maker", "goodsPrice": 12},
            {"id": 2, "distributorGoodsName": "NoId A №20", "distributorProducer": "Maker", "goodsPrice": 8},
        ],
    )

    assert len(rows) == 2


def test_fallback_rows_keep_all_positive_prices(tmp_path):
    stats, _rows = _parse_rows_to_stage(
        tmp_path,
        [
            {"id": 1, "distributorGoodsName": "NoId A", "distributorProducer": "Maker", "goodsPrice": 12},
            {"id": 2, "distributorGoodsName": "NoId A", "distributorProducer": "Maker", "goodsPrice": 8},
            {"id": 3, "distributorGoodsName": "NoId A", "distributorProducer": "Maker", "goodsPrice": 7},
        ],
    )

    assert stats.final_rows_saved == 3
    assert stats.duplicate_rows_removed == 0


def test_stage_audit_reports_suspicious_groups(tmp_path):
    rows = [
        {"id": i, "goodsId": 10, "distributorGoodsId": f"SKU-{i}", "distributorGoodsName": f"A {i}", "goodsPrice": i + 1}
        for i in range(10)
    ]
    stats, _staged = _parse_rows_to_stage(tmp_path, rows)

    assert any(item["reason"] == "same_goodsId_many_variants" for item in stats.suspicious_groups)


def test_failed_parse_keeps_old_data(tmp_path):
    db = _session()
    pf = _seed_format(db)
    old = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="emit:1106",
        display_name="Old Emit",
    )
    db.add(old)
    db.flush()
    db.add(CompetitorPriceListItem(price_list_id=old.id, name="Old Item", distributor_price=10))
    db.commit()

    bad = tmp_path / "bad.json"
    bad.write_text("[{bad json", encoding="utf-8")
    try:
        parse_normalize_stage(
            source_path=bad,
            stage_db_path=tmp_path / "stage.sqlite",
            filial_id=1106,
            filial_name="Emit",
            config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, min_final_rows=1),
        )
    except Exception:
        pass

    item = db.execute(select(CompetitorPriceListItem).where(CompetitorPriceListItem.price_list_id == old.id)).scalar_one()
    assert item.name == "Old Item"


def test_successful_parse_creates_selectable_competitor_price_list(tmp_path):
    db = _session()
    _seed_format(db)
    staging = tmp_path / "stage.sqlite"
    source = tmp_path / "emit.ndjson"
    source.write_text(
        json.dumps({"id": 1, "goodsId": 10, "distributorGoodsName": "A", "distributorGoodsId": "SKU", "goodsPrice": 7.5}) + "\n",
        encoding="utf-8",
    )
    stats = parse_normalize_stage(
        source_path=source,
        stage_db_path=staging,
        filial_id=1106,
        filial_name="Emit International Almaty",
        config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, min_final_rows=1),
    )

    row = replace_emit_price_list_from_staging(
        db=db,
        config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, batch_insert_size=1, min_final_rows=1),
        filial_id=1106,
        filial_name="Emit International Almaty",
        staging_path=staging,
        stats=stats,
    )

    assert row.source_type == "provisor"
    assert row.source_key == "emit:1106"
    assert row.last_refresh_status == "success"
    assert row.account_id == ""
    assert db.scalar(select(CompetitorPriceListItem).where(CompetitorPriceListItem.price_list_id == row.id).with_only_columns(CompetitorPriceListItem.id).limit(1)) is not None


def test_top_level_object_with_data_items_parsed_streaming(tmp_path):
    source = tmp_path / "emit_object.json"
    stage = tmp_path / "stage.sqlite"
    source.write_text(
        json.dumps(
            {
                "data": [
                    {"id": 1, "goodsId": 1, "distributorGoodsName": "A", "goodsPrice": 1},
                    {"id": 2, "goodsId": 2, "distributorGoodsName": "B", "goodsPrice": 2},
                ]
            }
        ),
        encoding="utf-8",
    )

    stats = parse_normalize_stage(
        source_path=source,
        stage_db_path=stage,
        filial_id=1106,
        filial_name="Emit",
        config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, min_final_rows=1),
    )

    assert stats.input_rows == 2
    assert stage_row_count(stage) == 2


def test_suspicious_low_row_count_fails(tmp_path):
    source = tmp_path / "emit.ndjson"
    stage = tmp_path / "stage.sqlite"
    source.write_text(
        json.dumps({"id": 1, "goodsId": 1, "distributorGoodsName": "A", "goodsPrice": 1}) + "\n",
        encoding="utf-8",
    )

    try:
        parse_normalize_stage(
            source_path=source,
            stage_db_path=stage,
            filial_id=1106,
            filial_name="Emit",
            config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, min_final_rows=100),
        )
    except RuntimeError as exc:
        assert "suspiciously low" in str(exc)
    else:
        raise AssertionError("Expected suspicious low row count failure")


def test_error_body_reads_only_64kb(monkeypatch, tmp_path):
    import backend.app.services.emit_worker as emit_worker

    class FakeResponse:
        status_code = 500
        headers = {}

        def __init__(self):
            self.bytes_yielded = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aiter_bytes(self, size):
            for _ in range(100):
                self.bytes_yielded += 8192
                yield b"x" * 8192

    fake_response = FakeResponse()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, *args, **kwargs):
            return fake_response

    async def fake_token(**kwargs):
        return "token"

    monkeypatch.setattr(emit_worker, "get_access_token", fake_token)
    monkeypatch.setattr(emit_worker.httpx, "AsyncClient", FakeClient)

    try:
        asyncio.run(
            download_emit_filial(
                config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0),
                filial_id=1106,
                filial_name="Emit",
            )
        )
    except RuntimeError as exc:
        assert "HTTP 500" in str(exc)
    else:
        raise AssertionError("Expected download failure")

    assert fake_response.bytes_yielded <= 72 * 1024


def test_new_row_count_ratio_drop_fails_and_keeps_old_rows(tmp_path):
    db = _session()
    pf = _seed_format(db)
    old = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="emit:1106",
        display_name="Old Emit",
    )
    db.add(old)
    db.flush()
    for index in range(10):
        db.add(CompetitorPriceListItem(price_list_id=old.id, name=f"Old {index}", distributor_price=10))
    db.commit()

    source = tmp_path / "emit.ndjson"
    stage = tmp_path / "stage.sqlite"
    source.write_text(
        json.dumps({"id": 1, "goodsId": 1, "distributorGoodsName": "A", "goodsPrice": 1}) + "\n",
        encoding="utf-8",
    )
    stats = parse_normalize_stage(
        source_path=source,
        stage_db_path=stage,
        filial_id=1106,
        filial_name="Emit",
        config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, min_final_rows=1),
    )

    try:
        replace_emit_price_list_from_staging(
            db=db,
            config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, min_final_rows=1, min_row_ratio=0.5),
            filial_id=1106,
            filial_name="Emit",
            staging_path=stage,
            stats=stats,
        )
    except RuntimeError as exc:
        assert "dropped below ratio" in str(exc)
        db.rollback()
    else:
        raise AssertionError("Expected row ratio validation failure")

    assert db.scalar(select(CompetitorPriceListItem).where(CompetitorPriceListItem.price_list_id == old.id).with_only_columns(func.count(CompetitorPriceListItem.id))) == 10


def test_temp_cleanup(tmp_path):
    files = [tmp_path / "emit_old.json", tmp_path / "emit_stage_old.sqlite", tmp_path / "emit_stage_old.sqlite-wal", tmp_path / "emit_stage_old.jsonl"]
    for old in files:
        old.write_text("{}", encoding="utf-8")
        old_time = old.stat().st_mtime - 48 * 3600
        os.utime(old, (old_time, old_time))

    assert cleanup_temp(EmitConfig(temp_dir=str(tmp_path), cleanup_temp_hours=24)) == len(files)
    assert all(not old.exists() for old in files)


def test_no_concurrent_emit_jobs():
    Session = _session_factory_static()
    worker = EmitWorker(session_factory=Session, config=EmitConfig(temp_dir="unused"))

    job, blocker, _token = worker.create_job(mode="selected", filial_ids=[1106])
    second, second_blocker, _ = worker.create_job(mode="selected", filial_ids=[1107])

    assert job is not None
    assert blocker is None
    assert second is None
    assert second_blocker is not None


def test_status_endpoint(monkeypatch):
    import backend.app.main as main

    Session = _session_factory_static()
    worker = EmitWorker(session_factory=Session, config=EmitConfig(temp_dir="unused"))
    job, _blocker, _token = worker.create_job(mode="selected", filial_ids=[1106])

    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(main, "_emit_worker", worker)
    main.app.dependency_overrides[main.get_db] = lambda: Session()
    try:
        client = TestClient(main.app)
        response = client.get("/api/emit/refresh/status")
        assert response.status_code == 200
        assert response.json()["source_type"] == "emit"
        assert response.json()["status"] == "pending"
        assert job is not None
    finally:
        main.app.dependency_overrides.clear()
