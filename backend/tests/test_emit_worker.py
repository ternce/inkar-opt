from __future__ import annotations

import asyncio
import inspect
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import (
    CompetitorPriceList,
    CompetitorPriceListItem,
    CompetitorPricePercentile,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    PriceSourceAccount,
    Product,
    RefreshJob,
)
from backend.app.services.emit_worker import (
    EmitConfig,
    EmitStats,
    EmitWorker,
    active_emit_job,
    cleanup_temp,
    deduplicate_emit_items,
    download_emit_filial,
    iter_stage_rows,
    is_emit_plk,
    mark_stale_emit_jobs,
    normalize_emit_item,
    open_stage_db,
    parse_normalize_stage,
    replace_emit_price_list_from_staging,
    stage_row_count,
    _recalculate_percentiles_for_emit_rows,
)
from backend.app.services.competitor_assignments import (
    propagate_emit_assignments_to_new_price_format,
    propagate_emit_assignments_to_price_formats,
)
from backend.app.services.competitors.percentiles.read_models import list_percentile_sources
from backend.app.services import provisor_auto_refresh as refresh_svc
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


def test_emit_percentile_rebuild_uses_assigned_price_format_not_first_format(tmp_path):
    db = _session()
    first_pf = PriceFormat(code="FIRST", name="FIRST", branch="Other")
    selected_pf = PriceFormat(code="SELECTED", name="SELECTED", branch="Emit International Almaty")
    product = Product(code="163571", name="163571", cost=100, provisor_goods_id=163571)
    db.add_all([first_pf, selected_pf, product])
    db.flush()
    staging = tmp_path / "stage.sqlite"
    source = tmp_path / "emit.ndjson"
    source.write_text(
        "\n".join(
            [
                json.dumps({"id": 1, "goodsId": 163571, "distributorGoodsName": "A", "distributorGoodsId": "163571", "goodsPrice": 8688.26}),
                json.dumps({"id": 2, "goodsId": 163571, "distributorGoodsName": "A", "distributorGoodsId": "163571", "goodsPrice": 8989.73}),
                json.dumps({"id": 3, "goodsId": 163571, "distributorGoodsName": "A", "distributorGoodsId": "163571", "goodsPrice": 9358.46}),
            ]
        ),
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
        config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, batch_insert_size=10, min_final_rows=1),
        filial_id=1106,
        filial_name="Emit International Almaty",
        staging_path=staging,
        stats=stats,
        price_format_code=selected_pf.code,
    )
    db.add(PriceFormatCompetitorAssignment(price_format_id=selected_pf.id, competitor_price_list_id=row.id, is_active=True))
    db.commit()

    result = _recalculate_percentiles_for_emit_rows(db, price_list_ids=[row.id], price_format_code=selected_pf.code)
    summary = result["summaries"]

    assert row.price_format_id == selected_pf.id
    assert result["assigned_price_format_ids"] == [first_pf.id, selected_pf.id]
    assert "FIRST" in summary
    assert "SELECTED" in summary
    assert summary["SELECTED"]["products_with_competitors"] == 1
    assert db.query(CompetitorPricePercentile).filter(CompetitorPricePercentile.price_format_id == first_pf.id).count() > 0
    rows = db.query(CompetitorPricePercentile).filter(CompetitorPricePercentile.price_format_id == selected_pf.id).all()
    by_percentile = {item.percentile: float(item.value) for item in rows if item.percentile_scope == "regional"}
    assert round(by_percentile[10], 3) == 8748.554
    assert round(by_percentile[60], 3) == 9063.476


