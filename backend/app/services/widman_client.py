from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)


class WidmanRedirectLoopError(RuntimeError):
    pass


class WidmanInvalidCredentialsError(RuntimeError):
    pass


@dataclass(frozen=True)
class WidmanPriceList:
    id: int
    name: str
    enabled: bool | None = None
    counterpart_id: str = ""
    updated_at: str = ""
    city: str = ""
    phone: str = ""


@dataclass(frozen=True)
class WidmanPriceItem:
    name: str
    manufacturer: str = ""
    expiry_date: str | None = None
    price: Decimal | None = None
    pack_quantity: Decimal | None = None
    min_order: Decimal | None = None
    stock: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WidmanFetchResult:
    price_lists: list[dict[str, Any]]


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(html.unescape(value).replace("\xa0", " ").split())


def parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    cleaned = normalize_text(value)
    cleaned = cleaned.replace("тг", "").replace("₸", "").replace(" ", "").replace(",", ".")
    cleaned = re.sub(r"[^0-9.\-]", "", cleaned)
    if not cleaned or cleaned in {"-", ".", "-."}:
        return None
    try:
        return Decimal(cleaned)
    except Exception:
        return None


def strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<script\b[^>]*>.*?</script>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<style\b[^>]*>.*?</style>", " ", fragment, flags=re.I | re.S)
    return normalize_text(re.sub(r"<[^>]+>", " ", fragment))


def split_rows(table_html: str) -> list[str]:
    return re.findall(r"<tr\b[^>]*>(.*?)</tr>", table_html or "", flags=re.I | re.S)


def split_cells(row_html: str) -> list[str]:
    return [
        match.group(2)
        for match in re.finditer(r"<(td|th)\b[^>]*>(.*?)</\s*\1\s*>", row_html or "", flags=re.I | re.S)
    ]


