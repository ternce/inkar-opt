from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    VidmanAccount,
    VidmanImportPage,
    VidmanImportRun,
    VidmanPriceList,
    VidmanRawItem,
)
from .widman_client import (
    WidmanClient,
    WidmanPriceList as ClientPriceList,
    parse_decimal,
    parse_max_page,
    split_rows,
    strip_tags,
)
from ..timezone import now_kz_naive


VIDMAN_STATUSES = {"queued", "running", "success", "partial", "error"}


@dataclass(frozen=True)
class VidmanRawRow:
    row_number: int
    raw_name: str
    raw_manufacturer: str
    raw_expiry_text: str
    expiry_date: date | None
    raw_price_text: str
    price: Decimal | None
    raw_pack_qty: str
    pack_qty: Decimal | None
    raw_min_order: str
    min_order: Decimal | None
    raw_stock: str
    stock: Decimal | None
    raw_html: str


@dataclass(frozen=True)
class VidmanCollectorConfig:
    delay_seconds: float = 0.25
    retry_count: int = 3
    timeout: float = 60.0
    auth_mode: str = "auto"
    only_main_id: int | None = None
    start_page: int | None = None
    max_pages: int | None = None


@dataclass(frozen=True)
class VidmanCollectionSummary:
    account: str
    import_run_id: int
    plks_discovered: int
    plks_succeeded: int
    plks_failed: int
    pages_fetched: int
    rows_stored: int
    elapsed_seconds: float
    status: str


class VidmanRawClient(Protocol):
    price_base_url: str

    async def login(self) -> None: ...

    async def get_price_lists(self) -> list[ClientPriceList]: ...

    async def _resolve_price_main_id(self, price_id: str) -> str: ...

    async def _load_items_page(self, main_id: str, page: int, *, referer_price_id: str | None = None) -> str: ...