def test_emit_percentile_rebuild_explicit_format_creates_assignment(tmp_path):
    db = _session()
    pf = PriceFormat(code="888", name="Format 888", branch="Emit International Almaty")
    product = Product(code="163571", name="163571", cost=100, provisor_goods_id=163571)
    db.add_all([pf, product])
    db.flush()
    staging = tmp_path / "stage.sqlite"
    source = tmp_path / "emit.ndjson"
    source.write_text(
        "\n".join(
            [
                json.dumps({"id": 1, "goodsId": 163571, "distributorGoodsName": "A", "distributorGoodsId": "163571", "goodsPrice": 8688.26}),
                json.dumps({"id": 2, "goodsId": 163571, "distributorGoodsName": "A", "distributorGoodsId": "163571", "goodsPrice": 8989.73}),
            ]
        ),
        encoding="utf-8",
    )
    stats = parse_normalize_stage(
        source_path=source,
        stage_db_path=staging,
        filial_id=1108,
        filial_name="Emit International 1108",
        config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, min_final_rows=1),
    )
    row = replace_emit_price_list_from_staging(
        db=db,
        config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, batch_insert_size=10, min_final_rows=1),
        filial_id=1108,
        filial_name="Emit International 1108",
        staging_path=staging,
        stats=stats,
        price_format_code=pf.code,
    )

    result = _recalculate_percentiles_for_emit_rows(db, price_list_ids=[row.id], price_format_code="888")

    assignment = db.execute(
        select(PriceFormatCompetitorAssignment)
        .where(PriceFormatCompetitorAssignment.price_format_id == pf.id)
        .where(PriceFormatCompetitorAssignment.competitor_price_list_id == row.id)
    ).scalars().one()
    assert assignment.is_active is True
    assert result["assigned_price_format_ids"] == [pf.id]
    assert result["warnings"] == []
    assert result["summaries"]["888"]["products_with_competitors"] == 1


def test_emit_percentile_rebuild_without_assignment_propagates_to_all_formats(tmp_path):
    db = _session()
    pf = PriceFormat(code="003", name="Format 003")
    product = Product(code="SKU1", name="Product", cost=100, provisor_goods_id=1)
    db.add_all([pf, product])
    db.flush()
    staging = tmp_path / "stage.sqlite"
    source = tmp_path / "emit.ndjson"
    source.write_text(
        json.dumps({"id": 1, "goodsId": 1, "distributorGoodsName": "A", "distributorGoodsId": "1", "goodsPrice": 1}) + "\n",
        encoding="utf-8",
    )
    stats = parse_normalize_stage(
        source_path=source,
        stage_db_path=staging,
        filial_id=1108,
        filial_name="Emit International 1108",
        config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, min_final_rows=1),
    )
    row = replace_emit_price_list_from_staging(
        db=db,
        config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, batch_insert_size=10, min_final_rows=1),
        filial_id=1108,
        filial_name="Emit International 1108",
        staging_path=staging,
        stats=stats,
    )

    result = _recalculate_percentiles_for_emit_rows(db, price_list_ids=[row.id])

    assignment = db.execute(
        select(PriceFormatCompetitorAssignment)
        .where(PriceFormatCompetitorAssignment.price_format_id == pf.id)
        .where(PriceFormatCompetitorAssignment.competitor_price_list_id == row.id)
    ).scalars().one()
    assert assignment.is_active is True
    assert assignment.percentile_mode == "multi_price_per_sku"
    assert result["assigned_price_format_ids"] == [pf.id]
    assert result["warnings"] == []
    assert result["summaries"]["003"]["products_with_competitors"] == 1
    sources = list_percentile_sources(db=db, price_format_code="003")
    assert {source["region"] for source in sources if source["scope"] == "regional"} == {"Emit International 1108"}


