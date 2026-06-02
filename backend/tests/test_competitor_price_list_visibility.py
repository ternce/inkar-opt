from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import (
    CompetitorPriceList,
    CompetitorPriceListItem,
    PriceFormat,
    PriceFormatCompetitorAssignment,
)
from backend.app.services.competitor_assignments import get_assigned_competitor_price_lists
from backend.app.services.competitor_price_lists import _visible_competitor_price_list_rows
from backend.app.services.competitor_price_lists import list_competitor_price_lists


def _row(row_id: int, source_type: str = "vidman", branch_name: str = "Aktau", competitor_name: str = "Inkar"):
    return SimpleNamespace(
        id=row_id,
        source_type=source_type,
        branch_name=branch_name,
        competitor_name=competitor_name,
    )


def test_assignment_visibility_keeps_non_empty_vidman_duplicates():
    rows = [_row(1), _row(2), _row(3, source_type="provisor")]
    counts = {1: 10, 2: 10, 3: 5}

    visible = _visible_competitor_price_list_rows(rows, counts)

    assert [row.id for row in visible] == [1, 2, 3]


def test_assignment_visibility_hides_only_real_zero_items():
    rows = [_row(1), _row(2), _row(3, source_type="provisor")]
    counts = {1: 10, 2: 0, 3: 0}

    visible = _visible_competitor_price_list_rows(rows, counts)

    assert [row.id for row in visible] == [1]


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _format(db, *, code="TEST_01", branch="Алматы"):
    row = PriceFormat(code=code, name=code, branch=branch)
    db.add(row)
    db.flush()
    return row


def _price_list(
    db,
    pf,
    *,
    source_key,
    branch_name,
    account_id="4",
    external_price_list_id=None,
):
    row = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key=source_key,
        display_name=branch_name,
        supplier=branch_name,
        branch_id=str(external_price_list_id or source_key),
        branch_code=str(external_price_list_id or source_key),
        branch_name=branch_name,
        competitor_name=branch_name,
        account_id=str(account_id),
        account_login=f"account-{account_id}",
        external_price_list_id=str(external_price_list_id or source_key),
    )
    db.add(row)
    db.flush()
    return row


def _item(db, price_list, *, distributor_goods_id="SKU-1", goods_id=100):
    db.add(
        CompetitorPriceListItem(
            price_list_id=price_list.id,
            provisor_goods_id=goods_id,
            filial_id=int(price_list.external_price_list_id or 0) if str(price_list.external_price_list_id or "").isdigit() else None,
            name="Supplier item",
            distributor_goods_name="Supplier item",
            distributor_goods_id=distributor_goods_id,
            distributor_price=10,
            stock=1,
            match_type="unmatched",
        )
    )
    db.flush()


def test_global_pool_returns_usable_provisor_rows_outside_format_branch_with_metadata():
    db = _session()
    pf = _format(db, branch="Алматы")
    almaty = _price_list(db, pf, source_key="4:128", branch_name="Инкар (Алматы)", external_price_list_id=128)
    aktau = _price_list(db, pf, source_key="3:159", branch_name="Медсервис (Актау)", account_id="3", external_price_list_id=159)
    zero = _price_list(db, pf, source_key="3:302", branch_name="Аманат (Актау)", account_id="3", external_price_list_id=302)
    _item(db, almaty)
    _item(db, aktau)

    rows = list_competitor_price_lists(db=db, price_format_code=pf.code)
    by_id = {row["id"]: row for row in rows}

    assert set(by_id) == {almaty.id, aktau.id}
    assert by_id[almaty.id]["visibleForFormatBranch"] is True
    assert by_id[almaty.id]["branchMatchReason"]
    assert by_id[aktau.id]["visibleForFormatBranch"] is False
    assert by_id[aktau.id]["branchMismatchReason"]
    assert zero.id not in by_id


def test_global_pool_dedupe_preserves_same_filial_from_different_accounts():
    db = _session()
    pf = _format(db, branch="Алматы")
    first = _price_list(db, pf, source_key="3:128", branch_name="Инкар (Алматы)", account_id="3", external_price_list_id=128)
    second = _price_list(db, pf, source_key="4:128", branch_name="Инкар (Алматы)", account_id="4", external_price_list_id=128)
    _item(db, first, distributor_goods_id="SKU-A", goods_id=101)
    _item(db, second, distributor_goods_id="SKU-B", goods_id=102)

    rows = list_competitor_price_lists(db=db, price_format_code=pf.code)

    assert {row["id"] for row in rows} == {first.id, second.id}
    assert {row["accountId"] for row in rows} == {"3", "4"}


def test_active_assignments_still_exclude_inactive_global_pool_rows():
    db = _session()
    pf = _format(db, branch="Алматы")
    active = _price_list(db, pf, source_key="4:128", branch_name="Инкар (Алматы)", external_price_list_id=128)
    inactive = _price_list(db, pf, source_key="4:1075", branch_name="Аманат (Алматы)", external_price_list_id=1075)
    _item(db, active)
    _item(db, inactive)
    db.add(PriceFormatCompetitorAssignment(price_format_id=pf.id, competitor_price_list_id=active.id, is_active=True))
    db.add(PriceFormatCompetitorAssignment(price_format_id=pf.id, competitor_price_list_id=inactive.id, is_active=False))
    db.flush()

    assigned = get_assigned_competitor_price_lists(db=db, price_format_id=pf.id)

    assert [item.price_list.id for item in assigned] == [active.id]
