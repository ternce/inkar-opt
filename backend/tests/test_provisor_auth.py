from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from backend.app.services import provisor


BASE_URL = "https://provisor.invalid"
LOGIN = "test-account"
PASSWORD = "test-password"
CACHE_KEY = (BASE_URL, LOGIN)


@pytest.fixture(autouse=True)
def clear_token_cache():
    provisor._tokens_by_key.clear()
    yield
    provisor._tokens_by_key.clear()


def _expired_tokens() -> provisor.ProvisorTokens:
    expired = int(time.time()) - 60
    return provisor.ProvisorTokens(
        access="expired-access",
        refresh="expired-refresh",
        access_exp_unix=expired,
    )


def _fresh_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"accessToken": "fresh-access", "refreshToken": "fresh-refresh"},
        request=request,
    )


async def _access_token(client: httpx.AsyncClient) -> str:
    return await provisor.get_access_token(
        base_url=BASE_URL,
        login=LOGIN,
        password=PASSWORD,
        client=client,
    )


def test_expired_refresh_token_response_reauthenticates_with_create_all():
    provisor._tokens_by_key[CACHE_KEY] = _expired_tokens()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/Token/Update":
            return httpx.Response(
                400,
                json={
                    "error": "Срок жизни токена истек. Авторизуйтесь в системе.",
                    "status": 400,
                },
                request=request,
            )
        return _fresh_response(request)

    async def run() -> str:
        async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
            return await _access_token(client)

    assert asyncio.run(run()) == "fresh-access"
    assert paths == ["/Token/Update", "/Token/CreateAll"]
    assert provisor._tokens_by_key[CACHE_KEY].refresh == "fresh-refresh"


def test_invalid_refresh_token_response_reauthenticates_with_create_all():
    provisor._tokens_by_key[CACHE_KEY] = _expired_tokens()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/Token/Update":
            return httpx.Response(400, json={"error": "Invalid refresh token"}, request=request)
        return _fresh_response(request)

    async def run() -> str:
        async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
            return await _access_token(client)

    assert asyncio.run(run()) == "fresh-access"
    assert paths == ["/Token/Update", "/Token/CreateAll"]


def test_invalid_create_all_credentials_fail_without_retry():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(400, json={"error": "Invalid login or password"}, request=request)

    async def run() -> str:
        async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
            return await _access_token(client)

    with pytest.raises(provisor.ProvisorAuthError, match="Token/CreateAll failed: HTTP 400"):
        asyncio.run(run())
    assert paths == ["/Token/CreateAll"]
    assert CACHE_KEY not in provisor._tokens_by_key


def test_unrelated_update_http_400_does_not_trigger_reauthentication():
    cached = _expired_tokens()
    provisor._tokens_by_key[CACHE_KEY] = cached
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(400, json={"error": "Malformed token request"}, request=request)

    async def run() -> str:
        async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
            return await _access_token(client)

    with pytest.raises(provisor.ProvisorRequestError) as caught:
        asyncio.run(run())
    assert caught.value.status_code == 400
    assert caught.value.retryable is False
    assert paths == ["/Token/Update"]
    assert provisor._tokens_by_key[CACHE_KEY] is cached


def test_concurrent_expired_token_requests_share_one_reauthentication_flow():
    provisor._tokens_by_key[CACHE_KEY] = _expired_tokens()
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        await asyncio.sleep(0.01)
        if request.url.path == "/Token/Update":
            return httpx.Response(400, json={"error": "Invalid refresh token"}, request=request)
        return _fresh_response(request)

    async def run() -> list[str]:
        async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
            return await asyncio.gather(*[_access_token(client) for _ in range(10)])

    tokens = asyncio.run(run())
    assert tokens == ["fresh-access"] * 10
    assert paths.count("/Token/Update") == 1
    assert paths.count("/Token/CreateAll") == 1


def test_successful_update_remains_single_request_without_create_all():
    provisor._tokens_by_key[CACHE_KEY] = _expired_tokens()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={"accessToken": "updated-access", "refreshToken": "updated-refresh"},
            request=request,
        )

    async def run() -> str:
        async with httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as client:
            return await _access_token(client)

    assert asyncio.run(run()) == "updated-access"
    assert paths == ["/Token/Update"]
    assert provisor._tokens_by_key[CACHE_KEY].refresh == "updated-refresh"