def test_scheduled_emit_percentile_rebuild_scopes_each_refreshed_region():
    db = _session()
    pf = PriceFormat(code="FMT", name="Format")
    product = Product(code="SKU1", name="Product", cost=100, provisor_goods_id=1)
    db.add_all([pf, product])
    db.flush()
    almaty = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="emit:1108",
        display_name="Emit International 1108",
        supplier="Emit International 1108",
        branch_id="1108",
        branch_code="1108",
        branch_name="Emit International 1108",
        competitor_name="Emit International 1108",
        external_price_list_id="1108",
        account_login="emit",
    )
    astana = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="emit:1107",
        display_name="Emit International 1107",
        supplier="Emit International 1107",
        branch_id="1107",
        branch_code="1107",
        branch_name="Emit International 1107",
        competitor_name="Emit International 1107",
        external_price_list_id="1107",
        account_login="emit",
    )
    db.add_all([almaty, astana])
    db.flush()
    db.add_all(
        [
            PriceFormatCompetitorAssignment(price_format_id=pf.id, competitor_price_list_id=almaty.id, is_active=True),
            PriceFormatCompetitorAssignment(price_format_id=pf.id, competitor_price_list_id=astana.id, is_active=True),
            CompetitorPriceListItem(price_list_id=almaty.id, provisor_goods_id=1, filial_id=1108, name="A", distributor_price=100),
            CompetitorPriceListItem(price_list_id=almaty.id, provisor_goods_id=1, filial_id=1108, name="A", distributor_price=120),
            CompetitorPriceListItem(price_list_id=astana.id, provisor_goods_id=1, filial_id=1107, name="A", distributor_price=200),
            CompetitorPriceListItem(price_list_id=astana.id, provisor_goods_id=1, filial_id=1107, name="A", distributor_price=240),
        ]
    )
    db.commit()

    first = _recalculate_percentiles_for_emit_rows(
        db,
        price_list_ids=[almaty.id],
        scope_to_price_list_ids=True,
    )

    assert first["assigned_price_format_ids"] == [pf.id]
    rows_after_first = db.query(CompetitorPricePercentile).filter(
        CompetitorPricePercentile.price_format_id == pf.id,
        CompetitorPricePercentile.percentile_scope == "regional",
    ).all()
    assert {row.branch_name for row in rows_after_first} == {"Emit International 1108"}

    second = _recalculate_percentiles_for_emit_rows(
        db,
        price_list_ids=[astana.id],
        scope_to_price_list_ids=True,
    )

    assert second["assigned_price_format_ids"] == [pf.id]
    rows = db.query(CompetitorPricePercentile).filter(
        CompetitorPricePercentile.price_format_id == pf.id,
        CompetitorPricePercentile.product_id == product.id,
        CompetitorPricePercentile.percentile_scope == "regional",
        CompetitorPricePercentile.percentile == 10,
    ).all()
    by_branch = {row.branch_name: float(row.value) for row in rows}
    assert round(by_branch["Emit International 1108"], 3) == 102.0
    assert round(by_branch["Emit International 1107"], 3) == 204.0


def test_emit_global_propagation_assigns_new_region_to_all_existing_formats():
    db = _session()
    pf003 = PriceFormat(code="003", name="Format 003")
    pf004 = PriceFormat(code="004", name="Format 004")
    product = Product(code="SKU1", name="Product", cost=100, provisor_goods_id=1)
    db.add_all([pf003, pf004, product])
    db.flush()
    emit_8371 = CompetitorPriceList(
        price_format_id=pf003.id,
        source_type="provisor",
        source_key="emit:8371",
        display_name="Emit International 8371",
        supplier="Emit International 8371",
        branch_id="8371",
        branch_code="8371",
        branch_name="Emit International 8371",
        competitor_name="Emit International 8371",
        external_price_list_id="8371",
        account_login="emit",
        last_refresh_status="success",
    )
    db.add(emit_8371)
    db.flush()
    db.add_all(
        [
            CompetitorPriceListItem(price_list_id=emit_8371.id, provisor_goods_id=1, filial_id=8371, name="A", distributor_price=300),
            CompetitorPriceListItem(price_list_id=emit_8371.id, provisor_goods_id=1, filial_id=8371, name="A", distributor_price=330),
        ]
    )
    db.commit()

    first = _recalculate_percentiles_for_emit_rows(db, price_list_ids=[emit_8371.id], scope_to_price_list_ids=True)
    second = propagate_emit_assignments_to_price_formats(db=db, emit_price_list_ids=[emit_8371.id])
    db.commit()

    assert set(first["assigned_price_format_ids"]) == {pf003.id, pf004.id}
    assert first["assignment_propagation"]["created_count"] == 2
    assert second.created_count == 0
    assert second.reused_count == 2
    assert db.scalar(
        select(func.count(PriceFormatCompetitorAssignment.id))
        .where(PriceFormatCompetitorAssignment.competitor_price_list_id == emit_8371.id)
    ) == 2
    for pf in (pf003, pf004):
        rows = db.query(CompetitorPricePercentile).filter(
            CompetitorPricePercentile.price_format_id == pf.id,
            CompetitorPricePercentile.branch_name == "Emit International 8371",
            CompetitorPricePercentile.percentile_scope == "regional",
        ).all()
        assert rows


