from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import (
    CompetitorPriceList,
    CompetitorPriceListItem,
    CompetitorPricePercentile,
    Job,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    PriceFormatPercentilePreparation,
    Product,
)
from backend.app.services.competitor_source_config import MULTI_PRICE_PERCENTILE_MODE
from backend.app.services.percentile_preparation import (
    JOB_TYPE,
    enqueue_percentile_preparation,
    ensure_percentile_ready_for_generation,
    percentile_preparation_to_dict,
    retry_waiting_percentile_preparations,
    run_percentile_preparation_job,
)


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _format(db, *, code: str, mode: str = "percentile"):
    row = PriceFormat(code=code, name=code, branch="Astana", competitor_price_mode=mode)
    db.add(row)
    db.flush()
    return row


def _product(db, *, code: str = "SKU-1"):
    row = Product(code=code, name=code, cost=Decimal("10"))
    db.add(row)
    db.flush()
    return row


def _price_list(db, pf, *, source_key: str = "emit:kz:alpha", competitor: str = "Alpha"):
    row = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="emit",
        source_key=source_key,
        display_name=competitor,
        supplier=competitor,
        branch_name="Astana",
        competitor_name=competitor,
        sync_batch_id="batch-1",
    )
    db.add(row)
    db.flush()
    return row


def _assign(db, pf, price_list):
    row = PriceFormatCompetitorAssignment(
        price_format_id=pf.id,
        competitor_price_list_id=price_list.id,
        is_active=True,
        percentile_mode=MULTI_PRICE_PERCENTILE_MODE,
    )
    db.add(row)
    db.flush()
    return row


def _raw_price(db, price_list, product, price: str):
    db.add(
        CompetitorPriceListItem(
            price_list_id=price_list.id,
            product_id=product.id,
            matched_sku=product.code,
            distributor_goods_id=product.code,
            distributor_price=Decimal(price),
            name=product.name,
            distributor_goods_name=product.name,
        )
    )


def _configured_format(db, *, code: str = "PF", prices: tuple[str, ...] = ("100", "120", "140")):
    pf = _format(db, code=code)
    product = _product(db)
    price_list = _price_list(db, pf)
    _assign(db, pf, price_list)
    for price in prices:
        _raw_price(db, price_list, product, price)
    db.commit()
    return pf, product, price_list


def test_selecting_configured_format_queues_and_builds_percentile_rows(monkeypatch):
    Session = _session_factory()
    monkeypatch.setattr("backend.app.services.percentile_preparation.SessionLocal", Session)
    with Session() as db:
        pf, _, _ = _configured_format(db)
        status = enqueue_percentile_preparation(db=db, price_format_id=int(pf.id), start_worker=False)
        assert status["status"] == "pending"
        job = db.execute(select(Job).where(Job.type == JOB_TYPE, Job.price_format_id == pf.id)).scalars().one()

    run_percentile_preparation_job(job.id)

    with Session() as db:
        status = percentile_preparation_to_dict(db, int(pf.id))
        assert status["status"] == "ready"
        assert status["rowsCount"] > 0


def test_rule_without_raw_percentile_data_displays_failed_empty_state():
    Session = _session_factory()
    with Session() as db:
        pf = _format(db, code="NO-RAW")
        price_list = _price_list(db, pf)
        _assign(db, pf, price_list)
        db.commit()

        status = enqueue_percentile_preparation(db=db, price_format_id=int(pf.id), start_worker=False)

        assert status["status"] == "failed"
        assert "unavailable" in status["lastError"]
        assert status["rowsCount"] == 0


def test_unconfigured_format_has_not_configured_state():
    Session = _session_factory()
    with Session() as db:
        pf = _format(db, code="EMPTY")
        db.commit()

        status = enqueue_percentile_preparation(db=db, price_format_id=int(pf.id), start_worker=False)

        assert status["status"] == "not_configured"
        assert "ещё не заданы" in status["message"]


