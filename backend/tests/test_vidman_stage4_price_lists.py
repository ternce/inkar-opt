from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import (
    CompetitorPrice,
    CompetitorPriceList,
    CompetitorPriceListItem,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    Product,
    VidmanAccount,
    VidmanCanonicalProduct,
    VidmanCompetitorPriceListSource,
    VidmanImportPage,
    VidmanImportRun,
    VidmanNormalizedItem,
    VidmanPriceList,
    VidmanProductMatch,
    VidmanRawCanonicalLink,
    VidmanRawItem,
)
from backend.app.services.vidman_competitor_price_lists import (
    build_vidman_competitor_price_list,
    ensure_vidman_competitor_price_list_source,
    vidman_competitor_source_key,
)
from backend.app.services.vidman_product_matching import AUTO_MATCHED, MANUALLY_APPROVED, REVIEW_REQUIRED


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(db, *, source_active: bool = True, run_status: str = "success", page_status: str = "success"):
    pf = PriceFormat(code="FMT", name="Format")
    account = VidmanAccount(login="vidman-a", display_name="Vidman A")
    product_a = Product(code="SKU-A", name="Product A", cost=10)
    product_b = Product(code="SKU-B", name="Product B", cost=10)
    db.add_all([pf, account, product_a, product_b])
    db.flush()
    plk = VidmanPriceList(account_id=account.id, main_id=1191, name="Amanat Aktau")
    run = VidmanImportRun(account_id=account.id, status=run_status)
    db.add_all([plk, run])
    db.flush()
    page = VidmanImportPage(
        import_run_id=run.id,
        price_list_id=plk.id,
        main_id=plk.main_id,
        page_number=1,
        status=page_status,
    )
    db.add(page)
    ensure_vidman_competitor_price_list_source(
        db=db,
        account_id=account.id,
        main_id=plk.main_id,
        price_format_code=pf.code,
        is_active=source_active,
        region="Aktau",
        branch_id="aktau",
        branch_code="aktau",
        branch_name="Aktau",
        competitor_name="Amanat",
    )
    db.flush()
    return pf, account, plk, run, product_a, product_b


def _raw_link_match(
    db,
    *,
    account,
    plk,
    run,
    product,
    status=AUTO_MATCHED,
    price="10",
    raw_name="Medicine",
    raw_manufacturer="Maker",
    raw_stock="5",
    row_number=1,
):
    raw = VidmanRawItem(
        import_run_id=run.id,
        account_id=account.id,
        price_list_id=plk.id,
        main_id=plk.main_id,
        page_number=1,
        row_number=row_number,
        raw_name=raw_name,
        raw_manufacturer=raw_manufacturer,
        raw_price_text=str(price),
        price=Decimal(str(price)) if str(price).replace(".", "", 1).isdigit() else None,
        raw_stock=raw_stock,
        stock=Decimal("5"),
        row_hash=f"{run.id}:{row_number}:{raw_name}:{price}",
    )
    norm = VidmanNormalizedItem(raw_item_id=1, normalized_name=raw_name, normalized_manufacturer=raw_manufacturer)
    canonical = VidmanCanonicalProduct(
        canonical_name=raw_name,
        canonical_manufacturer=raw_manufacturer,
        base_name=raw_name.casefold(),
        canonical_signature=f"{raw_name}:{raw_manufacturer}:{row_number}",
    )
    db.add_all([raw, canonical])
    db.flush()
    norm.raw_item_id = raw.id
    db.add(norm)
    db.flush()
    link = VidmanRawCanonicalLink(raw_item_id=raw.id, normalized_item_id=norm.id, canonical_product_id=canonical.id)
    match = VidmanProductMatch(canonical_product_id=canonical.id, product_id=product.id, status=status, confidence=99)
    db.add_all([link, match])
    db.flush()
    return raw, canonical, match


def _build(db, account, plk, *, apply=False, run=None, require_active=True):
    return build_vidman_competitor_price_list(
        db=db,
        account_id=account.id,
        main_id=plk.main_id,
        price_format_code="FMT",
        import_run_id=run.id if run is not None else None,
        apply=apply,
        require_active=require_active,
    )


def test_auto_matched_and_manually_approved_are_published():
    db = _session()
    _, account, plk, run, product_a, product_b = _seed(db)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, status=AUTO_MATCHED, row_number=1)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_b, status=MANUALLY_APPROVED, row_number=2)

    summary = _build(db, account, plk, apply=True, run=run)

    assert summary.rows_written == 2
    assert db.query(CompetitorPriceListItem).count() == 2