def test_emit_global_propagation_reuses_reactivates_and_skips_incompatible():
    db = _session()
    pf_reuse = PriceFormat(code="REUSE", name="Reuse")
    pf_reactivate = PriceFormat(code="REACT", name="Reactivate")
    pf_skip = PriceFormat(code="SKIP", name="Skip")
    db.add_all([pf_reuse, pf_reactivate, pf_skip])
    db.flush()
    emit_row = CompetitorPriceList(
        price_format_id=pf_reuse.id,
        source_type="provisor",
        source_key="emit:1108",
        display_name="Emit International 1108",
        supplier="Emit International 1108",
        branch_id="1108",
        branch_name="Emit International 1108",
        competitor_name="Emit International 1108",
        account_login="emit",
        last_refresh_status="success",
    )
    db.add(emit_row)
    db.flush()
    reuse = PriceFormatCompetitorAssignment(
        price_format_id=pf_reuse.id,
        competitor_price_list_id=emit_row.id,
        is_active=True,
        percentile_mode="multi_price_per_sku",
    )
    reactivate = PriceFormatCompetitorAssignment(
        price_format_id=pf_reactivate.id,
        competitor_price_list_id=emit_row.id,
        is_active=False,
        percentile_mode="",
    )
    skip = PriceFormatCompetitorAssignment(
        price_format_id=pf_skip.id,
        competitor_price_list_id=emit_row.id,
        is_active=False,
        percentile_mode="single_latest",
    )
    db.add_all([reuse, reactivate, skip])
    db.commit()

    result = propagate_emit_assignments_to_price_formats(db=db, emit_price_list_ids=[emit_row.id])
    db.commit()

    assert result.created_count == 0
    assert result.reused_assignment_ids == [reuse.id]
    assert result.reactivated_assignment_ids == [reactivate.id]
    assert result.skipped_incompatible_assignment_ids == [skip.id]
    db.refresh(reactivate)
    db.refresh(skip)
    assert reactivate.is_active is True
    assert reactivate.percentile_mode == "multi_price_per_sku"
    assert skip.is_active is False
    assert skip.percentile_mode == "single_latest"


