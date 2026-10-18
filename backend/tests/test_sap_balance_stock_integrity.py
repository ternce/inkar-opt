from __future__ import annotations

import json
import asyncio
import base64
import csv
import io
from datetime import date
from decimal import Decimal

import pytest
import httpx
from fastapi import HTTPException
from openpyxl import load_workbook
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.app.main as main
from backend.app.config import get_settings
from backend.app.db import Base
from backend.app.main import _stock_quantities_for_price_list, export_price_list_csv, export_price_list_xlsx
from backend.app.models import (
    BranchStock,
    CalculatedPrice,
    PriceFormat,
    PriceList,
    Product,
    ReferenceImportJob,
    ReferenceUpdateStatus,
    RefreshJob,
    RefreshLock,
    SapBalanceScheduledRun,
    StockSnapshot,
)
from backend.app.services.references.sap_balance import (
    SAP_PLANTS,
    branch_id_for_plant,
    fetch_balance,
    parse_balance_response,
)
from backend.app.services.references.stock_snapshots import (
    CanonicalStockRow,
    import_sap_balance_payload,
    publish_stock_snapshot,
)
from backend.app.services.references.imports import import_reference_excel
from backend.app.services.references.stock_diagnostics import compare_stock_quantities
from backend.app.services.sku import normalize_sku
from backend.app.services.refresh_queue import (
    claim_next_queued_refresh_job,
    create_queued_provisor_auto_refresh_job,
    create_queued_sap_balance_job,
)


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _response(plant: str, items: list[dict]) -> dict:
    return {"MT_BalanceResponse": {"status": "SUCCESS", "rowCount": len(items), "items": items}}


def _item(material: str, quantity: str, *, plant: str = "1004", lgort: str = "1000", charge: str = "") -> dict:
    return {
        "material": material,
        "plant": plant,
        "lgort": lgort,
        "charge": charge,
        "endingStockQuantity": quantity,
        "endingStockValue": "999999.99",
    }


def _job(db, branch_id: str, source_type: str = "excel") -> ReferenceImportJob:
    row = ReferenceImportJob(
        data_type="stock",
        branch_ids_json=json.dumps([branch_id]),
        filename="fixture.xlsx",
        source_type=source_type,
        status="running",
    )
    db.add(row)
    db.flush()
    return row


def test_authoritative_mapping_has_all_12_explicit_plants(monkeypatch):
    monkeypatch.setenv("SAP_BALANCE_BRANCH_MAP_JSON", "{}")
    get_settings.cache_clear()
    settings = get_settings()
    try:
        assert [row.plant for row in SAP_PLANTS] == [
            "1001", "1002", "1003", "1004", "1005", "1006",
            "1007", "1008", "1009", "1010", "1011", "1013",
        ]
        assert {row.plant: branch_id_for_plant(settings, row.plant) for row in SAP_PLANTS} == {
            "1001": "1", "1002": "2", "1003": "5", "1004": "Есик",
            "1005": "6", "1006": "8", "1007": "Семей", "1008": "16",
            "1009": "10", "1010": "3", "1011": "4", "1013": "17",
        }
        with pytest.raises(ValueError, match="1012"):
            branch_id_for_plant(settings, "1012")
    finally:
        get_settings.cache_clear()


def test_sap_parser_preserves_leading_zeroes_and_decimal_quantity():
    payload = parse_balance_response(
        _response("1004", [_item("0000000001005213", "32.7500")]),
        requested_plant="1004",
        effective_date=date(2026, 10, 14),
    )
    assert payload.items[0].material == "0000000001005213"
    assert payload.items[0].quantity == Decimal("32.7500")


def test_fetch_balance_uses_basic_auth_and_only_requested_plant_lgort(monkeypatch):
    monkeypatch.setenv("SAP_BALANCE_ENABLED", "true")
    monkeypatch.setenv("SAP_BALANCE_USERNAME", "local-user")
    monkeypatch.setenv("SAP_BALANCE_PASSWORD", "local-pass")
    monkeypatch.setenv("SAP_BALANCE_COMPANY_CODE", "LOCAL")
    get_settings.cache_clear()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_response("1004", [_item("0000000001005213", "32")]))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_balance(
                get_settings(),
                plant="1004",
                date_from=date(2026, 10, 14),
                date_to=date(2026, 10, 14),
                client=client,
            )

    try:
        result = asyncio.run(run())
        assert result.plant == "1004"
        assert captured["body"]["plants"] == ["1004"]
        assert captured["body"]["lgort"] == ["1000"]
        assert captured["authorization"] == "Basic " + base64.b64encode(b"local-user:local-pass").decode()
    finally:
        get_settings.cache_clear()