def test_failed_preparation_preserves_old_percentile_rows(monkeypatch):
    Session = _session_factory()
    monkeypatch.setattr("backend.app.services.percentile_preparation.SessionLocal", Session)
    with Session() as db:
        pf, product, price_list = _configured_format(db, code="ATOMIC")
        db.add(
            CompetitorPricePercentile(
                price_format_id=pf.id,
                product_id=product.id,
                competitor_price_list_id=price_list.id,
                source_type="emit",
                source_key=price_list.source_key,
                branch_name="Astana",
                competitor_name="Alpha",
                percentile_scope="regional",
                percentile=10,
                value=Decimal("77.00"),
                source_count=1,
                price_count=1,
                used_price_count=1,
                status="Calculated",
            )
        )
        db.commit()
        enqueue_percentile_preparation(db=db, price_format_id=int(pf.id), start_worker=False)
        job = db.execute(select(Job).where(Job.type == JOB_TYPE, Job.price_format_id == pf.id)).scalars().one()

    def fail_after_delete(*, db, price_format_id, source_price_list_ids=None):
        db.query(CompetitorPricePercentile).filter(CompetitorPricePercentile.price_format_id == price_format_id).delete()
        raise RuntimeError("boom")

    monkeypatch.setattr("backend.app.services.percentile_preparation.recalculate_competitor_percentiles", fail_after_delete)
    run_percentile_preparation_job(job.id)

    with Session() as db:
        status = percentile_preparation_to_dict(db, int(pf.id))
        values = db.execute(select(CompetitorPricePercentile.value).where(CompetitorPricePercentile.price_format_id == pf.id)).scalars().all()
        assert status["status"] == "failed"
        assert values == [Decimal("77.00")]


def test_stale_running_job_queues_replacement(monkeypatch):
    Session = _session_factory()
    monkeypatch.setattr("backend.app.services.percentile_preparation.SessionLocal", Session)
    monkeypatch.setattr("backend.app.services.percentile_preparation.start_percentile_preparation_worker", lambda job_id: None)
    with Session() as db:
        pf, _, price_list = _configured_format(db, code="STALE")
        enqueue_percentile_preparation(db=db, price_format_id=int(pf.id), start_worker=False)
        job = db.execute(select(Job).where(Job.type == JOB_TYPE, Job.price_format_id == pf.id)).scalars().one()
        pf_id = int(pf.id)
        job_id = job.id
        price_list.sync_batch_id = "batch-2"
        db.commit()

    run_percentile_preparation_job(job_id)

    with Session() as db:
        jobs = db.execute(select(Job).where(Job.type == JOB_TYPE, Job.price_format_id == pf_id).order_by(Job.created_at.asc())).scalars().all()
        assert jobs[0].status == "error"
        assert jobs[-1].status in {"pending", "running", "success"}


def test_retry_waiting_percentile_preparations_requeues_failed_format():
    Session = _session_factory()
    with Session() as db:
        pf, _, _ = _configured_format(db, code="RETRY")
        status = enqueue_percentile_preparation(db=db, price_format_id=int(pf.id), start_worker=False)
        job = db.execute(select(Job).where(Job.id == status["jobId"])).scalars().one()
        job.status = "error"
        prep = db.get(PriceFormatPercentilePreparation, pf.id)
        prep.status = "failed"
        db.commit()

        count = retry_waiting_percentile_preparations(db=db, start_worker=False)

        assert count == 1
        assert percentile_preparation_to_dict(db, int(pf.id))["status"] == "pending"


def test_generation_blocks_percentile_and_mixed_until_ready():
    Session = _session_factory()
    with Session() as db:
        pf, _, _ = _configured_format(db, code="BLOCK", prices=("100",))
        with pytest.raises(ValueError, match="not ready"):
            ensure_percentile_ready_for_generation(db, pf)

        pf.competitor_price_mode = "regular"
        ensure_percentile_ready_for_generation(db, pf)

        pf.competitor_price_mode = "mixed"
        with pytest.raises(ValueError, match="not ready"):
            ensure_percentile_ready_for_generation(db, pf)