def test_new_price_format_gets_active_emit_assignments_for_scheduler():
    db = _session()
    existing_pf = PriceFormat(code="OLD", name="Old")
    new_pf = PriceFormat(code="NEW", name="New")
    product = Product(code="SKU1", name="Product", cost=100, provisor_goods_id=1)
    db.add_all([existing_pf, new_pf, product])
    db.flush()
    emit_1108 = CompetitorPriceList(
        price_format_id=existing_pf.id,
        source_type="provisor",
        source_key="emit:1108",
        display_name="Emit International 1108",
        supplier="Emit International 1108",
        branch_id="1108",
        branch_code="1108",
        branch_name="Emit International 1108",
        competitor_name="Emit International 1108",
        external_price_list_id="1108",
        account_login="emit",
        last_refresh_status="success",
    )
    emit_1107 = CompetitorPriceList(
        price_format_id=existing_pf.id,
        source_type="provisor",
        source_key="emit:1107",
        display_name="Emit International 1107",
        supplier="Emit International 1107",
        branch_id="1107",
        branch_code="1107",
        branch_name="Emit International 1107",
        competitor_name="Emit International 1107",
        external_price_list_id="1107",
        account_login="emit",
        last_refresh_status="success",
    )
    non_emit = CompetitorPriceList(
        price_format_id=existing_pf.id,
        source_type="phcenter",
        source_key="phcenter:1",
        display_name="Regular competitor",
        supplier="Regular competitor",
        branch_name="Regular branch",
        competitor_name="Regular competitor",
        last_refresh_status="success",
    )
    db.add_all([emit_1108, emit_1107, non_emit])
    db.flush()
    db.add_all(
        [
            CompetitorPriceListItem(price_list_id=emit_1108.id, provisor_goods_id=1, filial_id=1108, name="A", distributor_price=100),
            CompetitorPriceListItem(price_list_id=emit_1108.id, provisor_goods_id=1, filial_id=1108, name="A", distributor_price=120),
            CompetitorPriceListItem(price_list_id=emit_1107.id, provisor_goods_id=1, filial_id=1107, name="A", distributor_price=200),
            CompetitorPriceListItem(price_list_id=emit_1107.id, provisor_goods_id=1, filial_id=1107, name="A", distributor_price=240),
            CompetitorPriceListItem(price_list_id=non_emit.id, provisor_goods_id=1, name="A", distributor_price=50),
            PriceFormatCompetitorAssignment(
                price_format_id=existing_pf.id,
                competitor_price_list_id=emit_1108.id,
                is_active=True,
                coefficient=1.25,
                percentile_mode="multi_price_per_sku",
                source_mode="regional-template",
            ),
        ]
    )
    db.commit()

    created = propagate_emit_assignments_to_new_price_format(db=db, price_format_id=int(new_pf.id))
    created_again = propagate_emit_assignments_to_new_price_format(db=db, price_format_id=int(new_pf.id))

    assert created == 2
    assert created_again == 0
    assignments = db.execute(
        select(PriceFormatCompetitorAssignment)
        .where(PriceFormatCompetitorAssignment.price_format_id == new_pf.id)
        .order_by(PriceFormatCompetitorAssignment.competitor_price_list_id.asc())
    ).scalars().all()
    assigned_ids = {int(row.competitor_price_list_id) for row in assignments}
    assert assigned_ids == {int(emit_1108.id), int(emit_1107.id)}
    assert db.scalar(
        select(func.count(PriceFormatCompetitorAssignment.id))
        .where(PriceFormatCompetitorAssignment.price_format_id == new_pf.id)
        .where(PriceFormatCompetitorAssignment.competitor_price_list_id == non_emit.id)
    ) == 0
    copied = next(row for row in assignments if int(row.competitor_price_list_id) == int(emit_1108.id))
    assert copied.is_active is True
    assert float(copied.coefficient) == 1.25
    assert copied.percentile_mode == "multi_price_per_sku"
    assert copied.source_mode == "regional-template"

    result = _recalculate_percentiles_for_emit_rows(
        db,
        price_list_ids=[emit_1107.id],
        scope_to_price_list_ids=True,
    )

    assert int(new_pf.id) in result["assigned_price_format_ids"]
    assert result["summaries"]["NEW"]["products_with_competitors"] == 1
    rows = db.query(CompetitorPricePercentile).filter(
        CompetitorPricePercentile.price_format_id == new_pf.id,
        CompetitorPricePercentile.percentile_scope == "regional",
        CompetitorPricePercentile.percentile == 10,
    ).all()
    assert {row.branch_name for row in rows} == {"Emit International 1107"}


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


def test_global_refresh_lock_blocks_emit_job_start():
    Session = _session_factory_static()
    with Session() as db:
        token = refresh_svc.new_owner_token()
        assert refresh_svc.try_acquire_global_refresh_lock(db, owner_token=token, source="provisor", requested_by="test") is True

    worker = EmitWorker(session_factory=Session, config=EmitConfig(temp_dir="unused"))
    job, blocker, owner_token = worker.create_job(mode="selected", filial_ids=[1106])

    assert job is None
    assert blocker is None
    assert owner_token is None


