from __future__ import annotations

import asyncio

import httpx
import pytest

from backend.app.services import provisor


async def _fetch(monkeypatch, handler):
    tokens = iter(("token-1", "token-2", "token-3"))

    async def fake_token(**_kwargs):
        return next(tokens)

    monkeypatch.setattr(provisor, "get_access_token", fake_token)
    monkeypatch.setenv("PROVISOR_PRICE_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("PROVISOR_RETRY_BASE_DELAY_SECONDS", "0")
    monkeypatch.setenv("PROVISOR_RETRY_MAX_DELAY_SECONDS", "0")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="https://provisor.invalid", transport=transport) as client:
        return await provisor.get_prices_by_filial_id(
            base_url="https://provisor.invalid",
            login="account",
            password="secret",
            filial_id=134,
            timeout_seconds=1,
            client=client,
        )


def test_http_400_temporary_server_error_retries_then_recovers(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(400, text="Upstream server error", request=request)
        return httpx.Response(200, json=[{"id": 1}], request=request)

    rows = asyncio.run(_fetch(monkeypatch, handler))

    assert list(rows) == [{"id": 1}]
    assert rows.benchmark["http_attempt_count"] == 2
    assert calls == 2


def test_non_auth_http_400_is_request_error_not_auth_error(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="Malformed filial request", request=request)

    with pytest.raises(provisor.ProvisorRequestError) as caught:
        asyncio.run(_fetch(monkeypatch, handler))

    assert caught.value.status_code == 400
    assert caught.value.retryable is False
    assert calls == 1


def test_credential_shaped_http_400_is_authentication_error_without_retry(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="Invalid login or password", request=request)

    with pytest.raises(provisor.ProvisorAuthError):
        asyncio.run(_fetch(monkeypatch, handler))

    assert calls == 1


@pytest.mark.parametrize("status_code", [401, 403])
def test_authentication_status_refreshes_token_once_then_fails(monkeypatch, status_code):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, text="Unauthorized", request=request)

    with pytest.raises(provisor.ProvisorAuthError):
        asyncio.run(_fetch(monkeypatch, handler))

    assert calls == 2


@pytest.mark.parametrize("status_code", [429, 500, 502, 503])
def test_transient_http_status_retries_and_recovers(monkeypatch, status_code):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            headers = {"Retry-After": "0"} if status_code == 429 else {}
            return httpx.Response(status_code, text="temporary", headers=headers, request=request)
        return httpx.Response(200, json=[], request=request)

    rows = asyncio.run(_fetch(monkeypatch, handler))

    assert list(rows) == []
    assert rows.benchmark["http_attempt_count"] == 2
    assert calls == 2


def test_network_timeout_retries_and_recovers(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("temporary timeout", request=request)
        return httpx.Response(200, json=[{"id": 2}], request=request)

    rows = asyncio.run(_fetch(monkeypatch, handler))

    assert list(rows) == [{"id": 2}]
    assert calls == 2


def test_transient_failure_stops_after_bounded_attempts(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="unavailable", request=request)

    with pytest.raises(provisor.ProvisorRequestError) as caught:
        asyncio.run(_fetch(monkeypatch, handler))

    assert caught.value.status_code == 503
    assert caught.value.retryable is True
    assert calls == 3
