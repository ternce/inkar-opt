from __future__ import annotations

from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import CompetitorPriceList, CompetitorPriceListItem, PriceFormat
import backend.app.services.vidman_rollout_audit as audit_module
from backend.app.services.vidman_rollout_audit import (
    SourceMetrics,
    _readiness,
    build_rollout_audit,
    classify_duplicate_candidate,
    duplicate_candidates,
)


def _source(
    account_id: int,
    main_id: int,
    *,
    name: str = "Inkar Aktau",
    region: str = "Aktau",
    format_code: str = "004",
    competitor: str = "Inkar Aktau",
    prices: dict[int, str] | None = None,
    published: bool = False,
) -> SourceMetrics:
    product_prices = {pid: Decimal(price) for pid, price in (prices or {1: "10", 2: "20"}).items()}
    return SourceMetrics(
        account_id=account_id,
        account_login=f"a{account_id}",
        account_name="",
        main_id=main_id,
        plk_name=name,
        total_raw_rows=len(product_prices),
        import_run_count=1,
        latest_run_id=1,
        latest_run_status="success",
        latest_successful_run_id=1,
        latest_successful_at="",
        latest_successful_rows=len(product_prices),
        snapshot_status="READY_SNAPSHOT",
        trusted_mapped_rows=len(product_prices),
        valid_price_rows=len(product_prices),
        unique_internal_products=len(product_prices),
        publishable_rows=len(product_prices),
        raw_to_publishable_percent=Decimal("100"),
        trusted_to_publishable_percent=Decimal("100"),
        region=region,
        format_code=format_code,
        logical_competitor=competitor,
        source_active=True,
        competitor_price_list_id=719 if published else None,
        current_stage4_status="PUBLISHED" if published else "NOT_PUBLISHED",
        coverage_band="HIGH_COVERAGE",
        readiness="READY_FOR_APPLY",
        readiness_reason="",
        product_prices=product_prices,
        trusted_products=set(product_prices),
    )


def test_same_plk_across_accounts_detected():
    [dup] = duplicate_candidates(
        [
            _source(1, 9373, name="Emiti Aktau", prices={1: "10", 2: "20", 3: "30"}),
            _source(2, 9167, name="Emiti Aktau", prices={1: "10", 2: "20", 3: "30"}),
        ]
    )

    assert dup.classification == "DEFINITE_SAME_PHYSICAL_PLK"
    assert dup.exact_price_percent == Decimal("100.00")


def test_same_name_different_product_composition_not_auto_same():
    assert (
        classify_duplicate_candidate(
            name_match=True,
            region_match=True,
            overlap_a=Decimal("10"),
            overlap_b=Decimal("10"),
            exact_price_percent=Decimal("100"),
            median_price_diff=Decimal("0"),
        )
        == "LIKELY_DIFFERENT"
    )


def test_different_names_high_overlap_surfaced_possible_duplicate():
    [dup] = duplicate_candidates(
        [
            _source(1, 10, name="Name A", prices={1: "10", 2: "20", 3: "30", 4: "40"}),
            _source(2, 20, name="Name B", prices={1: "10", 2: "20", 3: "30", 4: "41"}),
        ]
    )

    assert dup.classification in {"VERY_LIKELY_SAME", "POSSIBLE_DUPLICATE"}


def test_duplicate_physical_plks_recommend_one_primary():
    duplicates = duplicate_candidates(
        [
            _source(1, 1, prices={1: "10", 2: "20"}),
            _source(2, 2, prices={1: "10", 2: "20"}, published=True),
        ]
    )

    assert duplicates[0].recommended_primary == (2, 2)
    assert duplicates[0].recommended_fallback == (1, 1)


def test_missing_region_blocks_rollout_classification():
    assert _source(1, 1, region="").region == ""


def test_missing_format_blocks_rollout_classification():
    assert _source(1, 1, format_code="").format_code == ""