def test_emit_run_job_mixed_filials_finishes_partial_success(monkeypatch):
    Session = _session_factory_static()
    worker = EmitWorker(session_factory=Session, config=EmitConfig(temp_dir="unused"))
    job, _blocker, token = worker.create_job(mode="selected", filial_ids=[1106, 1107])
    assert job is not None

    async def fake_refresh_filial(*, job_id, filial_id, owner_token=None):
        if filial_id == 1106:
            return {"ok": True, "filial_id": filial_id, "price_list_id": 0, "duration_sec": 0.1}
        return {"ok": False, "filial_id": filial_id, "error": "download failed", "duration_sec": 0.1}

    monkeypatch.setattr(worker, "refresh_filial", fake_refresh_filial)

    asyncio.run(worker.run_job(int(job.id), owner_token=token))

    with Session() as db:
        saved = db.get(RefreshJob, int(job.id))
        assert saved.status == "partial_success"
        metadata = json.loads(saved.metadata_json)
        assert metadata["success"] == 1
        assert metadata["failed"] == 1
        assert metadata["refreshed_filials"] == [1106]


def test_emit_run_job_no_success_finishes_error(monkeypatch):
    Session = _session_factory_static()
    worker = EmitWorker(session_factory=Session, config=EmitConfig(temp_dir="unused"))
    job, _blocker, token = worker.create_job(mode="selected", filial_ids=[1106])
    assert job is not None

    async def fake_refresh_filial(*, job_id, filial_id, owner_token=None):
        return {"ok": False, "filial_id": filial_id, "error": "download failed", "duration_sec": 0.1}

    monkeypatch.setattr(worker, "refresh_filial", fake_refresh_filial)

    asyncio.run(worker.run_job(int(job.id), owner_token=token))

    with Session() as db:
        saved = db.get(RefreshJob, int(job.id))
        assert saved.status == "error"
        metadata = json.loads(saved.metadata_json)
        assert metadata["success"] == 0
        assert metadata["failed"] == 1


def test_recent_emit_job_not_marked_stale():
    Session = _session_factory_static()
    with Session() as db:
        token = refresh_svc.new_owner_token()
        job = RefreshJob(
            source_type="emit",
            mode="selected",
            status="running",
            started_at=datetime.utcnow(),
            heartbeat_at=datetime.utcnow(),
            metadata_json=json.dumps({"owner_token": token}),
        )
        db.add(job)
        db.commit()

        recovered = mark_stale_emit_jobs(db, config=EmitConfig(stale_timeout_seconds=60))
        db.refresh(job)

        assert recovered == []
        assert job.status == "running"


def test_stale_emit_job_marked_and_locks_released(tmp_path):
    Session = _session_factory_static()
    stale_time = datetime.utcnow() - timedelta(seconds=120)
    with Session() as db:
        token = refresh_svc.new_owner_token()
        assert refresh_svc.try_acquire_global_refresh_lock(db, owner_token=token, source="emit", requested_by="test")
        assert refresh_svc.try_acquire_lock(
            db,
            name="emit_refresh",
            lock_type="refresh",
            owner_token=token,
            lease=refresh_svc.REFRESH_LOCK_LEASE,
        )
        job = RefreshJob(
            source_type="emit",
            mode="selected",
            status="running",
            started_at=stale_time,
            heartbeat_at=stale_time,
            metadata_json=json.dumps({"owner_token": token}),
        )
        db.add(job)
        db.commit()

        recovered = mark_stale_emit_jobs(db, config=EmitConfig(temp_dir=str(tmp_path), stale_timeout_seconds=60))
        db.refresh(job)

        assert [row.id for row in recovered] == [job.id]
        assert job.status == "stale"
        assert job.finished_at is not None
        metadata = json.loads(job.metadata_json)
        assert set(metadata["locks_released"]) == {"emit_refresh", "competitor_refresh_global"}
        next_token = refresh_svc.new_owner_token()
        assert refresh_svc.try_acquire_global_refresh_lock(db, owner_token=next_token, source="emit", requested_by="test")