def test_sap_queue_keeps_explicit_plants_and_effective_date():
    db = _session()
    job = create_queued_sap_balance_job(
        db,
        plants=["1001", "1013", "1001"],
        effective_date="2026-10-16",
        requested_by="phase5-test",
    )
    metadata = json.loads(job.metadata_json)
    assert job.source_type == "sap"
    assert job.mode == "stock"
    assert job.status == "queued"
    assert metadata["plants"] == ["1001", "1013"]
    assert metadata["effective_date"] == "2026-10-16"
    assert metadata["force_manual_override"] is False


def test_scheduled_sap_enqueue_is_durable_and_deduplicated_for_all_approved_branches(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(
        main,
        "settings",
        main.settings.model_copy(
            update={
                "sap_balance_enabled": True,
                "sap_balance_schedule_enabled": True,
                "sap_balance_branch_map_json": "{}",
            }
        ),
    )
    asyncio.run(main._queue_scheduled_sap_balance())
    asyncio.run(main._queue_scheduled_sap_balance())
    with Session() as db:
        jobs = db.scalars(select(RefreshJob).where(RefreshJob.source_type == "sap")).all()
        reservations = db.scalars(select(SapBalanceScheduledRun)).all()
        assert len(jobs) == 1
        assert len(reservations) == 12
        assert {row.plant for row in reservations} == {row.plant for row in SAP_PLANTS}
        assert {row.refresh_job_id for row in reservations} == {jobs[0].id}


def test_sap_worker_completion_releases_shared_mutex_for_provisor(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    material = "0000000001005213"
    with Session() as db:
        db.add(Product(code=normalize_sku(material), name="Esik worker fixture", cost=1))
        db.commit()
        queued = create_queued_sap_balance_job(
            db,
            plants=["1004"],
            effective_date="2026-10-15",
            requested_by="test",
        )

    async def fake_fetch_balance(_settings, *, plant, date_from, date_to):
        return parse_balance_response(
            _response(plant, [_item(material, "32", plant=plant)]),
            requested_plant=plant,
            effective_date=date_to,
        )

    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(main, "fetch_balance", fake_fetch_balance)
    monkeypatch.setattr(
        main,
        "settings",
        main.settings.model_copy(
            update={
                "sap_balance_enabled": True,
                "sap_manual_precedence_hours": 24,
            }
        ),
    )

    with Session() as db:
        claimed, owner_token = claim_next_queued_refresh_job(db, worker_id="worker-sap")
        assert claimed is not None and claimed.id == queued.id and owner_token
    asyncio.run(main._execute_claimed_refresh_job(int(queued.id), owner_token=owner_token))

    with Session() as db:
        completed = db.get(RefreshJob, queued.id)
        assert completed is not None and completed.status == "success"
        assert db.scalars(select(RefreshLock)).all() == []
        status = db.scalar(
            select(ReferenceUpdateStatus)
            .where(ReferenceUpdateStatus.branch_id == "Есик")
            .where(ReferenceUpdateStatus.data_type == "stock")
        )
        assert status is not None and status.active_source_type == "sap"
        provisor = create_queued_provisor_auto_refresh_job(db, mode="selected", requested_by="test")

    with Session() as db:
        next_job, next_token = claim_next_queued_refresh_job(db, worker_id="worker-provisor")
        assert next_job is not None and next_job.id == provisor.id and next_token


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"MT_BalanceResponse": {"status": "ERROR", "rowCount": 0, "items": []}}, "business error"),
        ({"MT_BalanceResponse": {"status": "SUCCESS", "rowCount": 2, "items": []}}, "incomplete"),
        (_response("1004", [_item("1", "1", plant="1005")]), "plant mismatch"),
        (_response("1004", [_item("1", "1", lgort="4000")]), "LGORT mismatch"),
    ],
)
def test_sap_parser_rejects_unsafe_responses(body, message):
    with pytest.raises(ValueError, match=message):
        parse_balance_response(body, requested_plant="1004", effective_date=date(2026, 10, 14))


def test_sap_parser_rejects_ambiguous_visible_duplicates():
    body = _response("1004", [_item("0001", "2"), _item("0001", "3")])
    with pytest.raises(ValueError, match="ambiguous duplicate"):
        parse_balance_response(body, requested_plant="1004", effective_date=date(2026, 10, 14))


