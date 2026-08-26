from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import VidmanAccount, VidmanImportPage, VidmanImportRun, VidmanPriceList, VidmanRawItem
from backend.app.services.vidman_raw_collector import (
    VidmanCollectorConfig,
    VidmanRawCollector,
    parse_expiry_date,
    parse_raw_price_rows,
)
from backend.app.services.widman_client import WidmanPriceList as ClientPriceList, parse_max_page


HEADER = """
<tr>
  <th>РќР°РёРјРµРЅРѕРІР°РЅРёРµ</th>
  <th>РџСЂРѕРёР·РІРѕРґРёС‚РµР»СЊ</th>
  <th>РЎСЂРѕРє РіРѕРґРЅРѕСЃС‚Рё</th>
  <th>Р¦РµРЅР°</th>
  <th>РљРѕР». РІ СѓРї.</th>
  <th>РњРёРЅ. Р·Р°РєР°Р·</th>
  <th>РћСЃС‚Р°С‚РѕРє</th>
</tr>
"""


def table(*rows: str, pages: int = 1) -> str:
    pagination = "".join(f"<a onclick=\"paginationSet({page})\">{page}</a>" for page in range(2, pages + 1))
    return f"<table>{HEADER}{''.join(rows)}</table><div class=\"pagination\">{pagination}</div>"


def row(
    name: str = "L-С‚РёСЂРѕРєСЃРёРЅ 100 Р‘РµСЂР»РёРЅ РҐРµРјРё",
    manufacturer: str = "Р‘РµСЂР»РёРЅ РҐРµРјРё",
    expiry: str = "12.2027",
    price: str = "1 234,50 С‚Рі",
    pack: str = "10",
    min_order: str = "2",
    stock: str = "45",
) -> str:
    return (
        "<tr>"
        f"<td>{name}</td><td>{manufacturer}</td><td>{expiry}</td><td>{price}</td>"
        f"<td>{pack}</td><td>{min_order}</td><td>{stock}</td>"
        "</tr>"
    )


def vidman_row_with_source_number(
    row_number: int = 8,
    name: str = "L-Цет 5 мг, №30, табл. (Кусум Хелтхер)",
    manufacturer: str = "Кусум Хелтхер",
    expiry: str = "30.09.2028",
    price: str = "2135.00 тг",
    pack: str = "1",
    min_order: str = "1",
    stock: str = "366",
) -> str:
    return (
        "<tr>"
        f"<th class='center'>{row_number}</th>"
        f"<td>{name}</td>"
        f"<td>{manufacturer}</td>"
        f"<td class='center'>{expiry}</td>"
        f"<td>{price}</td>"
        f"<td>{pack}</td>"
        f"<td>{min_order}</td>"
        f"<td>{stock}</td>"
        "</tr>"
    )


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@dataclass
class FakeVidmanClient:
    pages: dict[tuple[int, int], str]
    lists: list[ClientPriceList]
    failures: dict[tuple[int, int], int] | None = None
    price_base_url: str = "https://prv.kz"

    async def login(self) -> None:
        return None

    async def get_price_lists(self) -> list[ClientPriceList]:
        return self.lists

    async def _resolve_price_main_id(self, price_id: str) -> str:
        return str(price_id)

    async def _load_items_page(self, main_id: str, page: int, *, referer_price_id: str | None = None) -> str:
        key = (int(main_id), page)
        if self.failures and self.failures.get(key, 0) > 0:
            self.failures[key] -= 1
            raise RuntimeError(f"temporary failure {main_id}/{page}")
        return self.pages[key]


def test_html_row_parsing_empty_manufacturer_price_expiry_and_raw_preserved():
    parsed = parse_raw_price_rows(table(row(manufacturer="", price="1 234,50 С‚Рі", expiry="31.12.2027")))

    assert len(parsed) == 1
    item = parsed[0]
    assert item.raw_name.startswith("L-С‚РёСЂРѕРєСЃРёРЅ")
    assert item.raw_manufacturer == ""
    assert item.raw_price_text == "1 234,50 С‚Рі"
    assert item.price is not None and str(item.price) == "1234.50"
    assert item.expiry_date is not None and item.expiry_date.isoformat() == "2027-12-31"
    assert "<td>" in item.raw_html


def test_vidman_row_parser_uses_th_as_source_row_number_and_tds_as_data_columns():
    parsed = parse_raw_price_rows(table(vidman_row_with_source_number()))

    assert len(parsed) == 1
    item = parsed[0]
    assert item.row_number == 8
    assert item.raw_name == "L-Цет 5 мг, №30, табл. (Кусум Хелтхер)"
    assert item.raw_manufacturer == "Кусум Хелтхер"
    assert item.raw_expiry_text == "30.09.2028"
    assert item.expiry_date is not None and item.expiry_date.isoformat() == "2028-09-30"
    assert item.raw_price_text == "2135.00 тг"
    assert item.price is not None and str(item.price) == "2135.00"
    assert item.raw_pack_qty == "1"
    assert item.pack_qty is not None and str(item.pack_qty) == "1"
    assert item.raw_min_order == "1"
    assert item.min_order is not None and str(item.min_order) == "1"
    assert item.raw_stock == "366"
    assert item.stock is not None and str(item.stock) == "366"


