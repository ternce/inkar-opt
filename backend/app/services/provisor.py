from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


class ProvisorResponseRows(list):
    def __init__(self, rows=None, *, benchmark: dict | None = None):
        super().__init__(rows or [])
        self.benchmark = benchmark or {}


def process_memory_snapshot() -> dict[str, float | None]:
    rss_mb: float | None = None
    try:
        import psutil  # type: ignore

        rss_mb = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        if os.name == "posix":
            try:
                with open("/proc/self/status", "r", encoding="utf-8") as fh:
                    for line in fh:
                        if line.startswith("VmRSS:"):
                            parts = line.split()
                            if len(parts) >= 2:
                                rss_mb = float(parts[1]) / 1024
                            break
            except Exception:
                rss_mb = None
    return {"rss_mb": round(rss_mb, 2) if rss_mb is not None else None}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def provisor_http_limits(max_parallel_plk: int = 1) -> httpx.Limits:
    """Conservative pool sized for the configured in-account PLK concurrency."""

    try:
        parallel = max(1, int(max_parallel_plk))
    except Exception:
        parallel = 1
    return httpx.Limits(
        max_connections=max(2, min(8, parallel + 1)),
        max_keepalive_connections=max(1, min(4, parallel)),
        keepalive_expiry=_env_float("PROVISOR_HTTP_KEEPALIVE_EXPIRY_SECONDS", 30.0),
    )


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _jwt_exp_unix(token: str) -> int | None:
    """Returns exp from JWT (unix seconds) without verifying signature."""

    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def _is_expired(exp_unix: int | None, skew_seconds: int = 30) -> bool:
    if exp_unix is None:
        return False
    return time.time() >= (exp_unix - skew_seconds)


@dataclass
class ProvisorTokens:
    access: str
    refresh: str
    access_exp_unix: int | None


_tokens_by_key: dict[tuple[str, str], ProvisorTokens] = {}
_lock = asyncio.Lock()


class ProvisorAuthError(RuntimeError):
    pass