def test_failed_latest_previous_success_status_is_safe_snapshot_label():
    source = SourceMetrics(
        **{
            **_source(1, 1).__dict__,
            "latest_run_id": 2,
            "latest_run_status": "error",
            "latest_successful_run_id": 1,
            "snapshot_status": "FAILED_LATEST_BUT_PREVIOUS_SUCCESS_AVAILABLE",
        }
    )

    assert source.snapshot_status == "FAILED_LATEST_BUT_PREVIOUS_SUCCESS_AVAILABLE"


def test_no_successful_snapshot_blocks_rollout():
    source = SourceMetrics(**{**_source(1, 1).__dict__, "latest_successful_run_id": None, "snapshot_status": "NO_SUCCESSFUL_SNAPSHOT"})

    assert source.snapshot_status == "NO_SUCCESSFUL_SNAPSHOT"


def test_audit_performs_no_competitor_list_writes_for_empty_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    pf = PriceFormat(code="004", name="Aktau")
    db.add(pf)
    db.flush()
    before_lists = db.query(CompetitorPriceList).count()
    before_items = db.query(CompetitorPriceListItem).count()

    monkeypatch.setattr(audit_module, "_source_rows", lambda conn: [])
    monkeypatch.setattr(audit_module, "_mapping_rows", lambda conn: {})
    monkeypatch.setattr(audit_module, "_logical_mapping_rows", lambda conn: {})
    monkeypatch.setattr(audit_module, "_stage4_lists", lambda conn: {})
    monkeypatch.setattr(audit_module, "_existing_collisions", lambda conn, sources: [])
    monkeypatch.setattr(audit_module, "_published_11870", lambda conn: {})

    report = build_rollout_audit(db.connection())

    assert report.before_counts["competitor_price_lists"] == before_lists
    assert db.query(CompetitorPriceList).count() == before_lists
    assert db.query(CompetitorPriceListItem).count() == before_items


def test_existing_competitor_collision_fixture_is_preserved():
    source = _source(1, 1, competitor="Inkar Aktau")

    assert source.logical_competitor == "Inkar Aktau"


def test_published_11870_remains_untouched_fixture():
    source = _source(2, 11870, published=True)

    assert source.competitor_price_list_id == 719
    assert source.current_stage4_status == "PUBLISHED"


def test_unresolved_existing_competitor_collision_blocks_readiness():
    source = SourceMetrics(
        **{
            **_source(2, 11870, published=False).__dict__,
            "logical_competitor_id": 1,
            "logical_source_role": "PRIMARY",
            "logical_source_selected": True,
            "collision_status": "POSSIBLE_COLLISION",
        }
    )

    readiness, reason = _readiness(source, duplicate_hold=False, collision_hold=True)

    assert readiness == "READY_AFTER_LOGICAL_MAPPING"
    assert "collision" in reason


def test_manual_source_identity_is_preserved():
    source = _source(2, 11870)

    assert (source.account_id, source.main_id) == (2, 11870)


def test_9352_cannot_contribute_while_11870_primary_is_healthy():
    from backend.app.services.vidman_logical_competitors import selected_logical_sources

    selected = selected_logical_sources(
        sources=[
            {"logical_competitor_id": 1, "account_id": 2, "main_id": 11870, "role": "PRIMARY", "priority": 0, "active": True},
            {"logical_competitor_id": 1, "account_id": 1, "main_id": 9352, "role": "FALLBACK", "priority": 10, "active": True},
        ],
        snapshot_ready={(2, 11870): True, (1, 9352): True},
    )

    assert selected[1]["main_id"] == 11870
    assert all(row["main_id"] != 9352 for row in selected.values())


def test_logical_duplicate_count_in_percentile_plan_remains_one():
    from backend.app.services.vidman_logical_competitors import selected_logical_sources

    selected = selected_logical_sources(
        sources=[
            {"logical_competitor_id": 1, "account_id": 2, "main_id": 11870, "role": "PRIMARY", "priority": 0, "active": True},
            {"logical_competitor_id": 1, "account_id": 1, "main_id": 9352, "role": "FALLBACK", "priority": 10, "active": True},
        ],
        snapshot_ready={(2, 11870): True, (1, 9352): True},
    )

    assert len(selected) == 1