@pytest.mark.parametrize("status", [REVIEW_REQUIRED, "UNMATCHED", "REJECTED", "MANUAL_UNMATCHED", "MANUAL_NO_VIDMAN_MATCH"])
def test_untrusted_statuses_are_excluded(status):
    db = _session()
    _, account, plk, run, product_a, _ = _seed(db)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, status=status)

    summary = _build(db, account, plk, apply=True, run=run)

    assert summary.rows_written == 0
    assert summary.skipped_reason == "no_rows_to_publish"
    assert db.query(CompetitorPriceListItem).count() == 0


@pytest.mark.parametrize("price", [None, "", "0", "-1", "NaN", "Infinity", "abc"])
def test_invalid_prices_are_skipped(price):
    db = _session()
    _, account, plk, run, product_a, _ = _seed(db)
    raw, _, _ = _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, price="10")
    raw.raw_price_text = "" if price is None else str(price)
    raw.price = None
    db.flush()

    summary = _build(db, account, plk, apply=True, run=run)

    assert summary.invalid_price_rows == 1
    assert summary.rows_written == 0


def test_price_validation_accepts_decimal_text_with_comma():
    db = _session()
    _, account, plk, run, product_a, _ = _seed(db)
    raw, _, _ = _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, price="10")
    raw.price = None
    raw.raw_price_text = "12,50"
    db.flush()

    summary = _build(db, account, plk, apply=True, run=run)
    item = db.execute(select(CompetitorPriceListItem)).scalar_one()

    assert summary.rows_written == 1
    assert item.distributor_price == Decimal("12.5000")


def test_same_snapshot_rerun_is_idempotent():
    db = _session()
    _, account, plk, run, product_a, _ = _seed(db)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, price="10")

    first = _build(db, account, plk, apply=True, run=run)
    second = _build(db, account, plk, apply=True, run=run)

    assert first.competitor_price_list_id == second.competitor_price_list_id
    assert db.query(CompetitorPriceListItem).count() == 1


def test_failed_run_cannot_replace_previous_valid_list():
    db = _session()
    _, account, plk, run, product_a, _ = _seed(db)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, price="10")
    _build(db, account, plk, apply=True, run=run)
    failed = VidmanImportRun(account_id=account.id, status="error")
    db.add(failed)
    db.flush()

    summary = build_vidman_competitor_price_list(
        db=db,
        account_id=account.id,
        main_id=plk.main_id,
        price_format_code="FMT",
        import_run_id=failed.id,
        apply=True,
    )

    assert summary.skipped_reason == "import_run_not_success"
    assert summary.preserved_previous_snapshot is True
    assert db.query(CompetitorPriceListItem).count() == 1


def test_failed_page_cannot_replace_previous_valid_list():
    db = _session()
    _, account, plk, run, product_a, _ = _seed(db)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, price="10")
    _build(db, account, plk, apply=True, run=run)
    failed_run = VidmanImportRun(account_id=account.id, status="success")
    db.add(failed_run)
    db.flush()
    db.add(VidmanImportPage(import_run_id=failed_run.id, price_list_id=plk.id, main_id=plk.main_id, page_number=1, status="error"))
    db.flush()

    summary = _build(db, account, plk, apply=True, run=failed_run)

    assert summary.skipped_reason == "snapshot_has_no_rows"
    assert db.query(CompetitorPriceListItem).count() == 1


def test_inactive_source_is_blocked():
    db = _session()
    _, account, plk, run, product_a, _ = _seed(db, source_active=False)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a)

    summary = _build(db, account, plk, apply=True, run=run)

    assert summary.skipped_reason == "inactive_or_unconfigured_vidman_plk_source"
    assert db.query(CompetitorPriceListItem).count() == 0


def test_missing_region_mapping_is_blocked():
    db = _session()
    _, account, plk, run, product_a, _ = _seed(db)
    source = db.execute(select(CompetitorPriceList).where(CompetitorPriceList.source_type == "none")).scalar_one_or_none()
    assert source is None
    cfg = db.query(VidmanCompetitorPriceListSource).one()
    cfg.region = ""
    db.flush()
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a)

    summary = _build(db, account, plk, apply=True, run=run)

    assert summary.skipped_reason == "missing_explicit_region_or_competitor_mapping"
    assert db.query(CompetitorPriceListItem).count() == 0