def test_sap_missing_product_fails_without_replacing_active_snapshot():
    db = _session()
    product = Product(code="1005213".zfill(18), name="Known", cost=1)
    db.add(product)
    db.flush()
    manual = _job(db, "Есик")
    first = publish_stock_snapshot(
        db,
        branch_id="Есик",
        source_type="manual",
        effective_date=date(2026, 10, 14),
        rows=[CanonicalStockRow(product.code, product.code, product.id, Decimal("32"), {})],
        import_job=manual,
    )
    db.commit()
    payload = parse_balance_response(
        _response("1004", [_item("0000000009999999", "10")]),
        requested_plant="1004",
        effective_date=date(2026, 10, 15),
    )
    result = import_sap_balance_payload(
        db,
        branch_id="Есик",
        payload=payload,
        user_name="test",
        manual_precedence_hours=0,
    )
    assert result.status == "error"
    status = db.scalar(select(ReferenceUpdateStatus).where(ReferenceUpdateStatus.branch_id == "Есик"))
    assert status.active_stock_snapshot_id == first.id
    assert db.scalar(select(BranchStock.stock).where(BranchStock.branch_id == "Есик")) == Decimal("32.0000")


def test_manual_csv_uses_same_atomic_snapshot_publication_path():
    db = _session()
    result = import_reference_excel(
        db=db,
        data_type="stock",
        branch_ids=["Есик"],
        content="Материал;Остаток\n1005213;32,75\n".encode("utf-8"),
        filename="Остаток_Есик.csv",
        user_name="manual-test",
    )
    assert result.status == "success"
    status = db.scalar(select(ReferenceUpdateStatus).where(ReferenceUpdateStatus.branch_id == "Есик"))
    assert status.active_source_type == "manual"
    assert status.active_stock_snapshot_id is not None
    assert db.scalar(select(BranchStock.stock).where(BranchStock.branch_id == "Есик")) == Decimal("32.7500")


def test_newer_manual_snapshot_blocks_scheduled_sap_publication():
    db = _session()
    product = Product(code="0000000001005213", name="Known", cost=1)
    db.add(product)
    db.flush()
    manual = _job(db, "Есик")
    first = publish_stock_snapshot(
        db,
        branch_id="Есик",
        source_type="manual",
        effective_date=date(2026, 10, 14),
        rows=[CanonicalStockRow(product.code, product.code, product.id, Decimal("32"), {})],
        import_job=manual,
    )
    db.commit()
    payload = parse_balance_response(
        _response("1004", [_item(product.code, "6")]),
        requested_plant="1004",
        effective_date=date(2026, 10, 15),
    )
    result = import_sap_balance_payload(
        db,
        branch_id="Есик",
        payload=payload,
        user_name="scheduler",
        manual_precedence_hours=24,
    )
    assert result.status == "error"
    assert "manual_snapshot_precedence" in result.error
    assert db.get(StockSnapshot, first.id).status == "active"


def test_identical_sap_retry_reuses_active_snapshot_without_rewriting_lineage():
    db = _session()
    product = Product(code="1005213".zfill(18), name="Known", cost=1)
    db.add(product)
    db.commit()
    payload = parse_balance_response(
        _response("1004", [_item(product.code, "32.75")]),
        requested_plant="1004",
        effective_date=date(2026, 10, 15),
    )
    first_job = import_sap_balance_payload(
        db, branch_id="Есик", payload=payload, user_name="worker", manual_precedence_hours=24
    )
    first_status = db.scalar(select(ReferenceUpdateStatus).where(ReferenceUpdateStatus.branch_id == "Есик"))
    first_snapshot_id = first_status.active_stock_snapshot_id
    second_job = import_sap_balance_payload(
        db, branch_id="Есик", payload=payload, user_name="retry", manual_precedence_hours=24
    )
    second_status = db.scalar(select(ReferenceUpdateStatus).where(ReferenceUpdateStatus.branch_id == "Есик"))
    assert first_job.status == second_job.status == "success", (first_job.error, second_job.error)
    assert second_status.active_stock_snapshot_id == first_snapshot_id
    assert db.scalar(select(func.count(StockSnapshot.id))) == 1