def test_stale_global_lock_not_released_when_owner_differs(tmp_path):
    Session = _session_factory_static()
    stale_time = datetime.utcnow() - timedelta(seconds=120)
    with Session() as db:
        stale_token = refresh_svc.new_owner_token()
        live_token = refresh_svc.new_owner_token()
        assert refresh_svc.try_acquire_global_refresh_lock(db, owner_token=live_token, source="provisor", requested_by="test")
        job = RefreshJob(
            source_type="emit",
            mode="selected",
            status="running",
            started_at=stale_time,
            heartbeat_at=stale_time,
            metadata_json=json.dumps({"owner_token": stale_token}),
        )
        db.add(job)
        db.commit()

        mark_stale_emit_jobs(db, config=EmitConfig(temp_dir=str(tmp_path), stale_timeout_seconds=60))

        next_token = refresh_svc.new_owner_token()
        assert not refresh_svc.try_acquire_global_refresh_lock(db, owner_token=next_token, source="emit", requested_by="test")


def test_emit_start_succeeds_after_stale_recovery(tmp_path):
    Session = _session_factory_static()
    stale_time = datetime.utcnow() - timedelta(seconds=120)
    with Session() as db:
        token = refresh_svc.new_owner_token()
        assert refresh_svc.try_acquire_global_refresh_lock(db, owner_token=token, source="emit", requested_by="test")
        assert refresh_svc.try_acquire_lock(
            db,
            name="emit_refresh",
            lock_type="refresh",
            owner_token=token,
            lease=refresh_svc.REFRESH_LOCK_LEASE,
        )
        db.add(
            RefreshJob(
                source_type="emit",
                mode="selected",
                status="running",
                started_at=stale_time,
                heartbeat_at=stale_time,
                metadata_json=json.dumps({"owner_token": token}),
            )
        )
        db.commit()

    worker = EmitWorker(session_factory=Session, config=EmitConfig(temp_dir=str(tmp_path), stale_timeout_seconds=60))
    job, blocker, owner_token = worker.create_job(mode="selected", filial_ids=[1106])

    assert job is not None
    assert blocker is None
    assert owner_token


def test_stale_recovery_race_recheck_keeps_fresh_job(monkeypatch, tmp_path):
    Session = _session_factory_static()
    stale_time = datetime.utcnow() - timedelta(seconds=120)
    with Session() as db:
        job = RefreshJob(
            source_type="emit",
            mode="selected",
            status="running",
            started_at=stale_time,
            heartbeat_at=stale_time,
            metadata_json=json.dumps({"owner_token": refresh_svc.new_owner_token()}),
        )
        db.add(job)
        db.commit()
        job_id = job.id

        def make_fresh(_job_id):
            db.execute(
                RefreshJob.__table__.update()
                .where(RefreshJob.id == job_id)
                .values(heartbeat_at=datetime.utcnow())
            )
            db.commit()

        monkeypatch.setattr("backend.app.services.emit_worker._emit_stale_recovery_before_mark_hook", make_fresh)
        recovered = mark_stale_emit_jobs(db, config=EmitConfig(temp_dir=str(tmp_path), stale_timeout_seconds=60))
        saved = db.get(RefreshJob, job_id)

        assert recovered == []
        assert saved.status == "running"


def test_stale_recovery_cleans_known_temp_files(tmp_path):
    Session = _session_factory_static()
    stale_time = datetime.utcnow() - timedelta(seconds=120)
    temp_file = tmp_path / "emit_old.json"
    stage_file = tmp_path / "emit_stage_old.sqlite"
    temp_file.write_text("{}", encoding="utf-8")
    stage_file.write_text("db", encoding="utf-8")
    with Session() as db:
        job = RefreshJob(
            source_type="emit",
            mode="selected",
            status="running",
            started_at=stale_time,
            heartbeat_at=stale_time,
            metadata_json=json.dumps(
                {
                    "owner_token": refresh_svc.new_owner_token(),
                    "temp_file_path": str(temp_file),
                    "stage_db_path": str(stage_file),
                }
            ),
        )
        db.add(job)
        db.commit()

        mark_stale_emit_jobs(db, config=EmitConfig(temp_dir=str(tmp_path), stale_timeout_seconds=60))
        db.refresh(job)
        metadata = json.loads(job.metadata_json)

        assert not temp_file.exists()
        assert not stage_file.exists()
        assert metadata["temp_cleanup"]["files_deleted"] == 2