def test_identity_equivalent_duplicate_with_same_price_dedupes():
    db = _session()
    _, account, plk, run, product_a, _ = _seed(db)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, price="10", row_number=1)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, price="10", row_number=2)

    summary = _build(db, account, plk, apply=True, run=run)

    assert summary.duplicate_equivalent_rows == 1
    assert summary.rows_written == 1


def test_same_identity_different_prices_is_conflict_and_excluded():
    db = _session()
    _, account, plk, run, product_a, _ = _seed(db)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, price="10", row_number=1)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, price="11", row_number=2)

    summary = _build(db, account, plk, apply=True, run=run)

    assert summary.conflicting_duplicate_rows == 1
    assert summary.rows_written == 0


def test_same_product_different_raw_identity_can_publish_multiple_prices():
    db = _session()
    _, account, plk, run, product_a, _ = _seed(db)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, price="10", raw_name="Med A", row_number=1)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, price="11", raw_name="Med A series", row_number=2)

    summary = _build(db, account, plk, apply=True, run=run)

    assert summary.rows_written == 2


def test_unmapped_raw_rows_do_not_break_publish():
    db = _session()
    _, account, plk, run, product_a, _ = _seed(db)
    db.add(
        VidmanRawItem(
            import_run_id=run.id,
            account_id=account.id,
            price_list_id=plk.id,
            main_id=plk.main_id,
            page_number=1,
            row_number=99,
            raw_name="Unmapped",
            raw_price_text="20",
            price=Decimal("20"),
            row_hash="unmapped",
        )
    )
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a)
    db.flush()

    summary = _build(db, account, plk, apply=True, run=run)

    assert summary.total_snapshot_rows == 2
    assert summary.rows_written == 1


def test_manual_mapping_survives_as_trusted_status():
    db = _session()
    _, account, plk, run, product_a, _ = _seed(db)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, status=MANUALLY_APPROVED)

    summary = _build(db, account, plk, apply=True, run=run)
    item = db.execute(select(CompetitorPriceListItem)).scalar_one()

    assert summary.rows_written == 1
    assert item.product_id == product_a.id


def test_source_identity_preserves_account_and_main_id():
    assert vidman_competitor_source_key(7, 1191) == "account:7:main:1191"


def test_apply_writes_existing_competitor_price_list_metadata_and_raw_json():
    db = _session()
    _, account, plk, run, product_a, _ = _seed(db)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a)

    _build(db, account, plk, apply=True, run=run)
    price_list = db.execute(select(CompetitorPriceList)).scalar_one()
    item = db.execute(select(CompetitorPriceListItem)).scalar_one()
    raw_json = json.loads(item.raw_json)

    assert price_list.source_type == "vidman"
    assert price_list.source_key == f"account:{account.id}:main:{plk.main_id}"
    assert price_list.region == "Aktau"
    assert raw_json["importRunId"] == run.id


def test_legacy_competitor_prices_are_written_without_touching_emit_or_provisor():
    db = _session()
    pf, account, plk, run, product_a, _ = _seed(db)
    emit = CompetitorPrice(price_format_id=pf.id, product_id=product_a.id, source_name="emit:1", supplier="Emit", source_price=5)
    provisor = CompetitorPrice(price_format_id=pf.id, product_id=product_a.id, source_name="provisor:1", supplier="Provisor", source_price=6)
    db.add_all([emit, provisor])
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, price="10")

    _build(db, account, plk, apply=True, run=run)
    names = sorted(row.source_name for row in db.execute(select(CompetitorPrice)).scalars())

    assert "emit:1" in names
    assert "provisor:1" in names
    assert f"vidman:account:{account.id}:main:{plk.main_id}" in names


def test_active_assignment_rebuilds_selected_competitor_prices():
    db = _session()
    pf, account, plk, run, product_a, _ = _seed(db)
    _raw_link_match(db, account=account, plk=plk, run=run, product=product_a, price="10")
    first = _build(db, account, plk, apply=True, run=run)
    db.add(PriceFormatCompetitorAssignment(price_format_id=pf.id, competitor_price_list_id=first.competitor_price_list_id))
    db.flush()

    second = _build(db, account, plk, apply=True, run=run)

    assert second.rows_written == 1
    assert db.query(CompetitorPrice).filter(CompetitorPrice.source_name.like("vidman:%")).count() == 1