def hidden_input_values(fragment: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for match in re.finditer(r"<input\b([^>]*)>", fragment or "", flags=re.I | re.S):
        attrs = match.group(1)
        name_m = re.search(r"\bname\s*=\s*['\"]?([^'\"\s>]+)", attrs, flags=re.I)
        if not name_m:
            continue
        type_m = re.search(r"\btype\s*=\s*['\"]?([^'\"\s>]+)", attrs, flags=re.I)
        if type_m and type_m.group(1).lower() not in {"hidden", "submit"}:
            continue
        value_m = re.search(r"\bvalue\s*=\s*['\"]([^'\"]*)", attrs, flags=re.I)
        values[html.unescape(name_m.group(1))] = html.unescape(value_m.group(1)) if value_m else ""
    return values


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_d = {k.lower(): v or "" for k, v in attrs}
        tag_l = tag.lower()
        if tag_l == "form":
            self._current = {"attrs": attrs_d, "inputs": {}}
        elif tag_l == "input" and self._current is not None:
            name = attrs_d.get("name")
            if name:
                self._current["inputs"][name] = attrs_d.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def parse_price_lists(html_text: str) -> list[WidmanPriceList]:
    out: list[WidmanPriceList] = []
    seen: set[int] = set()
    for row_html in split_rows(html_text):
        link = re.search(
            r"<a\b[^>]*href\s*=\s*['\"]?[^'\">\s]*price/see/(\d+)['\"]?[^>]*>(.*?)</a>",
            row_html,
            re.I | re.S,
        )
        if not link:
            continue
        price_id = int(link.group(1))
        if price_id in seen:
            continue
        seen.add(price_id)

        cells = split_cells(row_html)
        first_cell = cells[0] if cells else row_html
        counterpart_m = re.search(r"<td\b[^>]*\btitle\s*=\s*['\"]?([^'\"\s>]+)", row_html, re.I)
        supplier_m = re.search(
            r"<span\b[^>]*id\s*=\s*['\"]supplier_name_[^'\"]+['\"][^>]*>(.*?)</span>",
            row_html,
            re.I | re.S,
        )
        name = normalize_text(strip_tags(supplier_m.group(1))) if supplier_m else normalize_text(strip_tags(link.group(2)))

        enabled: bool | None
        if "glyphicon-eye-open" in row_html:
            enabled = True
        elif "glyphicon-eye-close" in row_html:
            enabled = False
        else:
            enabled = None

        out.append(
            WidmanPriceList(
                id=price_id,
                name=name or f"Widman {price_id}",
                enabled=enabled,
                counterpart_id=counterpart_m.group(1) if counterpart_m else "",
                updated_at=strip_tags(cells[1]) if len(cells) > 1 else "",
                phone=strip_tags(cells[6]) if len(cells) > 6 else "",
                city=strip_tags(cells[7]) if len(cells) > 7 else "",
            )
        )
    return out


def parse_max_page(html_text: str) -> int:
    text = html_text or ""
    pages = [int(x) for x in re.findall(r"paginationSet\((\d+)\)", text)]
    pages.extend(int(x) for x in re.findall(r"\bpageCurrent\s*[:=]\s*['\"]?(\d+)", text, re.I))
    pages.extend(int(x) for x in re.findall(r"\bdata-page\s*=\s*['\"]?(\d+)", text, re.I))
    for fragment in re.findall(r"<(?:ul|div|nav)\b[^>]*(?:pagination|pager)[^>]*>(.*?)</(?:ul|div|nav)>", text, re.I | re.S):
        pages.extend(int(x) for x in re.findall(r">\s*(\d{1,4})\s*<", fragment))
    current_m = re.search(r'id\s*=\s*["\']pageCurrent["\'][^>]*value\s*=\s*["\'](\d+)', text, re.I)
    if current_m:
        pages.append(int(current_m.group(1)))
    return max(pages) if pages else 1


def parse_price_items(html_text: str) -> list[WidmanPriceItem]:
    rows = split_rows(html_text)
    if not rows:
        logger.warning("[WIDMAN_PRICE_ITEMS_FAIL_REASON] reason=no_rows html_head=%s", (html_text or "")[:3000])
        return []

    header_cells = [strip_tags(x).lower() for x in split_cells(rows[0])]
    required_header_groups = (
        ("наименование", "товар", "название"),
        ("производитель",),
        ("цена",),
        ("остаток",),
    )
    missing_header_groups = [
        group for group in required_header_groups
        if not any(any(needle in header for header in header_cells) for needle in group)
    ]
    if missing_header_groups:
        logger.warning(
            "[WIDMAN_PRICE_ITEMS_FAIL_REASON] reason=missing_headers missing=%s header_cells=%s html_head=%s",
            missing_header_groups,
            header_cells,
            (html_text or "")[:3000],
        )

    def idx(*needles: str) -> int | None:
        for needle in needles:
            needle_l = needle.lower()
            for i, header in enumerate(header_cells):
                if needle_l in header:
                    return i
        return None

    name_i = idx("наименование", "товар", "название")
    manufacturer_i = idx("производитель")
    expiry_i = idx("срок")
    def price_idx() -> int | None:
        excluded = ("стара", "скид", "розн", "old", "discount")
        preferred = ("цена с ндс", "цена, тг", "цена тг", "цена")
        for needle in preferred:
            needle_l = needle.lower()
            for i, header in enumerate(header_cells):
                if needle_l in header and not any(x in header for x in excluded):
                    return i
        return None

    price_i = price_idx()
    pack_i = idx("кол. в уп", "кол", "уп")
    min_order_i = idx("мин. заказ", "миним")
    stock_i = idx("остаток")
    if len(header_cells) >= 8 and name_i is None and price_i is None:
        name_i = 1
        manufacturer_i = 2
        expiry_i = 3
        price_i = 4
        pack_i = 5
        min_order_i = 6
        stock_i = 7

    out: list[WidmanPriceItem] = []
    for row_no, row_html in enumerate(rows[1:], start=1):
        cells = split_cells(row_html)

        def cell_text(i: int | None) -> str:
            if i is None or i < 0 or i >= len(cells):
                return ""
            return strip_tags(cells[i])

        product_name = cell_text(name_i)
        price = parse_decimal(cell_text(price_i))
        if not product_name and price is None:
            continue

        out.append(
            WidmanPriceItem(
                name=product_name,
                manufacturer=cell_text(manufacturer_i).lstrip(" -\u2010\u2011\u2012\u2013\u2014\u2015").strip(),
                expiry_date=cell_text(expiry_i) or None,
                price=price,
                pack_quantity=parse_decimal(cell_text(pack_i)),
                min_order=parse_decimal(cell_text(min_order_i)),
                stock=parse_decimal(cell_text(stock_i)),
                raw={
                    "rowNumber": row_no,
                    "headers": header_cells,
                    "cells": [strip_tags(c) for c in cells],
                    "priceColumn": price_i,
                    "priceHeader": header_cells[price_i] if price_i is not None and price_i < len(header_cells) else "",
                    "priceValue": cell_text(price_i),
                },
            )
        )
    return out


class WidmanClient:
    def __init__(
        self,
        *,
        login: str,
        password: str,
        auth_base_url: str = "https://1.provizor.kz",
        price_base_url: str = "https://prv.kz",
        login_path: str = "/pages/login",
        timeout: float = 60.0,
        max_retries: int = 3,
        retry_delay: float = 0.5,
        auth_mode: str = "auto",
    ) -> None:
        self.login_name = login
        self.password = password
        self.auth_base_url = auth_base_url.rstrip("/")
        self.price_base_url = price_base_url.rstrip("/")
        self.login_path = login_path or "/pages/login"
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.retry_delay = retry_delay
        auth_mode_l = (auth_mode or "auto").strip().lower()
        self.auth_mode = auth_mode_l if auth_mode_l in {"auto", "httpx", "playwright"} else "auto"
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/147.0.0.0 Safari/537.36"
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=timeout, write=30.0, pool=30.0),
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html, */*; q=0.01",
            },
        )
        self._logged_in = False
        self._last_verify_response = ""
        self._last_verify_url = ""
        self._last_verify_status: int | None = None

    def _cache_bust_params(self) -> dict[str, str]:
        return {"_": str(int(time.time() * 1000))}

    def _no_cache_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Cache-Control": "no-store, no-cache, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }
        if extra:
            headers.update(extra)
        return headers

    async def __aenter__(self) -> "WidmanClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def login(self) -> None:
        if self.auth_mode == "playwright":
            await self._login_playwright()
            return
        if self.auth_mode == "httpx":
            await self._login_httpx()
            return

        try:
            await self._login_httpx()
        except WidmanRedirectLoopError as e:
            logger.warning("Widman httpx login hit redirect loop, switching to Playwright: %s", e)
            await self._login_playwright()

    async def force_relogin(self) -> None:
        logger.info("Widman force relogin: clearing cookies before login")
        self._logged_in = False
        self._client.cookies.clear()
        await self.login()

    async def _login_httpx(self) -> None:
        logger.info("Widman login: opening %s", self.auth_base_url)
        start = await self._client.get(
            self.auth_base_url + "/",
            params=self._cache_bust_params(),
            headers=self._no_cache_headers(),
            follow_redirects=True,
        )
        start.raise_for_status()
        logger.info(
            "Widman login start: status=%s url=%s headers=%s html=%s cookies=%s",
            start.status_code,
            str(start.url),
            self._safe_headers(start.headers),
            (start.text or "")[:2000],
            self._cookie_snapshot(),
        )

        url = urljoin(self.auth_base_url + "/", self.login_path.lstrip("/"))
        payload = {"email": self.login_name, "password": self.password}
        logger.info("Widman login: POST %s with fields %s", url, [k for k in payload])
        res = await self._client.post(
            url,
            params=self._cache_bust_params(),
            data=payload,
            headers=self._no_cache_headers({
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self.auth_base_url,
                "Referer": self.auth_base_url + "/",
            }),
            follow_redirects=True,
        )
        logger.info(
            "Widman login response: status=%s url=%s headers=%s html=%s cookies=%s",
            res.status_code,
            str(res.url),
            self._safe_headers(res.headers),
            (res.text or "")[:2000],
            self._cookie_snapshot(),
        )
        if res.status_code >= 400:
            self._logged_in = False
            raise RuntimeError(f"Widman login failed: HTTP {res.status_code}. Response: {(res.text or '')[:3000]}")

        self._sync_phpsessid_to_price_domain()
        await self._run_autoredirect_flow()
        self._sync_phpsessid_to_price_domain()
        await self._open_counterparts_index()
        logger.info("Widman cookies after login warmup: %s", self._cookie_snapshot())

        if await self._verify_session():
            self._logged_in = True
            logger.info("Widman login: success")
            return

        self._logged_in = False
        verify_response = self._last_verify_response[:3000] if self._last_verify_response else ""
        raise RuntimeError(
            "Widman login returned HTTP "
            f"{res.status_code}, but session verification failed. "
            f"Verify URL: {self._last_verify_url or 'unknown'}, "
            f"verify status: {self._last_verify_status or 'unknown'}. "
            f"Verify response: {verify_response}"
        )

    async def _login_playwright(self) -> None:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError(
                "Widman Playwright auth requested, but playwright is not installed. "
                "Install backend dependency `playwright` and run `python -m playwright install chromium`."
            ) from e

        try:
            from playwright._impl._errors import TargetClosedError
        except Exception:
            TargetClosedError = Exception

        started_at = time.monotonic()
        login_timeout = min(max(float(self.timeout or 30.0), 30.0), 180.0)

        logger.info(
            "Widman Playwright login start: auth_base_url=%s timeout=%ss",
            self.auth_base_url,
            login_timeout,
        )

        playwright = None
        browser = None
        context = None
        page = None

        async def close_resource(name: str, obj: Any, method_name: str = "close") -> None:
            if obj is None:
                return
            try:
                if name == "page" and getattr(obj, "is_closed", lambda: False)():
                    logger.info("Widman Playwright resource already closed: %s", name)
                    return
                if name == "browser" and not getattr(obj, "is_connected", lambda: True)():
                    logger.info("Widman Playwright resource already closed: %s", name)
                    return
                method = getattr(obj, method_name, None)
                if method is None:
                    return
                await method()
                logger.info("Widman Playwright resource closed: %s", name)
            except TargetClosedError as e:
                logger.warning("Widman Playwright resource was already closed: %s error=%s", name, e)
            except Exception as e:
                logger.warning("Widman Playwright resource close failed: %s error=%s", name, e)

        try:
            playwright = await async_playwright().start()
            logger.info("Widman Playwright launch start")
            browser = await asyncio.wait_for(playwright.chromium.launch(headless=True), timeout=login_timeout)
            logger.info("Widman Playwright launch done in %.2fs", time.monotonic() - started_at)
            context = await browser.new_context(
                user_agent=self.user_agent,
                locale="ru-RU",
                viewport={"width": 1366, "height": 768},
            )
            page = await context.new_page()

            async def run_login() -> None:
                assert page is not None
                assert context is not None
                page.set_default_timeout(int(login_timeout * 1000))
                logger.info("Widman Playwright login page open: %s", self.auth_base_url)
                await page.goto(self.auth_base_url + "/", wait_until="domcontentloaded", timeout=int(login_timeout * 1000))
                modal = await self._playwright_open_login_form(page)
                await self._playwright_fill_login_form(modal)
                logger.info("Widman Playwright credentials submitted")
                await self._playwright_submit_login_form(modal)

                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    pass
                try:
                    await modal.wait_for(state="hidden", timeout=5000)
                except PlaywrightTimeoutError:
                    pass
                await page.wait_for_timeout(3000)

                if await self._playwright_login_error_text(page):
                    raise WidmanInvalidCredentialsError("Неверный логин или пароль")

                await self._playwright_wait_for_prv_session(context, page)
                await self._copy_playwright_cookies_to_httpx(context)
                logger.info("Widman Playwright token/cookies received: %s", self._cookie_snapshot())

            await asyncio.wait_for(run_login(), timeout=login_timeout)
        except WidmanInvalidCredentialsError:
            self._logged_in = False
            raise
        except (asyncio.TimeoutError, PlaywrightTimeoutError) as e:
            self._logged_in = False
            raise RuntimeError("Не удалось авторизоваться в Vidman: timeout при входе") from e
        except TargetClosedError as e:
            self._logged_in = False
            raise RuntimeError("Не удалось авторизоваться в Vidman") from e
        except Exception as e:
            self._logged_in = False
            msg = str(e) or e.__class__.__name__
            if "Target page, context or browser has been closed" in msg:
                raise RuntimeError("Не удалось авторизоваться в Vidman") from e
            raise
        finally:
            await close_resource("page", page)
            await close_resource("context", context)
            await close_resource("browser", browser)
            if playwright is not None:
                try:
                    await playwright.stop()
                    logger.info("Widman Playwright resources closed in %.2fs", time.monotonic() - started_at)
                except TargetClosedError as e:
                    logger.warning("Widman Playwright was already closed: %s", e)
                except Exception as e:
                    logger.warning("Widman Playwright stop failed: %s", e)

        self._sync_phpsessid_to_price_domain()
        logger.info("Widman cookies after Playwright login: %s", self._cookie_snapshot())
        if await self._verify_session():
            self._logged_in = True
            logger.info("Widman Playwright login finish: success elapsed=%.2fs", time.monotonic() - started_at)
            return

        self._logged_in = False
        verify_response = self._last_verify_response[:3000] if self._last_verify_response else ""
        raise RuntimeError(
            "Widman Playwright login completed, but session verification failed. "
            f"Verify URL: {self._last_verify_url or 'unknown'}, "
            f"verify status: {self._last_verify_status or 'unknown'}. "
            f"Verify response: {verify_response}"
        )

    async def _playwright_open_login_form(self, page: Any) -> Any:
        for selector in [
            "a.auth-btn:has-text('Войти')",
            "button:has-text('Войти')",
            "a:has-text('Войти')",
            "button:has-text('Кіру')",
            "a:has-text('Кіру')",
            "[data-target*='login']",
            "[data-bs-target*='login']",
        ]:
            locator = page.locator(selector).first
            try:
                if await locator.count():
                    await locator.click()
                    break
            except Exception:
                continue

        modal = page.locator(".modal:visible, #mAuth:visible, .modal-dialog:visible").first
        email = modal.locator("input[name='email'], input[name='login'], input[type='email'], input#email").first
        await email.wait_for(state="visible", timeout=10000)
        return modal

    async def _playwright_fill_login_form(self, modal: Any) -> None:
        email = modal.locator("input[name='email'], input[name='login'], input[type='email'], input#email").first
        password = modal.locator("input[name='password'], input[type='password'], input#password").first
        await email.fill(self.login_name)
        await password.fill(self.password)

    async def _playwright_submit_login_form(self, modal: Any) -> None:
        selectors = [
            "form:has(input[name='password']) button[type='submit']",
            "form:has(input[type='password']) button[type='submit']",
            "form:has(input[name='password']) input[type='submit']",
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Войти')",
            "button:has-text('Кіру')",
            "button:has-text('Login')",
        ]
        for selector in selectors:
            locator = modal.locator(selector).first
            try:
                if await locator.count():
                    await locator.click(force=True, timeout=10000)
                    return
            except Exception:
                continue

        password = modal.locator("input[name='password'], input[type='password'], input#password").first
        await password.press("Enter")

    async def _playwright_wait_for_prv_session(self, context: Any, page: Any) -> None:
        deadline = asyncio.get_running_loop().time() + max(self.timeout, 30.0)
        index_url = urljoin(self.price_base_url + "/", "counterparts/index")
        while asyncio.get_running_loop().time() < deadline:
            try:
                if "counterparts/index" not in page.url.lower():
                    await page.goto(index_url, wait_until="domcontentloaded", timeout=15000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page_url_l = page.url.lower()
                body_l = (await page.locator("body").inner_text(timeout=5000)).lower()
                if (
                    "counterparts/index" in page_url_l
                    and (
                        "price/see/" in body_l
                        or "контрагент" in body_l
                    )
                ):
                    cookies = await context.cookies([self.price_base_url])
                    if any(c.get("name", "").upper() == "PHPSESSID" and c.get("value") for c in cookies):
                        return
            except Exception:
                await asyncio.sleep(1.0)
                continue
        raise RuntimeError("Widman Playwright login did not open counterparts table on prv.kz")

    async def _copy_playwright_cookies_to_httpx(self, context: Any) -> None:
        cookies = await context.cookies([self.auth_base_url, self.price_base_url])
        for cookie in cookies:
            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue
            domain = (cookie.get("domain") or "").lstrip(".")
            path = cookie.get("path") or "/"
            if domain:
                self._client.cookies.set(name, value, domain=domain, path=path)
            else:
                self._client.cookies.set(name, value, path=path)

    async def _playwright_login_error_text(self, page: Any) -> str:
        selectors = ".alert-danger, .alert-error, .error, .help-block, [class*='error'], [role='alert']"
        try:
            for i in range(await page.locator(selectors).count()):
                text = normalize_text(await page.locator(selectors).nth(i).inner_text(timeout=1000))
                text_l = text.lower()
                if text and (
                    "невер" in text_l
                    or "ошиб" in text_l
                    or "incorrect" in text_l
                    or "invalid" in text_l
                    or "қате" in text_l
                ):
                    return text
        except Exception:
            return ""
        return ""

    async def get_price_lists(self) -> list[WidmanPriceList]:
        await self._ensure_logged_in()
        html_text = await self._post_html(
            "/counterparts/view",
            data={"str": ""},
            referer=urljoin(self.price_base_url + "/", "counterparts/index"),
            operation="load counterparts",
        )
        logger.info("[WIDMAN_HTML_PREVIEW] operation=price_lists html=%s", html_text[:2000])
        lists = parse_price_lists(html_text)
        logger.info("Widman price lists parsed: %s", len(lists))
        if not lists:
            logger.error("Widman: price list parser returned 0. HTML head: %s", html_text[:5000])
            raise RuntimeError("Widman: список прайсов не найден, возможно проблема с сессией или парсером")
        return lists

    async def get_price_items(self, price_id: int | str) -> list[WidmanPriceItem]:
        await self._ensure_logged_in()
        price_id_s = str(price_id)
        main_id = await self._resolve_price_main_id(price_id_s)
        first_html = await self._load_items_page(main_id, 1, referer_price_id=price_id_s)
        logger.info("[WIDMAN_HTML_PREVIEW] operation=price_items price_list_id=%s main_id=%s page=1 html=%s", price_id_s, main_id, first_html[:2000])
        max_page = parse_max_page(first_html)
        items = parse_price_items(first_html)
        logger.info("Widman price %s main_id %s page 1/%s: %s items", price_id_s, main_id, max_page, len(items))

        for page in range(2, max_page + 1):
            page_html = await self._load_items_page(main_id, page, referer_price_id=price_id_s)
            logger.info("[WIDMAN_HTML_PREVIEW] operation=price_items price_list_id=%s main_id=%s page=%s html=%s", price_id_s, main_id, page, page_html[:2000])
            page_items = parse_price_items(page_html)
            logger.info("Widman price %s main_id %s page %s/%s: %s items", price_id_s, main_id, page, max_page, len(page_items))
            items.extend(page_items)
        if not items:
            text_l = (first_html or "").lower()
            reason = "empty_parse"
            if "method not found" in text_l or ("404" in text_l and "not found" in text_l):
                reason = "method_not_found"
            elif self._looks_like_login(first_html):
                reason = "login_form"
            elif "<table" in text_l:
                reason = "empty_or_unrecognized_table"
            logger.warning(
                "[WIDMAN_PRICE_ITEMS_FAIL_REASON] price_list_id=%s reason=%s parsed_items_count=0 html_head=%s",
                price_id_s,
                reason,
                (first_html or "")[:3000],
            )
        return items

    async def fetch_all(self) -> dict[str, list[dict[str, Any]]]:
        price_lists = []
        for price_list in await self.get_price_lists():
            items = await self.get_price_items(price_list.id)
            price_lists.append(
                {
                    "id": price_list.id,
                    "name": price_list.name,
                    "enabled": price_list.enabled,
                    "counterpart_id": price_list.counterpart_id,
                    "updated_at": price_list.updated_at,
                    "city": price_list.city,
                    "phone": price_list.phone,
                    "items": [
                        {
                            "name": item.name,
                            "manufacturer": item.manufacturer,
                            "expiry_date": item.expiry_date,
                            "price": float(item.price) if item.price is not None else None,
                            "pack_quantity": float(item.pack_quantity) if item.pack_quantity is not None else None,
                            "min_order": float(item.min_order) if item.min_order is not None else None,
                            "stock": float(item.stock) if item.stock is not None else None,
                        }
                        for item in items
                    ],
                }
            )
        return {"price_lists": price_lists}

    async def _ensure_logged_in(self) -> None:
        if not self._logged_in:
            await self.login()

    async def _verify_session(self) -> bool:
        url = urljoin(self.price_base_url + "/", "counterparts/view")
        payload = {"str": ""}
        root_url = self.price_base_url + "/"
        referer = urljoin(self.price_base_url + "/", "counterparts/index")
        verify_headers = self._no_cache_headers(
            {
                "X-Requested-With": "XMLHttpRequest",
                "Referer": referer,
                "Origin": self.price_base_url,
                "Content-Type": "application/x-www-form-urlencoded",
            }
        )

        async def post_verify() -> httpx.Response:
            logger.info(
                "Widman verify request: method=POST url=%s payload=%s cookies_before=%s",
                url,
                payload,
                self._cookie_snapshot(),
            )
            return await self._client.post(
                url,
                data=payload,
                headers=verify_headers,
                follow_redirects=False,
            )

        try:
            root_res = await self._client.get(
                root_url,
                headers=self._no_cache_headers(),
                follow_redirects=True,
            )
            logger.info(
                "[WIDMAN_VERIFY_STEP] step=root url=%s status=%s is_404=%s",
                str(root_res.url),
                root_res.status_code,
                root_res.status_code == 404,
            )
            logger.info(
                "Widman verify root open: status=%s url=%s headers=%s html=%s cookies=%s",
                root_res.status_code,
                str(root_res.url),
                self._safe_headers(root_res.headers),
                (root_res.text or "")[:2000],
                self._cookie_snapshot(),
            )
            index_res = await self._client.get(
                referer,
                headers=self._no_cache_headers(),
                follow_redirects=True,
            )
            logger.info(
                "[WIDMAN_VERIFY_STEP] step=index url=%s status=%s is_404=%s",
                str(index_res.url),
                index_res.status_code,
                index_res.status_code == 404,
            )
            logger.info(
                "Widman verify index open: status=%s url=%s headers=%s html=%s cookies=%s",
                index_res.status_code,
                str(index_res.url),
                self._safe_headers(index_res.headers),
                (index_res.text or "")[:2000],
                self._cookie_snapshot(),
            )

            res = await post_verify()
            self._last_verify_url = str(res.url)
            self._last_verify_status = res.status_code
            self._last_verify_response = res.text or ""
            logger.info(
                "[WIDMAN_VERIFY_STEP] step=view url=%s status=%s is_404=%s",
                str(res.url),
                res.status_code,
                res.status_code == 404,
            )
            logger.info(
                "Widman verify response: status=%s url=%s headers=%s html=%s cookies_after=%s",
                res.status_code,
                str(res.url),
                self._safe_headers(res.headers),
                (res.text or "")[:3000],
                self._cookie_snapshot(),
            )
            if res.status_code in {301, 302, 303, 307, 308} or self._looks_like_redirect_flow(res.text, str(res.url)):
                logger.info(
                    "Widman verify POST needs redirect/session warmup: status=%s location=%s",
                    res.status_code,
                    res.headers.get("location", ""),
                )
                await self._run_autoredirect_flow()
                await self._open_counterparts_index()
                res = await post_verify()
                self._last_verify_url = str(res.url)
                self._last_verify_status = res.status_code
                self._last_verify_response = res.text or ""
                logger.info(
                    "[WIDMAN_VERIFY_STEP] step=view url=%s status=%s is_404=%s",
                    str(res.url),
                    res.status_code,
                    res.status_code == 404,
                )
                logger.info(
                    "Widman verify retry response: status=%s url=%s headers=%s html=%s cookies_after=%s",
                    res.status_code,
                    str(res.url),
                    self._safe_headers(res.headers),
                    (res.text or "")[:3000],
                    self._cookie_snapshot(),
                )
            if res.status_code in {401, 403}:
                return False

            text_l = (res.text or "").lower()
            method_not_found = "method not found" in text_l or ("404" in text_l and "not found" in text_l)
            has_price_links = "price/see/" in text_l
            has_supplier_name = "supplier_name" in text_l
            has_table = "<table" in text_l
            has_counterpart = "контрагент" in text_l or "РєРѕРЅС‚СЂР°РіРµРЅС‚" in text_l
            has_counterpart_table = has_table and has_supplier_name
            ok = res.status_code < 400 and not method_not_found and has_price_links and has_supplier_name and has_table
            logger.info(
                "[WIDMAN_VERIFY] method=POST url=/counterparts/view status=%s has_price_links=%s has_counterpart_table=%s ok=%s",
                res.status_code,
                has_price_links,
                has_counterpart_table,
                ok,
            )
            logger.info(
                "Widman session verification: status=%s ok=%s table=%s price_link=%s counterpart=%s supplier_name=%s method_not_found=%s cookies=%s",
                res.status_code,
                ok,
                has_table,
                has_price_links,
                has_counterpart,
                has_supplier_name,
                method_not_found,
                self._cookie_snapshot(),
            )
            if not ok:
                logger.warning("Widman session verification failed. HTML head: %s", (res.text or "")[:3000])
            return ok
        except WidmanRedirectLoopError:
            raise
        except Exception as e:
            logger.warning("Widman session verification failed: %s", e)
            return False

    async def _verify_session_legacy(self) -> bool:
        url = urljoin(self.price_base_url + "/", "counterparts/view")
        payload = {"str": ""}
        referer = urljoin(self.price_base_url + "/", "counterparts/index")
        try:
            logger.info(
                "Widman verify request: url=%s payload=%s cookies_before=%s",
                url,
                payload,
                self._cookie_snapshot(),
            )
            res = await self._client.post(
                url,
                data=payload,
                headers=self._ajax_headers(referer=referer),
                follow_redirects=True,
            )
            self._last_verify_url = str(res.url)
            self._last_verify_status = res.status_code
            self._last_verify_response = res.text or ""
            logger.info(
                "Widman verify response: status=%s url=%s headers=%s html=%s cookies_after=%s",
                res.status_code,
                str(res.url),
                self._safe_headers(res.headers),
                (res.text or "")[:3000],
                self._cookie_snapshot(),
            )
            if self._looks_like_redirect_flow(res.text, str(res.url)):
                logger.info("Widman verify returned redirect flow marker, running autoredirect and retrying verify once")
                await self._run_autoredirect_flow()
                await self._open_counterparts_index()
                logger.info(
                    "Widman verify retry request: url=%s payload=%s cookies_before=%s",
                    url,
                    payload,
                    self._cookie_snapshot(),
                )
                res = await self._client.post(
                    url,
                    data=payload,
                    headers=self._ajax_headers(referer=referer),
                    follow_redirects=True,
                )
                self._last_verify_url = str(res.url)
                self._last_verify_status = res.status_code
                self._last_verify_response = res.text or ""
                logger.info(
                    "Widman verify retry response: status=%s url=%s headers=%s html=%s cookies_after=%s",
                    res.status_code,
                    str(res.url),
                    self._safe_headers(res.headers),
                    (res.text or "")[:3000],
                    self._cookie_snapshot(),
                )
            if res.status_code in {401, 403}:
                return False
            text_l = (res.text or "").lower()
            ok = res.status_code < 400 and (
                "<table" in text_l
                or "price/see/" in text_l
                or "контрагент" in text_l
            )
            logger.info(
                "Widman session verification: status=%s ok=%s table=%s price_link=%s counterpart=%s cookies=%s",
                res.status_code,
                ok,
                "<table" in text_l,
                "price/see/" in text_l,
                "контрагент" in text_l,
                self._cookie_snapshot(),
            )
            if not ok:
                logger.warning("Widman session verification failed. HTML head: %s", (res.text or "")[:3000])
            return ok
        except WidmanRedirectLoopError:
            raise
        except Exception as e:
            logger.warning("Widman session verification failed: %s", e)
            return False

    async def _resolve_price_main_id(self, price_id: str) -> str:
        url = urljoin(self.price_base_url + "/", f"price/see/{price_id}")
        res = await self._client.get(
            url,
            headers=self._no_cache_headers({"Referer": urljoin(self.price_base_url + "/", "counterparts/index")}),
            follow_redirects=True,
        )
        if res.status_code in {401, 403} or self._looks_like_login(res.text):
            self._logged_in = False
            await self.login()
            res = await self._client.get(
                url,
                headers=self._no_cache_headers({"Referer": urljoin(self.price_base_url + "/", "counterparts/index")}),
                follow_redirects=True,
            )
        res.raise_for_status()
        html_text = res.text or ""
        match = re.search(r"\bmain_id\s*:\s*(\d+)", html_text)
        main_id = match.group(1) if match else price_id
        logger.info(
            "[WIDMAN_PRICE_MAIN_ID] price_list_id=%s main_id=%s resolved=%s",
            price_id,
            main_id,
            main_id != price_id,
        )
        return main_id

    async def _load_items_page(self, main_id: str, page: int, *, referer_price_id: str | None = None) -> str:
        data: dict[str, Any] = {"main_id": main_id}
        if page > 1:
            data["pageCurrent"] = page
        return await self._post_html(
            "/price/see_view",
            data=data,
            referer=urljoin(self.price_base_url + "/", f"price/see/{referer_price_id or main_id}"),
            operation=f"load price {referer_price_id or main_id} main_id {main_id} page {page}",
        )

    async def _post_html(
        self,
        path: str,
        *,
        data: dict[str, Any],
        referer: str,
        operation: str,
    ) -> str:
        url = urljoin(self.price_base_url + "/", path.lstrip("/"))
        path_key = path.lstrip("/")
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info("Widman cookies before request %s: %s", operation, self._cookie_snapshot())
                request_data = dict(data)
                if path_key == "price/see_view":
                    request_data.update(self._cache_bust_params())
                res = await self._client.post(
                    url,
                    data=request_data,
                    headers=self._ajax_headers(referer=referer),
                    follow_redirects=True,
                )
                if path_key == "price/see_view":
                    html_head = (res.text or "")[:3000]
                    logger.info(
                        "[WIDMAN_PRICE_ITEMS_RAW] price_list_id=%s status=%s url=%s payload=%s html_head=%s",
                        data.get("main_id", ""),
                        res.status_code,
                        str(res.url),
                        request_data,
                        html_head,
                    )
                    text_l = (res.text or "").lower()
                    fail_reason = ""
                    if "method not found" in text_l or ("404" in text_l and "not found" in text_l):
                        fail_reason = "method_not_found"
                    elif self._looks_like_login(res.text):
                        fail_reason = "login_form"
                    elif "<table" in text_l and not re.search(r"<tr\b[^>]*>.*?</tr>", res.text or "", flags=re.I | re.S):
                        fail_reason = "empty_table"
                    if fail_reason:
                        logger.warning(
                            "[WIDMAN_PRICE_ITEMS_FAIL_REASON] price_list_id=%s reason=%s status=%s url=%s html_head=%s",
                            data.get("main_id", ""),
                            fail_reason,
                            res.status_code,
                            str(res.url),
                            html_head,
                        )
                if res.status_code in {401, 403} or self._looks_like_login(res.text):
                    logger.warning("Widman %s: session expired, relogin", operation)
                    self._logged_in = False
                    await self.login()
                    continue
                if self._looks_like_redirect_flow(res.text, str(res.url)):
                    logger.warning("Widman %s: redirect flow required, running autoredirect", operation)
                    try:
                        await self._run_autoredirect_flow()
                    except WidmanRedirectLoopError:
                        if self.auth_mode == "auto":
                            logger.warning("Widman %s: redirect loop, refreshing session with Playwright", operation)
                            self._logged_in = False
                            await self._login_playwright()
                            continue
                        raise
                    await self._open_counterparts_index()
                    continue
                res.raise_for_status()
                logger.info("[WIDMAN_HTML_PREVIEW] operation=%s html=%s", operation, res.text[:2000])
                return res.text
            except (httpx.HTTPError, RuntimeError) as e:
                last_error = e
                logger.warning("Widman %s failed, attempt %s/%s: %s", operation, attempt, self.max_retries, e)
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_delay * attempt)
        raise RuntimeError(f"Widman {operation} failed after {self.max_retries} attempts: {last_error}")

    def _ajax_headers(self, *, referer: str) -> dict[str, str]:
        return self._no_cache_headers({
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.price_base_url,
            "Referer": referer,
        })

    async def _prime_price_domain(self, stage: str) -> None:
        try:
            res = await self._client.get(
                self.price_base_url + "/",
                headers=self._no_cache_headers(),
                follow_redirects=True,
            )
            logger.info(
                "Widman prime %s %s: HTTP %s url=%s html=%s cookies=%s",
                stage,
                self.price_base_url,
                res.status_code,
                str(res.url),
                (res.text or "")[:1000],
                self._cookie_snapshot(),
            )
        except Exception as e:
            logger.warning("Widman prime %s failed: %s", stage, e)

    async def _run_autoredirect_flow(self) -> None:
        url = urljoin(self.auth_base_url + "/", "pages/autoredirect/")
        await self._get_and_follow_js_redirects(url, stage="autoredirect")

    async def _open_counterparts_index(self) -> None:
        url = urljoin(self.price_base_url + "/", "counterparts/index")
        try:
            logger.info("Widman counterparts index request: url=%s cookies_before=%s", url, self._cookie_snapshot())
            res = await self._client.get(
                url,
                headers=self._no_cache_headers(),
                follow_redirects=True,
            )
            logger.info(
                "Widman counterparts index response: status=%s url=%s headers=%s html=%s cookies_after=%s",
                res.status_code,
                str(res.url),
                self._safe_headers(res.headers),
                (res.text or "")[:3000],
                self._cookie_snapshot(),
            )
            if self._looks_like_redirect_flow(res.text, str(res.url)):
                logger.info("Widman counterparts index returned redirect flow marker, following it")
                await self._follow_js_redirects_from_response(res, stage="counterparts-index")
        except Exception as e:
            logger.warning("Widman counterparts index failed: %s", e)

    async def _get_and_follow_js_redirects(self, url: str, *, stage: str, max_steps: int = 5) -> httpx.Response:
        current_url = url
        last_res: httpx.Response | None = None
        for step in range(1, max_steps + 1):
            logger.info(
                "Widman redirect step %s/%s %s: GET %s cookies_before=%s",
                step,
                max_steps,
                stage,
                current_url,
                self._cookie_snapshot(),
            )
            res = await self._client.get(
                current_url,
                headers=self._no_cache_headers(),
                follow_redirects=True,
            )
            last_res = res
            logger.info(
                "Widman redirect step %s/%s %s response: status=%s url=%s headers=%s html=%s cookies_after=%s",
                step,
                max_steps,
                stage,
                res.status_code,
                str(res.url),
                self._safe_headers(res.headers),
                (res.text or "")[:3000],
                self._cookie_snapshot(),
            )
            next_url = self._extract_js_redirect_url(res.text, str(res.url))
            if not next_url:
                return res
            current_url = next_url
        if last_res is None:
            raise RuntimeError(f"Widman redirect flow {stage} did not run")
        logger.warning("Widman redirect flow %s stopped after %s JS redirects", stage, max_steps)
        raise WidmanRedirectLoopError(f"Widman redirect flow {stage} stopped after {max_steps} JS redirects")

    async def _follow_js_redirects_from_response(self, res: httpx.Response, *, stage: str) -> httpx.Response:
        next_url = self._extract_js_redirect_url(res.text, str(res.url))
        if not next_url:
            return res
        return await self._get_and_follow_js_redirects(next_url, stage=stage)

    def _sync_phpsessid_to_price_domain(self) -> None:
        auth_phpsessid = None
        auth_domain = ""
        fallback_phpsessid = None
        fallback_domain = ""
        for cookie in self._client.cookies.jar:
            if cookie.name.upper() == "PHPSESSID" and cookie.value:
                if "prv.kz" not in cookie.domain:
                    auth_phpsessid = cookie.value
                    auth_domain = cookie.domain
                else:
                    fallback_phpsessid = cookie.value
                    fallback_domain = cookie.domain
        phpsessid = fallback_phpsessid or auth_phpsessid
        source_domain = fallback_domain or auth_domain
        if phpsessid:
            self._client.cookies.set("PHPSESSID", phpsessid, domain="prv.kz", path="/")
            logger.info("Widman copied PHPSESSID from domain=%s to prv.kz", source_domain)

    def _cookie_snapshot(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for cookie in self._client.cookies.jar:
            value = cookie.value or ""
            out.append(
                {
                    "name": cookie.name,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "value": value[:6] + "..." if value else "",
                }
            )
        return out

    def _extract_js_redirect_url(self, text: str | None, base_url: str) -> str | None:
        text = text or ""
        patterns = [
            r"window\.location\.href\s*=\s*['\"]([^'\"]+)['\"]",
            r"window\.location\s*=\s*['\"]([^'\"]+)['\"]",
            r"location\.href\s*=\s*['\"]([^'\"]+)['\"]",
            r"location\.replace\(\s*['\"]([^'\"]+)['\"]\s*\)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.I)
            if match:
                return urljoin(base_url, html.unescape(match.group(1)))
        return None

    def _looks_like_redirect_flow(self, text: str | None, url: str = "") -> bool:
        text_l = (text or "").lower()
        url_l = (url or "").lower()
        return (
            "users/out" in text_l
            or "pages/autoredirect" in text_l
            or "users/out" in url_l
            or "pages/autoredirect" in url_l
            or self._extract_js_redirect_url(text, url or self.auth_base_url) is not None
        )

    @staticmethod
    def _safe_headers(headers: httpx.Headers) -> dict[str, str]:
        safe: dict[str, str] = {}
        for key, value in headers.items():
            if key.lower() in {"set-cookie", "cookie", "authorization"}:
                safe[key] = "<masked>"
            else:
                safe[key] = value
        return safe

    @staticmethod
    def _looks_like_login(text: str | None) -> bool:
        text_l = (text or "").lower()
        if not text_l:
            return False
        if "<table" in text_l or "price/see/" in text_l:
            return False
        if "контрагент" in text_l:
            return False
        has_password = "password" in text_l or "пароль" in text_l
        has_login = "login" in text_l or "email" in text_l or "войти" in text_l
        has_price_data = "/price/see/" in text_l or "paginationSet(" in text_l or "контрагент" in text_l
        return has_password and has_login and not has_price_data
