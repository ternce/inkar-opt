from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import PriceFormat, VidmanLogicalCompetitorSource
from backend.app.services.vidman_logical_competitors import (
    assign_source_to_logical_competitor,
    list_logical_competitors,
    selected_logical_sources,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_format(db):
    pf = PriceFormat(code="004", name="Aktau", branch="Aktau")
    db.add(pf)
    db.flush()
    return pf


def test_one_logical_competitor_can_have_primary_and_fallback():
    db = _session()
    _seed_format(db)

    assign_source_to_logical_competitor(db=db, logical_name="Inkar", account_id=2, main_id=11870, role="PRIMARY", region="Aktau", price_format_code="004", apply=True)
    assign_source_to_logical_competitor(db=db, logical_name="Inkar", account_id=1, main_id=9352, role="FALLBACK", region="Aktau", price_format_code="004", apply=True)
    db.commit()

    [logical] = list_logical_competitors(db=db)
    assert [row["role"] for row in logical["sources"]] == ["PRIMARY", "FALLBACK"]


def test_only_one_active_primary_allowed():
    db = _session()
    _seed_format(db)
    assign_source_to_logical_competitor(db=db, logical_name="Inkar", account_id=2, main_id=11870, role="PRIMARY", region="Aktau", price_format_code="004", apply=True)

    with pytest.raises(ValueError, match="active PRIMARY"):
        assign_source_to_logical_competitor(db=db, logical_name="Inkar", account_id=1, main_id=9352, role="PRIMARY", region="Aktau", price_format_code="004", apply=True)


def test_primary_and_fallback_never_both_contribute():
    selected = selected_logical_sources(
        sources=[
            {"logical_competitor_id": 1, "account_id": 2, "main_id": 11870, "role": "PRIMARY", "priority": 0, "active": True},
            {"logical_competitor_id": 1, "account_id": 1, "main_id": 9352, "role": "FALLBACK", "priority": 10, "active": True},
        ],
        snapshot_ready={(2, 11870): True, (1, 9352): True},
    )

    assert selected[1]["account_id"] == 2
    assert len(selected) == 1


def test_fallback_selected_only_when_primary_unavailable():
    selected = selected_logical_sources(
        sources=[
            {"logical_competitor_id": 1, "account_id": 2, "main_id": 11870, "role": "PRIMARY", "priority": 0, "active": True},
            {"logical_competitor_id": 1, "account_id": 1, "main_id": 9352, "role": "FALLBACK", "priority": 10, "active": True},
        ],
        snapshot_ready={(2, 11870): False, (1, 9352): True},
    )

    assert selected[1]["account_id"] == 1


def test_same_source_cannot_belong_to_two_logical_competitors():
    db = _session()
    _seed_format(db)
    assign_source_to_logical_competitor(db=db, logical_name="Inkar", account_id=2, main_id=11870, role="PRIMARY", region="Aktau", price_format_code="004", apply=True)

    with pytest.raises(ValueError, match="another logical competitor"):
        assign_source_to_logical_competitor(db=db, logical_name="Other", account_id=2, main_id=11870, role="PRIMARY", region="Aktau", price_format_code="004", apply=True)


def test_missing_region_blocks_assignment():
    db = _session()
    _seed_format(db)

    with pytest.raises(ValueError, match="region"):
        assign_source_to_logical_competitor(db=db, logical_name="Inkar", account_id=2, main_id=11870, role="PRIMARY", price_format_code="004", apply=True)


def test_missing_format_blocks_assignment():
    db = _session()
    _seed_format(db)

    with pytest.raises(ValueError, match="price_format"):
        assign_source_to_logical_competitor(db=db, logical_name="Inkar", account_id=2, main_id=11870, role="PRIMARY", region="Aktau", apply=True)


def test_rerun_is_idempotent():
    db = _session()
    _seed_format(db)
    first = assign_source_to_logical_competitor(db=db, logical_name="Inkar", account_id=2, main_id=11870, role="PRIMARY", region="Aktau", price_format_code="004", apply=True)
    second = assign_source_to_logical_competitor(db=db, logical_name="Inkar", account_id=2, main_id=11870, role="PRIMARY", region="Aktau", price_format_code="004", apply=True)
    db.commit()

    assert first.source_id == second.source_id
    assert db.query(VidmanLogicalCompetitorSource).count() == 1


def test_dry_run_does_not_write():
    db = _session()
    _seed_format(db)

    result = assign_source_to_logical_competitor(db=db, logical_name="Inkar", account_id=2, main_id=11870, role="PRIMARY", region="Aktau", price_format_code="004", apply=False)

    assert result.dry_run is True
    assert db.query(VidmanLogicalCompetitorSource).count() == 0