async def _create_tokens(
    *,
    client: httpx.AsyncClient,
    login: str,
    password: str,
    timeout: httpx.Timeout | None = None,
) -> ProvisorTokens:
    resp = await client.post(
        "/Token/CreateAll",
        json={
            "login": login,
            "password": password,
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise ProvisorAuthError(f"Token/CreateAll failed: HTTP {resp.status_code}: {resp.text}")

    data = resp.json()
    access = (data.get("accessToken") or "").strip()
    refresh = (data.get("refreshToken") or "").strip()
    if not access or not refresh:
        raise ProvisorAuthError("Token/CreateAll returned empty tokens")

    return ProvisorTokens(access=access, refresh=refresh, access_exp_unix=_jwt_exp_unix(access))


async def _update_tokens(
    *,
    client: httpx.AsyncClient,
    access: str,
    refresh: str,
    timeout: httpx.Timeout | None = None,
) -> ProvisorTokens:
    resp = await client.post(
        "/Token/Update",
        json={
            "Access": access,
            "Refresh": refresh,
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise ProvisorAuthError(f"Token/Update failed: HTTP {resp.status_code}: {resp.text}")

    data = resp.json()
    new_access = (data.get("accessToken") or "").strip()
    new_refresh = (data.get("refreshToken") or "").strip()
    if not new_access or not new_refresh:
        raise ProvisorAuthError("Token/Update returned empty tokens")

    return ProvisorTokens(access=new_access, refresh=new_refresh, access_exp_unix=_jwt_exp_unix(new_access))


async def get_access_token(
    *,
    base_url: str,
    login: str | None,
    password: str | None,
    timeout_seconds: float = 30.0,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Returns a valid access token. Caches and refreshes tokens in-memory."""

    parsed = urlparse(base_url)
    if parsed.path not in ("", "/"):
        raise ProvisorAuthError(
            "PROVISOR_BASE_URL must be the API origin (e.g., https://api.provisor.kz) without a path"
        )

    login_s = (login or "").strip()
    password_s = (password or "").strip()
    if not login_s or not password_s:
        raise ProvisorAuthError("PROVISOR_LOGIN/PROVISOR_PASSWORD is not configured")

    cache_key = (base_url.rstrip("/"), login_s)
    async with _lock:
        cached = _tokens_by_key.get(cache_key)
        if cached and cached.access and not _is_expired(cached.access_exp_unix):
            logger.info("[PROVISOR_AUTH_TIMING] login=%s cache_hit=true auth_elapsed_sec=0.0", login_s)
            return cached.access

        timeout = httpx.Timeout(connect=10.0, read=timeout_seconds, write=30.0, pool=30.0)

        async def _fetch_tokens(auth_client: httpx.AsyncClient) -> str:
            if cached and cached.access and cached.refresh:
                try:
                    auth_started_at = time.perf_counter()
                    updated = await _update_tokens(
                        client=auth_client,
                        access=cached.access,
                        refresh=cached.refresh,
                        timeout=timeout,
                    )
                    _tokens_by_key[cache_key] = updated
                    logger.info(
                        "[PROVISOR_AUTH_TIMING] login=%s cache_hit=false token_refresh=true auth_elapsed_sec=%s",
                        login_s,
                        round(time.perf_counter() - auth_started_at, 3),
                    )
                    return updated.access
                except Exception:
                    _tokens_by_key.pop(cache_key, None)

            auth_started_at = time.perf_counter()
            created = await _create_tokens(
                client=auth_client,
                login=login_s,
                password=password_s,
                timeout=timeout,
            )
            _tokens_by_key[cache_key] = created
            logger.info(
                "[PROVISOR_AUTH_TIMING] login=%s cache_hit=false token_refresh=false auth_elapsed_sec=%s",
                login_s,
                round(time.perf_counter() - auth_started_at, 3),
            )
            return created.access

        if client is not None:
            return await _fetch_tokens(client)

        async with httpx.AsyncClient(base_url=base_url, timeout=timeout, limits=provisor_http_limits(1)) as auth_client:
            return await _fetch_tokens(auth_client)


async def get_filials_by_context(
    *,
    base_url: str,
    login: str | None,
    password: str | None,
    timeout_seconds: float = 60.0,
    force_refresh: bool = False,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Fetches /Distributor/GetFilialsByContext returning available Provisor price lists."""

    timeout = httpx.Timeout(connect=10.0, read=timeout_seconds, write=30.0, pool=30.0)
    close_client = client is None
    if client is None:
        client = httpx.AsyncClient(base_url=base_url, timeout=timeout, limits=provisor_http_limits(1))
    try:
        token = await get_access_token(
            base_url=base_url,
            login=login,
            password=password,
            timeout_seconds=timeout_seconds,
            client=client,
        )

        async def _call(access_token: str) -> httpx.Response:
            headers = {"Authorization": f"Bearer {access_token}"}
            if force_refresh:
                headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache"})
            return await client.get(
                "/Distributor/GetFilialsByContext",
                params={"_ts": int(time.time() * 1000)} if force_refresh else None,
                headers=headers,
                timeout=timeout,
            )

        request_started_at = time.perf_counter()
        resp = await _call(token)
        request_elapsed_sec = round(time.perf_counter() - request_started_at, 3)
        if resp.status_code in (401, 403):
            cache_key = (base_url.rstrip("/"), (login or "").strip())
            async with _lock:
                _tokens_by_key.pop(cache_key, None)
            token2 = await get_access_token(
                base_url=base_url,
                login=login,
                password=password,
                timeout_seconds=timeout_seconds,
                client=client,
            )
            request_started_at = time.perf_counter()
            resp = await _call(token2)
            request_elapsed_sec += round(time.perf_counter() - request_started_at, 3)

        if resp.status_code >= 400:
            raise ProvisorAuthError(f"Distributor/GetFilialsByContext failed: HTTP {resp.status_code}: {resp.text}")

        try:
            decode_started_at = time.perf_counter()
            data = resp.json()
            decode_elapsed_sec = round(time.perf_counter() - decode_started_at, 3)
        except Exception:
            raise ProvisorAuthError(f"Distributor/GetFilialsByContext returned invalid JSON: {resp.text}")
        if not isinstance(data, list):
            raise ProvisorAuthError("Distributor/GetFilialsByContext returned non-list JSON")
        rows = [x for x in data if isinstance(x, dict)]
        try:
            response_bytes = len(resp.content)
        except Exception:
            response_bytes = None
        logger.info("Provisor filials loaded: %s", len(data))
        logger.info(
            "[PROVISOR_FILIAL_LIST_TIMING] login=%s http_status=%s filials_raw=%s filials_valid=%s response_size_mb=%s filial_list_request_elapsed_sec=%s filial_list_parse_elapsed_sec=%s",
            (login or "").strip(),
            resp.status_code,
            len(data),
            len(rows),
            round(response_bytes / (1024 * 1024), 3) if response_bytes is not None else None,
            request_elapsed_sec,
            decode_elapsed_sec,
        )
        return rows
    finally:
        if close_client:
            await client.aclose()


async def get_prices_by_filial_id(
    *,
    base_url: str,
    login: str | None,
    password: str | None,
    filial_id: int,
    timeout_seconds: float = 30.0,
    force_refresh: bool = False,
    client: httpx.AsyncClient | None = None,
    connection_reuse_scope: str = "single_request",
) -> list[dict]:
    """Fetches /Price/GetByFilialId?filialId=... returning JSON list."""

    if filial_id <= 0:
        raise ValueError("filial_id must be positive")

    started_at = time.perf_counter()
    started_wall = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    stage = "login"
    auth_wait_sec = 0.0
    http_request_sec = 0.0
    response_bytes: int | None = None
    json_decode_sec = 0.0
    pool_wait_sec: float | None = None
    http_attempt_count = 0
    auth_retry_count = 0
    read_timeout = timeout_seconds or _env_float("PROVISOR_PRICE_READ_TIMEOUT_SECONDS", 30.0)
    timeout = httpx.Timeout(
        connect=_env_float("PROVISOR_PRICE_CONNECT_TIMEOUT_SECONDS", 10.0),
        read=read_timeout,
        write=10.0,
        pool=10.0,
    )

    close_client = client is None
    if client is None:
        client = httpx.AsyncClient(base_url=base_url, timeout=timeout, limits=provisor_http_limits(1))
    try:
        try:
            auth_started_at = time.perf_counter()
            token = await get_access_token(
                base_url=base_url,
                login=login,
                password=password,
                timeout_seconds=timeout_seconds,
                client=client,
            )
            auth_wait_sec += time.perf_counter() - auth_started_at
        except (asyncio.TimeoutError, httpx.TimeoutException) as e:
            elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.warning(
                "[TIMEOUT_DEBUG] price_id=%s price_name=%s source=%s started_at=%s elapsed=%s stage=%s exception=%r",
                filial_id,
                "",
                "provisor",
                started_wall,
                elapsed_ms,
                stage,
                e,
            )
            raise asyncio.TimeoutError(f"Provisor filial {filial_id} login timeout > {timeout_seconds}s")

        async def _call(access_token: str) -> httpx.Response:
            headers = {"Authorization": f"Bearer {access_token}"}
            if force_refresh:
                headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache"})

            return await client.get(
                "/Price/GetByFilialId",
                params={
                    "filialId": filial_id,
                    "_ts": int(time.time() * 1000),
                } if force_refresh else {
                    "filialId": filial_id,
                },
                headers=headers,
                timeout=timeout,
            )

        try:
            stage = "get_price_items"
            http_started_at = time.perf_counter()
            http_attempt_count += 1
            resp = await asyncio.wait_for(_call(token), timeout=timeout_seconds)
            http_request_sec += time.perf_counter() - http_started_at
            http_elapsed_ms = round(http_request_sec * 1000, 2)

            if resp.status_code in (401, 403):
                stage = "login"
                cache_key = (base_url.rstrip("/"), (login or "").strip())
                async with _lock:
                    _tokens_by_key.pop(cache_key, None)

                auth_started_at = time.perf_counter()
                auth_retry_count += 1
                token2 = await get_access_token(
                    base_url=base_url,
                    login=login,
                    password=password,
                    timeout_seconds=timeout_seconds,
                    client=client,
                )
                auth_wait_sec += time.perf_counter() - auth_started_at

                stage = "get_price_items"
                http_started_at = time.perf_counter()
                http_attempt_count += 1
                resp = await asyncio.wait_for(_call(token2), timeout=timeout_seconds)
                http_request_sec += time.perf_counter() - http_started_at
                http_elapsed_ms = round(http_request_sec * 1000, 2)

        except httpx.PoolTimeout as e:
            stage = "connection_pool_wait"
            pool_wait_sec = timeout.pool
            elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.warning(
                "[TIMEOUT_DEBUG] price_id=%s price_name=%s source=%s started_at=%s elapsed=%s stage=%s exception=%r",
                filial_id,
                "",
                "provisor",
                started_wall,
                elapsed_ms,
                stage,
                e,
            )
            raise asyncio.TimeoutError(f"Provisor filial {filial_id} connection pool timeout > {timeout.pool}s")
        except (asyncio.TimeoutError, httpx.TimeoutException) as e:
            elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
            logger.warning(
                "[TIMEOUT_DEBUG] price_id=%s price_name=%s source=%s started_at=%s elapsed=%s stage=%s exception=%r",
                filial_id,
                "",
                "provisor",
                started_wall,
                elapsed_ms,
                stage,
                e,
            )
            logger.warning(
                "Provisor filial %s skipped: timeout > %ss",
                filial_id,
                timeout_seconds,
            )
            raise asyncio.TimeoutError(f"Provisor filial {filial_id} timeout > {timeout_seconds}s")

        if resp.status_code >= 400:
            raise ProvisorAuthError(
                f"Price/GetByFilialId failed: HTTP {resp.status_code}: {resp.text}"
            )

        try:
            stage = "parsing"
            try:
                response_bytes = len(resp.content)
            except Exception:
                response_bytes = None
            decode_started_at = time.perf_counter()
            data = resp.json()
            json_decode_sec = time.perf_counter() - decode_started_at
            decode_elapsed_ms = round(json_decode_sec * 1000, 2)
        except Exception:
            raise ProvisorAuthError(
                f"Price/GetByFilialId returned invalid JSON: {resp.text}"
            )

        if not isinstance(data, list):
            raise ProvisorAuthError("Price/GetByFilialId returned non-list JSON")

        memory_after_decode = process_memory_snapshot()
        logger.info(
            "[PROVISOR_PLK_FETCH_TIMING] filial_id=%s rows=%s response_size_mb=%s http_fetch_elapsed_ms=%s json_decode_elapsed_ms=%s rss_mb=%s",
            filial_id,
            len(data),
            round(response_bytes / (1024 * 1024), 3) if response_bytes is not None else None,
            http_elapsed_ms,
            decode_elapsed_ms,
            memory_after_decode.get("rss_mb"),
        )
        return ProvisorResponseRows(
            data,
            benchmark={
                "auth_wait_sec": round(auth_wait_sec, 6),
                "http_request_sec": round(http_request_sec, 6),
                "response_read_sec": None,
                "response_bytes": response_bytes,
                "json_decode_sec": round(json_decode_sec, 6),
                "rows_received": len(data),
                "pool_wait_sec": round(pool_wait_sec, 6) if pool_wait_sec is not None else None,
                "http_attempt_count": http_attempt_count,
                "auth_retry_count": auth_retry_count,
                "connection_reuse_scope": connection_reuse_scope,
                "rss_after_http_decode_mb": memory_after_decode.get("rss_mb"),
            },
        )
    finally:
        if close_client:
            await client.aclose()