def test_vidman_row_parser_preserves_empty_manufacturer_without_column_shift():
    parsed = parse_raw_price_rows(
        table(
            vidman_row_with_source_number(
                row_number=40,
                name="  Формула спокойствия магний + B6 №30, табл.  ",
                manufacturer="",
                expiry="01.08.2023",
                price="1635.00 тг",
                pack="1",
                min_order="1",
                stock="22",
            )
        )
    )

    assert len(parsed) == 1
    item = parsed[0]
    assert item.row_number == 40
    assert item.raw_name == "Формула спокойствия магний + B6 №30, табл."
    assert item.raw_manufacturer == ""
    assert item.raw_expiry_text == "01.08.2023"
    assert item.expiry_date is not None and item.expiry_date.isoformat() == "2023-08-01"
    assert item.raw_price_text == "1635.00 тг"
    assert item.price is not None and str(item.price) == "1635.00"
    assert item.raw_pack_qty == "1"
    assert item.raw_min_order == "1"
    assert item.raw_stock == "22"
    assert item.stock is not None and str(item.stock) == "22"


def test_expiry_parsing_month_year():
    assert parse_expiry_date("12.2027").isoformat() == "2027-12-01"


def test_pagination_extraction_one_page_and_multi_page():
    assert parse_max_page(table(row(), pages=1)) == 1
    assert parse_max_page(table(row(), pages=5)) == 5


def test_retry_handling_eventually_stores_page():
    db = _session()
    client = FakeVidmanClient(
        lists=[ClientPriceList(id=1191, name="Amanat")],
        pages={(1191, 1): table(row(), pages=1)},
        failures={(1191, 1): 1},
    )
    collector = VidmanRawCollector(
        db=db,
        login="login",
        password="secret",
        client=client,
        config=VidmanCollectorConfig(delay_seconds=0, retry_count=2),
    )

    summary = asyncio.run(collector.collect())

    assert summary.status == "success"
    assert db.scalar(select(func.count(VidmanRawItem.id))) == 1


def test_duplicate_page_retry_does_not_duplicate_rows():
    db = _session()
    client = FakeVidmanClient(
        lists=[ClientPriceList(id=1191, name="Amanat")],
        pages={(1191, 1): table(row(), pages=1)},
    )
    collector = VidmanRawCollector(
        db=db,
        login="login",
        password="secret",
        client=client,
        config=VidmanCollectorConfig(delay_seconds=0),
    )
    summary = asyncio.run(collector.collect())
    account_id = db.scalar(select(VidmanImportRun.account_id).where(VidmanImportRun.id == summary.import_run_id))
    price_list = db.execute(select(VidmanPriceList)).scalar_one()
    run = db.get(VidmanImportRun, summary.import_run_id)

    collector._store_page(
        run=run,
        account=db.get(VidmanAccount, account_id),
        price_list=price_list,
        page_number=1,
        rows=parse_raw_price_rows(table(row(), pages=1)),
    )
    db.commit()

    assert db.scalar(select(func.count(VidmanRawItem.id))) == 1


def test_resume_behavior_skips_completed_page_and_finishes_failed_page():
    db = _session()
    pages = {
        (1191, 1): table(row(name="first"), pages=2),
        (1191, 2): table(row(name="second"), pages=2),
    }
    failing = FakeVidmanClient(
        lists=[ClientPriceList(id=1191, name="Amanat")],
        pages=pages,
        failures={(1191, 2): 3},
    )
    first = VidmanRawCollector(
        db=db,
        login="login",
        password="secret",
        client=failing,
        config=VidmanCollectorConfig(delay_seconds=0, retry_count=1),
    )
    first_summary = asyncio.run(first.collect())

    assert first_summary.status == "partial"
    assert db.scalar(select(func.count(VidmanRawItem.id))) == 1

    healthy = FakeVidmanClient(lists=[ClientPriceList(id=1191, name="Amanat")], pages=pages)
    resumed = VidmanRawCollector(
        db=db,
        login="login",
        password="secret",
        client=healthy,
        config=VidmanCollectorConfig(delay_seconds=0, retry_count=1),
    )
    second_summary = asyncio.run(resumed.collect(resume_run_id=first_summary.import_run_id))

    assert second_summary.status == "success"
    assert db.scalar(select(func.count(VidmanRawItem.id))) == 2
    statuses = db.execute(select(VidmanImportPage.page_number, VidmanImportPage.status)).all()
    assert sorted(statuses) == [(1, "success"), (2, "success")]


def test_no_cross_plk_deduplication():
    db = _session()
    identical = row(name="same drug")
    client = FakeVidmanClient(
        lists=[ClientPriceList(id=1191, name="A"), ClientPriceList(id=1192, name="B")],
        pages={
            (1191, 1): table(identical, pages=1),
            (1192, 1): table(identical, pages=1),
        },
    )
    collector = VidmanRawCollector(
        db=db,
        login="login",
        password="secret",
        client=client,
        config=VidmanCollectorConfig(delay_seconds=0),
    )

    summary = asyncio.run(collector.collect())

    assert summary.status == "success"
    assert db.scalar(select(func.count(VidmanRawItem.id))) == 2
    assert sorted(db.execute(select(VidmanRawItem.main_id)).scalars().all()) == [1191, 1192]
