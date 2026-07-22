from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import (
    CompetitorPrice,
    CompetitorPriceList,
    CompetitorPriceListItem,
    PriceFormat,
    PriceFormatCompetitorAssignment,
)
from backend.app.services.provisor_plk_backfill import normalize_provisor_plk_rows


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_provisor_plk_backfill_dry_run_does_not_mutate():
    db = _session()
    pf = PriceFormat(code="FMT", name="Format")
    db.add(pf)
    db.flush()
    first = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="3:128",
        external_price_list_id="128",
        account_id="3",
        last_success_at=datetime.utcnow() - timedelta(days=1),
    )
    second = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="4:128",
        external_price_list_id="128",
        account_id="4",
        last_success_at=datetime.utcnow(),
    )
    db.add_all([first, second])
    db.commit()

    report = normalize_provisor_plk_rows(db=db, apply=False)

    assert report["groups_changed"] == 1
    assert report["rows_merged"] == 1
    assert sorted(db.execute(select(CompetitorPriceList.source_key)).scalars().all()) == ["3:128", "4:128"]


def test_provisor_plk_backfill_merges_duplicates_and_is_idempotent():
    db = _session()
    pf = PriceFormat(code="FMT", name="Format")
    db.add(pf)
    db.flush()
    older = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="3:128",
        external_price_list_id="128",
        account_id="3",
        account_login="A",
        display_name="Amanat",
        last_success_at=datetime.utcnow() - timedelta(days=2),
        updated_at=datetime.utcnow() - timedelta(days=2),
    )
    latest = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="4:128",
        external_price_list_id="128",
        account_id="4",
        account_login="B",
        display_name="Amanat",
        last_success_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    unique = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="3:129",
        external_price_list_id="129",
        account_id="3",
        display_name="Same Name",
        last_success_at=datetime.utcnow(),
    )
    db.add_all([older, latest, unique])
    db.flush()
    db.add_all(
        [
            CompetitorPriceListItem(price_list_id=older.id, name="old", distributor_price=10),
            CompetitorPriceListItem(price_list_id=latest.id, name="latest", distributor_price=11),
            CompetitorPriceListItem(price_list_id=unique.id, name="unique", distributor_price=12),
            PriceFormatCompetitorAssignment(price_format_id=pf.id, competitor_price_list_id=older.id, is_active=True, coefficient=1.2),
            PriceFormatCompetitorAssignment(price_format_id=pf.id, competitor_price_list_id=latest.id, is_active=False, coefficient=1.0),
            CompetitorPrice(price_format_id=pf.id, source_name="provisor:3:128", source_price=10),
        ]
    )
    db.commit()

    report = normalize_provisor_plk_rows(db=db, apply=True)

    assert report["groups_changed"] == 2
    assert report["rows_merged"] == 1
    assert report["assignments_merged"] == 1
    assert report["items_deleted_from_noncanonical_snapshots"] == 1
    rows = db.execute(select(CompetitorPriceList).order_by(CompetitorPriceList.external_price_list_id.asc())).scalars().all()
    assert [(row.external_price_list_id, row.source_key) for row in rows] == [("128", "plk:128"), ("129", "plk:129")]
    canonical = rows[0]
    assert canonical.id == latest.id
    assert "aliasesJson" in canonical.region
    assert [item.name for item in db.execute(select(CompetitorPriceListItem).where(CompetitorPriceListItem.price_list_id == canonical.id)).scalars()] == ["latest"]
    assignment = db.execute(select(PriceFormatCompetitorAssignment)).scalar_one()
    assert assignment.competitor_price_list_id == canonical.id
    assert assignment.is_active is True
    assert float(assignment.coefficient) == 1.2
    assert db.execute(select(CompetitorPrice.source_name)).scalar_one() == "provisor:plk:128"

    second_report = normalize_provisor_plk_rows(db=db, apply=True)

    assert second_report["groups_changed"] == 0
    assert db.execute(select(CompetitorPriceList)).scalars().all()