def parse_expiry_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    patterns = (
        r"\b(\d{2})[./-](\d{2})[./-](\d{4})\b",
        r"\b(\d{4})[./-](\d{2})[./-](\d{2})\b",
        r"\b(\d{2})[./-](\d{4})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            parts = [int(x) for x in match.groups()]
            if len(parts) == 3 and len(match.group(1)) == 4:
                return date(parts[0], parts[1], parts[2])
            if len(parts) == 3:
                return date(parts[2], parts[1], parts[0])
            return date(parts[1], parts[0], 1)
        except ValueError:
            return None
    return None


def parse_raw_price_rows(html_text: str) -> list[VidmanRawRow]:
    rows = split_rows(html_text)
    if not rows:
        return []

    parsed: list[VidmanRawRow] = []
    fallback_row_number = 0
    for row_html in rows:
        th_cells = re.findall(r"<th\b[^>]*>(.*?)</\s*th\s*>", row_html or "", flags=re.I | re.S)
        td_cells = re.findall(r"<td\b[^>]*>(.*?)</\s*td\s*>", row_html or "", flags=re.I | re.S)
        if not td_cells:
            continue

        fallback_row_number += 1

        def cell_text(index: int) -> str:
            if index < 0 or index >= len(td_cells):
                return ""
            return strip_tags(td_cells[index])

        source_row_number = fallback_row_number
        if th_cells:
            row_number_text = strip_tags(th_cells[0])
            try:
                source_row_number = int(Decimal(str(row_number_text).strip()))
            except Exception:
                source_row_number = fallback_row_number

        raw_name = cell_text(0).strip()
        raw_price_text = cell_text(3)
        if not raw_name and not raw_price_text:
            continue

        raw_expiry_text = cell_text(2)
        parsed.append(
            VidmanRawRow(
                row_number=source_row_number,
                raw_name=raw_name,
                raw_manufacturer=cell_text(1).lstrip(" -\u2010\u2011\u2012\u2013\u2014\u2015").strip(),
                raw_expiry_text=raw_expiry_text,
                expiry_date=parse_expiry_date(raw_expiry_text),
                raw_price_text=raw_price_text,
                price=parse_decimal(raw_price_text),
                raw_pack_qty=cell_text(4),
                pack_qty=parse_decimal(cell_text(4)),
                raw_min_order=cell_text(5),
                min_order=parse_decimal(cell_text(5)),
                raw_stock=cell_text(6),
                stock=parse_decimal(cell_text(6)),
                raw_html=row_html,
            )
        )
    return parsed


def make_row_hash(
    *,
    import_run_id: int,
    account_id: int,
    main_id: int,
    page_number: int,
    row: VidmanRawRow,
) -> str:
    payload = {
        "import_run_id": import_run_id,
        "account_id": account_id,
        "main_id": main_id,
        "page_number": page_number,
        "row_number": row.row_number,
        "raw": {
            "name": row.raw_name,
            "manufacturer": row.raw_manufacturer,
            "expiry": row.raw_expiry_text,
            "price": row.raw_price_text,
            "pack_qty": row.raw_pack_qty,
            "min_order": row.raw_min_order,
            "stock": row.raw_stock,
            "html": row.raw_html,
        },
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class VidmanRawCollector:
    def __init__(
        self,
        *,
        db: Session,
        login: str,
        password: str,
        account_name: str = "",
        config: VidmanCollectorConfig | None = None,
        client: VidmanRawClient | None = None,
    ) -> None:
        self.db = db
        self.login = login
        self.password = password
        self.account_name = account_name
        self.config = config or VidmanCollectorConfig()
        self.client = client or WidmanClient(
            login=login,
            password=password,
            timeout=self.config.timeout,
            max_retries=self.config.retry_count,
            auth_mode=self.config.auth_mode,
        )

    async def discover_price_lists(self) -> tuple[VidmanAccount, list[VidmanPriceList]]:
        account = self._ensure_account()
        await self.client.login()
        return account, await self._discover_price_lists(account)

    async def collect(self, *, resume_run_id: int | None = None) -> VidmanCollectionSummary:
        started = time.monotonic()
        account = self._ensure_account()
        run = self._ensure_run(account_id=account.id, resume_run_id=resume_run_id)
        run.status = "running"
        run.started_at = run.started_at or now_kz_naive()
        self.db.commit()

        print("VIDMAN COLLECTION START")
        print(f"Account: {account.display_name or account.login}")

        try:
            await self.client.login()
            discovered = await self._discover_price_lists(account)
            if self.config.only_main_id is not None:
                discovered = [pl for pl in discovered if pl.main_id == self.config.only_main_id]
            run.total_plks = len(discovered)
            self.db.commit()
            print(f"PLKs discovered: {len(discovered)}")
            for pl in discovered:
                print(f"main_id={pl.main_id} name={pl.name}")

            failed_plks = 0
            for index, price_list in enumerate(discovered, start=1):
                print("")
                print(f"[{index}/{len(discovered)}]")
                await self._collect_price_list(run=run, account=account, price_list=price_list)
                pl_failed = self._plk_has_failed_pages(run_id=run.id, price_list_id=price_list.id)
                failed_plks += 1 if pl_failed else 0
                print("PLK COMPLETE" if not pl_failed else "PLK PARTIAL")

            self._refresh_run_counts(run)
            run.failed_plks = failed_plks
            run.completed_plks = max(0, run.total_plks - failed_plks)
            run.status = "success" if failed_plks == 0 else "partial"
            run.finished_at = now_kz_naive()
            self.db.commit()
        except Exception as exc:
            self._refresh_run_counts(run)
            run.status = "error"
            run.error_message = str(exc)
            run.finished_at = now_kz_naive()
            self.db.commit()
            raise

        elapsed = time.monotonic() - started
        print("")
        print("FINAL SUMMARY:")
        print(f"account={account.display_name or account.login}")
        print(f"PLKs discovered={run.total_plks}")
        print(f"PLKs succeeded={run.completed_plks}")
        print(f"PLKs failed={run.failed_plks}")
        print(f"pages fetched={run.total_pages}")
        print(f"rows stored={run.total_rows}")
        print(f"elapsed={elapsed:.2f}s")
        return VidmanCollectionSummary(
            account=account.display_name or account.login,
            import_run_id=run.id,
            plks_discovered=run.total_plks,
            plks_succeeded=run.completed_plks,
            plks_failed=run.failed_plks,
            pages_fetched=run.total_pages,
            rows_stored=run.total_rows,
            elapsed_seconds=elapsed,
            status=run.status,
        )

    def _ensure_account(self) -> VidmanAccount:
        login = self.login.strip()
        account = self.db.execute(select(VidmanAccount).where(VidmanAccount.login == login)).scalars().first()
        if account is None:
            account = VidmanAccount(login=login, display_name=self.account_name or login)
            self.db.add(account)
            self.db.commit()
            self.db.refresh(account)
        elif self.account_name and account.display_name != self.account_name:
            account.display_name = self.account_name
            account.updated_at = now_kz_naive()
            self.db.commit()
        return account

    def _ensure_run(self, *, account_id: int, resume_run_id: int | None) -> VidmanImportRun:
        if resume_run_id is not None:
            run = self.db.get(VidmanImportRun, resume_run_id)
            if run is None:
                raise ValueError(f"Vidman import run {resume_run_id} was not found")
            if run.account_id != account_id:
                raise ValueError(f"Vidman import run {resume_run_id} belongs to another account")
            return run
        run = VidmanImportRun(account_id=account_id, status="queued", metadata_json="{}")
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    async def _discover_price_lists(self, account: VidmanAccount) -> list[VidmanPriceList]:
        client_lists = await self.client.get_price_lists()
        out: list[VidmanPriceList] = []
        for source in client_lists:
            resolved_main_id = int(await self.client._resolve_price_main_id(str(source.id)))
            existing = self.db.execute(
                select(VidmanPriceList)
                .where(VidmanPriceList.account_id == account.id)
                .where(VidmanPriceList.main_id == resolved_main_id)
            ).scalars().first()
            source_url = f"{self.client.price_base_url.rstrip('/')}/price/see/{source.id}"
            if existing is None:
                existing = VidmanPriceList(
                    account_id=account.id,
                    main_id=resolved_main_id,
                    name=source.name,
                    source_url=source_url,
                )
                self.db.add(existing)
            else:
                existing.name = source.name
                existing.source_url = source_url
                existing.updated_at = now_kz_naive()
            self.db.commit()
            self.db.refresh(existing)
            out.append(existing)
        return out

    async def _collect_price_list(self, *, run: VidmanImportRun, account: VidmanAccount, price_list: VidmanPriceList) -> None:
        first_html = ""
        first_page_done = self._page_success(run_id=run.id, price_list_id=price_list.id, page_number=1)
        if not first_page_done:
            first_html = await self._fetch_page(str(price_list.main_id), 1)
        detected_pages = parse_max_page(first_html) if first_html else max(price_list.detected_pages or 1, 1)
        if self.config.max_pages is not None:
            detected_pages = min(detected_pages, self.config.max_pages)
        price_list.detected_pages = detected_pages
        price_list.updated_at = now_kz_naive()
        run.total_pages = int(self.db.scalar(select(func.count(VidmanImportPage.id)).where(VidmanImportPage.import_run_id == run.id)) or 0)
        self.db.commit()

        print(f"main_id={price_list.main_id}")
        print(f"name={price_list.name}")
        print(f"pages={detected_pages}")

        start_page = self.config.start_page or 1
        for page_number in range(start_page, detected_pages + 1):
            if self._page_success(run_id=run.id, price_list_id=price_list.id, page_number=page_number):
                continue
            try:
                html_text = first_html if page_number == 1 and first_html else await self._fetch_page(str(price_list.main_id), page_number)
            except Exception as exc:
                page = self._ensure_page(
                    run_id=run.id,
                    price_list_id=price_list.id,
                    main_id=price_list.main_id,
                    page_number=page_number,
                )
                page.status = "error"
                page.attempts += self.config.retry_count
                page.error_message = str(exc)
                page.finished_at = now_kz_naive()
                page.updated_at = now_kz_naive()
                self._refresh_run_counts(run)
                self.db.commit()
                print(f"page {page_number}/{detected_pages}")
                print(f"ERROR: {exc}")
                continue
            rows = parse_raw_price_rows(html_text)
            stored = self._store_page(
                run=run,
                account=account,
                price_list=price_list,
                page_number=page_number,
                rows=rows,
            )
            self._refresh_run_counts(run)
            self.db.commit()
            print(f"page {page_number}/{detected_pages}")
            print(f"rows={stored}")
            print(f"total_rows={run.total_rows}")
            if self.config.delay_seconds > 0:
                await asyncio.sleep(self.config.delay_seconds)
        price_list.last_collected_at = now_kz_naive()
        price_list.updated_at = now_kz_naive()
        self.db.commit()

    async def _fetch_page(self, main_id: str, page_number: int) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.config.retry_count + 1):
            try:
                return await self.client._load_items_page(main_id, page_number, referer_price_id=main_id)
            except Exception as exc:
                last_error = exc
                if attempt < self.config.retry_count:
                    await asyncio.sleep(min(5.0, 0.5 * attempt))
        raise RuntimeError(f"Vidman page fetch failed for main_id={main_id} page={page_number}: {last_error}")

    def _store_page(
        self,
        *,
        run: VidmanImportRun,
        account: VidmanAccount,
        price_list: VidmanPriceList,
        page_number: int,
        rows: list[VidmanRawRow],
    ) -> int:
        page = self._ensure_page(run_id=run.id, price_list_id=price_list.id, main_id=price_list.main_id, page_number=page_number)
        page.status = "running"
        page.started_at = page.started_at or now_kz_naive()
        page.attempts += 1
        page.updated_at = now_kz_naive()
        self.db.flush()

        stored = 0
        for row in rows:
            row_hash = make_row_hash(
                import_run_id=run.id,
                account_id=account.id,
                main_id=price_list.main_id,
                page_number=page_number,
                row=row,
            )
            exists = self.db.scalar(select(VidmanRawItem.id).where(VidmanRawItem.row_hash == row_hash).limit(1))
            if exists is not None:
                continue
            self.db.add(
                VidmanRawItem(
                    import_run_id=run.id,
                    account_id=account.id,
                    price_list_id=price_list.id,
                    main_id=price_list.main_id,
                    page_number=page_number,
                    row_number=row.row_number,
                    raw_name=row.raw_name,
                    raw_manufacturer=row.raw_manufacturer,
                    raw_expiry_text=row.raw_expiry_text,
                    expiry_date=row.expiry_date,
                    raw_price_text=row.raw_price_text,
                    price=row.price,
                    raw_pack_qty=row.raw_pack_qty,
                    pack_qty=row.pack_qty,
                    raw_min_order=row.raw_min_order,
                    min_order=row.min_order,
                    raw_stock=row.raw_stock,
                    stock=row.stock,
                    raw_html=row.raw_html,
                    row_hash=row_hash,
                )
            )
            stored += 1
        page.status = "success"
        page.rows_count = len(rows)
        page.error_message = ""
        page.finished_at = now_kz_naive()
        page.updated_at = now_kz_naive()
        self.db.flush()
        return stored

    def _ensure_page(self, *, run_id: int, price_list_id: int, main_id: int, page_number: int) -> VidmanImportPage:
        page = self.db.execute(
            select(VidmanImportPage)
            .where(VidmanImportPage.import_run_id == run_id)
            .where(VidmanImportPage.price_list_id == price_list_id)
            .where(VidmanImportPage.page_number == page_number)
        ).scalars().first()
        if page is None:
            page = VidmanImportPage(
                import_run_id=run_id,
                price_list_id=price_list_id,
                main_id=main_id,
                page_number=page_number,
                status="queued",
            )
            self.db.add(page)
            self.db.flush()
        return page

    def _page_success(self, *, run_id: int, price_list_id: int, page_number: int) -> bool:
        return bool(
            self.db.scalar(
                select(VidmanImportPage.id)
                .where(VidmanImportPage.import_run_id == run_id)
                .where(VidmanImportPage.price_list_id == price_list_id)
                .where(VidmanImportPage.page_number == page_number)
                .where(VidmanImportPage.status == "success")
                .limit(1)
            )
        )

    def _plk_has_failed_pages(self, *, run_id: int, price_list_id: int) -> bool:
        return bool(
            self.db.scalar(
                select(VidmanImportPage.id)
                .where(VidmanImportPage.import_run_id == run_id)
                .where(VidmanImportPage.price_list_id == price_list_id)
                .where(VidmanImportPage.status == "error")
                .limit(1)
            )
        )

    def _refresh_run_counts(self, run: VidmanImportRun) -> None:
        run.total_pages = int(
            self.db.scalar(
                select(func.count(VidmanImportPage.id))
                .where(VidmanImportPage.import_run_id == run.id)
                .where(VidmanImportPage.status == "success")
            )
            or 0
        )
        run.total_rows = int(
            self.db.scalar(select(func.count(VidmanRawItem.id)).where(VidmanRawItem.import_run_id == run.id)) or 0
        )