def test_stale_recovery_cleanup_failure_does_not_block(monkeypatch, tmp_path):
    Session = _session_factory_static()
    stale_time = datetime.utcnow() - timedelta(seconds=120)
    temp_file = tmp_path / "emit_old.json"
    temp_file.write_text("{}", encoding="utf-8")
    with Session() as db:
        token = refresh_svc.new_owner_token()
        assert refresh_svc.try_acquire_global_refresh_lock(db, owner_token=token, source="emit", requested_by="test")
        job = RefreshJob(
            source_type="emit",
            mode="selected",
            status="running",
            started_at=stale_time,
            heartbeat_at=stale_time,
            metadata_json=json.dumps({"owner_token": token, "temp_file_path": str(temp_file)}),
        )
        db.add(job)
        db.commit()

        def fail_unlink(self):
            raise OSError("cannot delete")

        monkeypatch.setattr("pathlib.Path.unlink", fail_unlink)
        mark_stale_emit_jobs(db, config=EmitConfig(temp_dir=str(tmp_path), stale_timeout_seconds=60))
        db.refresh(job)
        metadata = json.loads(job.metadata_json)

        assert job.status == "stale"
        assert "competitor_refresh_global" in metadata["locks_released"]
        assert metadata["temp_cleanup"]["files_failed"] == [str(temp_file)]


def test_manual_emit_returns_409_when_provisor_active(monkeypatch):
    import backend.app.main as main

    Session = _session_factory_static()
    with Session() as db:
        job, _, token = refresh_svc.try_create_refresh_job(db, mode="selected", requested_by="test")
        assert job is not None and token is not None
        refresh_svc.start_job(db, job, total_accounts=1, total_plk=1, metadata={}, owner_token=token)

    worker = EmitWorker(session_factory=Session, config=EmitConfig(temp_dir="unused"))
    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(main, "_emit_worker", worker)
    main.app.dependency_overrides[main.get_db] = lambda: Session()
    try:
        client = TestClient(main.app)
        response = client.post("/api/emit/refresh/run-now", json={"mode": "selected", "filialIds": [1106]})
        assert response.status_code == 409
        assert "Provisor refresh is currently running" in response.json()["detail"]
    finally:
        main.app.dependency_overrides.clear()


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


def test_refresh_filial_returns_primitive_price_list_id_after_session_close(monkeypatch, tmp_path):
    Session = _session_factory_static()
    with Session() as db:
        db.add(PriceFormat(code="FMT", name="Format"))
        job = RefreshJob(
            source_type="emit",
            mode="selected",
            status="pending",
            total_plk=1,
            metadata_json=json.dumps({"filial_ids": [1106], "price_format_code": "FMT"}),
        )
        db.add(job)
        db.commit()
        job_id = int(job.id)

    async def fake_download_emit_filial(**_kwargs):
        source = tmp_path / "emit_download.ndjson"
        source.write_text(
            json.dumps(
                {
                    "id": 1,
                    "goodsId": 163571,
                    "distributorGoodsName": "A",
                    "distributorGoodsId": "SKU",
                    "goodsPrice": 8688.26,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return source

    monkeypatch.setattr("backend.app.services.emit_worker.download_emit_filial", fake_download_emit_filial)
    monkeypatch.setattr("backend.app.services.emit_worker._delete_stage_files", lambda _path: None)
    worker = EmitWorker(
        session_factory=Session,
        config=EmitConfig(temp_dir=str(tmp_path), min_free_disk_gb=0, min_final_rows=1, delete_temp_after_success=False),
    )

    result = asyncio.run(worker.refresh_filial(job_id=job_id, filial_id=1106))

    assert result["ok"] is True
    assert isinstance(result["price_list_id"], int)
    with Session() as db:
        saved = db.get(CompetitorPriceList, result["price_list_id"])
        assert saved is not None
        assert saved.source_key == "emit:1106"