def test_generated_price_list_keeps_exact_historical_esik_snapshot_after_republish():
    db = _session()
    examples = {
        "1005213": (Decimal("32"), Decimal("6")),
        "1017769": (Decimal("3"), Decimal("1000")),
        "1003454": (Decimal("140"), Decimal("1473")),
        "1003450": (Decimal("4"), Decimal("27")),
        "1005967": (Decimal("31"), Decimal("83")),
        "1005754": (Decimal("18"), Decimal("16")),
    }
    products = []
    for sku in examples:
        product = Product(code=sku.zfill(18), name=sku, cost=1)
        db.add(product)
        products.append(product)
    db.flush()
    first_job = _job(db, "Есик")
    first = publish_stock_snapshot(
        db,
        branch_id="Есик",
        source_type="manual",
        effective_date=date(2026, 10, 14),
        rows=[
            CanonicalStockRow(product.code, product.code, product.id, examples[product.name][0], {})
            for product in products
        ],
        import_job=first_job,
    )
    pf = PriceFormat(code="GPL_1004_001", name="Esik", branch="Есик", reference_branch_id="Есик")
    db.add(pf)
    db.flush()
    price_list = PriceList(
        number="ГПЛ_1004_001_Есик_2026-10-15_wf1850",
        price_format_id=pf.id,
        run_reference_versions_json=json.dumps({"generationReconciliation": {"stock_snapshot_id": first.id}}),
    )
    db.add(price_list)
    db.flush()
    db.add_all(
        [
            CalculatedPrice(
                price_list_id=price_list.id,
                product_id=product.id,
                cost=Decimal("1"),
                base_price=Decimal("1"),
                final_price=Decimal("1"),
            )
            for product in products
        ]
    )
    db.commit()

    second_job = _job(db, "Есик", "sap")
    publish_stock_snapshot(
        db,
        branch_id="Есик",
        source_type="sap",
        effective_date=date(2026, 10, 15),
        rows=[
            CanonicalStockRow(product.code, product.code, product.id, examples[product.name][1], {})
            for product in products
        ],
        import_job=second_job,
    )
    db.commit()

    historical = _stock_quantities_for_price_list(
        db,
        pl=price_list,
        pf=pf,
        product_ids=[product.id for product in products],
    )
    assert {product.name: historical[product.id] for product in products} == {
        sku: expected for sku, (expected, _wrong) in examples.items()
    }
    assert compare_stock_quantities(
        {sku.zfill(18): expected for sku, (expected, _wrong) in examples.items()},
        {sku.zfill(18): wrong for sku, (_expected, wrong) in examples.items()},
    )["mismatchCount"] == 6
    assert compare_stock_quantities(
        {sku.zfill(18): expected for sku, (expected, _wrong) in examples.items()},
        {product.code: historical[product.id] for product in products},
    )["mismatchCount"] == 0

    response = export_price_list_xlsx(
        price_list.number,
        search=None,
        top_filter="all",
        top_sort="default",
        db=db,
    )

    async def response_bytes() -> bytes:
        return b"".join([chunk async for chunk in response.body_iterator])

    workbook = load_workbook(io.BytesIO(asyncio.run(response_bytes())), data_only=True)
    exported = {
        str(row[1].value): Decimal(str(row[3].value))
        for row in workbook.active.iter_rows(min_row=2)
    }
    workbook.close()
    assert exported == {
        sku.zfill(18): expected for sku, (expected, _wrong) in examples.items()
    }

    csv_response = export_price_list_csv(
        price_list.number,
        search=None,
        top_filter="all",
        top_sort="default",
        db=db,
    )

    async def response_text() -> str:
        chunks = [chunk async for chunk in csv_response.body_iterator]
        return "".join(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in chunks)

    csv_rows = list(csv.reader(io.StringIO(asyncio.run(response_text()))))
    csv_exported = {row[1]: Decimal(row[3]) for row in csv_rows[1:]}
    assert csv_exported == {
        sku.zfill(18): expected for sku, (expected, _wrong) in examples.items()
    }

    other_branch_job = _job(db, "1", "manual")
    other_branch_snapshot = publish_stock_snapshot(
        db,
        branch_id="1",
        source_type="manual",
        effective_date=date(2026, 10, 15),
        rows=[
            CanonicalStockRow(product.code, product.code, product.id, Decimal("999"), {})
            for product in products
        ],
        import_job=other_branch_job,
    )
    price_list.run_reference_versions_json = json.dumps(
        {"generationReconciliation": {"stock_snapshot_id": other_branch_snapshot.id}}
    )
    db.commit()
    with pytest.raises(HTTPException, match="does not match generated price-list branch") as exc_info:
        _stock_quantities_for_price_list(
            db,
            pl=price_list,
            pf=pf,
            product_ids=[product.id for product in products],
        )
    assert exc_info.value.status_code == 409
