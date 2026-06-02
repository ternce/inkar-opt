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


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

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


async def _create_tokens(*, client: httpx.AsyncClient, login: str, password: str) -> ProvisorTokens:
    resp = await client.post(
        "/Token/CreateAll",
        json={
            "login": login,
            "password": password,
        },
    )
    if resp.status_code >= 400:
        raise ProvisorAuthError(f"Token/CreateAll failed: HTTP {resp.status_code}: {resp.text}")

    data = resp.json()
    access = (data.get("accessToken") or "").strip()
    refresh = (data.get("refreshToken") or "").strip()
    if not access or not refresh:
        raise ProvisorAuthError("Token/CreateAll returned empty tokens")

    return ProvisorTokens(access=access, refresh=refresh, access_exp_unix=_jwt_exp_unix(access))


async def _update_tokens(*, client: httpx.AsyncClient, access: str, refresh: str) -> ProvisorTokens:
    resp = await client.post(
        "/Token/Update",
        json={
            "Access": access,
            "Refresh": refresh,
        },
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
            return cached.access

        timeout = httpx.Timeout(connect=10.0, read=timeout_seconds, write=30.0, pool=30.0)
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
            if cached and cached.access and cached.refresh:
                try:
                    updated = await _update_tokens(client=client, access=cached.access, refresh=cached.refresh)
                    _tokens_by_key[cache_key] = updated
                    return updated.access
                except Exception:
                    _tokens_by_key.pop(cache_key, None)

            created = await _create_tokens(client=client, login=login_s, password=password_s)
            _tokens_by_key[cache_key] = created
            return created.access


async def get_filials_by_context(
    *,
    base_url: str,
    login: str | None,
    password: str | None,
    timeout_seconds: float = 60.0,
    force_refresh: bool = False,
) -> list[dict]:
    """Fetches /Distributor/GetFilialsByContext returning available Provisor price lists."""

    timeout = httpx.Timeout(connect=10.0, read=timeout_seconds, write=30.0, pool=30.0)
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        token = await get_access_token(base_url=base_url, login=login, password=password, timeout_seconds=timeout_seconds)

        async def _call(access_token: str) -> httpx.Response:
            headers = {"Authorization": f"Bearer {access_token}"}
            if force_refresh:
                headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache"})
            return await client.get(
                "/Distributor/GetFilialsByContext",
                params={"_ts": int(time.time() * 1000)} if force_refresh else None,
                headers=headers,
            )

        resp = await _call(token)
        if resp.status_code in (401, 403):
            cache_key = (base_url.rstrip("/"), (login or "").strip())
            async with _lock:
                _tokens_by_key.pop(cache_key, None)
            token2 = await get_access_token(base_url=base_url, login=login, password=password, timeout_seconds=timeout_seconds)
            resp = await _call(token2)

        if resp.status_code >= 400:
            raise ProvisorAuthError(f"Distributor/GetFilialsByContext failed: HTTP {resp.status_code}: {resp.text}")

        try:
            data = resp.json()
        except Exception:
            raise ProvisorAuthError(f"Distributor/GetFilialsByContext returned invalid JSON: {resp.text}")
        if not isinstance(data, list):
            raise ProvisorAuthError("Distributor/GetFilialsByContext returned non-list JSON")
        logger.info("Provisor filials loaded: %s", len(data))
        return [x for x in data if isinstance(x, dict)]


async def get_prices_by_filial_id(
    *,
    base_url: str,
    login: str | None,
    password: str | None,
    filial_id: int,
    timeout_seconds: float = 30.0,
    force_refresh: bool = False,
) -> list[dict]:
    """Fetches /Price/GetByFilialId?filialId=... returning JSON list."""

    if filial_id <= 0:
        raise ValueError("filial_id must be positive")

    started_at = time.perf_counter()
    started_wall = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    stage = "login"
    read_timeout = timeout_seconds or _env_float("PROVISOR_PRICE_READ_TIMEOUT_SECONDS", 30.0)
    timeout = httpx.Timeout(
        connect=_env_float("PROVISOR_PRICE_CONNECT_TIMEOUT_SECONDS", 10.0),
        read=read_timeout,
        write=10.0,
        pool=10.0,
    )

    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        try:
            token = await get_access_token(
                base_url=base_url,
                login=login,
                password=password,
                timeout_seconds=timeout_seconds,
            )
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
            )

        try:
            stage = "get_price_items"
            resp = await asyncio.wait_for(_call(token), timeout=timeout_seconds)

            if resp.status_code in (401, 403):
                stage = "login"
                cache_key = (base_url.rstrip("/"), (login or "").strip())
                async with _lock:
                    _tokens_by_key.pop(cache_key, None)

                token2 = await get_access_token(
                    base_url=base_url,
                    login=login,
                    password=password,
                    timeout_seconds=timeout_seconds,
                )

                stage = "get_price_items"
                resp = await asyncio.wait_for(_call(token2), timeout=timeout_seconds)

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
            data = resp.json()
        except Exception:
            raise ProvisorAuthError(
                f"Price/GetByFilialId returned invalid JSON: {resp.text}"
            )

        if not isinstance(data, list):
            raise ProvisorAuthError("Price/GetByFilialId returned non-list JSON")

        return data
