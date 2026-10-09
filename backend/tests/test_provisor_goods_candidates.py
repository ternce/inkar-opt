from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app import main
from backend.app.models import CompetitorCodeMapping, Product, ProductExtra
from backend.app.services import provisor as auth
from backend.app.services import provisor_goods as goods_module
from backend.app.services.competitors.code_mappings import (
    provisor_goods_search_seeds,
    rank_provisor_goods_candidates,
)
from backend.app.services.provisor_candidate_search import official_product_candidates
from backend.app.services.provisor_goods import (
    GOODS_SEARCH_PATH,
    ProvisorGoodsCandidate,
    ProvisorGoodsClient,
    ProvisorGoodsError,
    ProvisorGoodsRequestError,
    parse_goods_response,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _api_item(goods_id: int, number: int | None = 20) -> dict:
    return {
        "id": goods_id,
        "name": {"name": "Темпалгин М"},
        "brand": {"name": "Темпалгин"},
        "dose": {"name": "500мг"},
        "dosageForm": {"name": "Таблетки"},
        "number": number,
        "producer": {"name": "Sopharma S.A."},
        "corporation": {"name": "Sopharma S.A."},
        "inn": {"name": "Метамизол натрия"},
        "atc": {"name": "N02BB02"},
        "fullName": f"Темпалгин М/Таблетки/500мг/{number or ''}/Sopharma S.A.",
        "barcodes": [{"name": "3800010645881"}],
    }


def _candidate(goods_id: int, number: int | None = 20) -> ProvisorGoodsCandidate:
    return parse_goods_response([_api_item(goods_id, number)], limit=1)[0]


class _Response:
    def __init__(self, status_code: int, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class _Client:
    responses: list[object] = []
    posts: list[dict] = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, path, *, json=None, headers=None, **kwargs):
        type(self).posts.append({"path": path, "json": json, "headers": headers, "kwargs": kwargs})
        response = type(self).responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _install_client(monkeypatch, responses):
    _Client.responses = list(responses)
    _Client.posts = []
    tokens = iter(["token-one", "token-two", "token-three"])

    async def get_token(**_kwargs):
        return next(tokens)

    invalidations: list[bool] = []

    async def invalidate(**_kwargs):
        invalidations.append(True)

    monkeypatch.setattr(goods_module.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(goods_module, "get_access_token", get_token)
    monkeypatch.setattr(goods_module, "invalidate_access_token", invalidate)
    return invalidations


def test_goods_client_posts_official_payload_and_parses_structured_fields(monkeypatch):
    _install_client(monkeypatch, [_Response(200, [_api_item(82708)])])
    client = ProvisorGoodsClient(base_url="https://example.test", login="user", password="secret")

    rows = asyncio.run(client.search_goods(full_name="Темпалгин М", limit=100))

    assert rows[0].goods_id == 82708
    assert rows[0].package_count == 20
    assert rows[0].dose == "500мг"
    request = _Client.posts[0]
    assert request["path"] == GOODS_SEARCH_PATH
    assert request["json"]["Pagination"] == {"Take": "100", "Skip": "0"}
    assert request["json"]["SearchProperty"]["FullName"] == "Темпалгин М"
    assert request["json"]["SearchProperty"]["GoodsId"] is None
    assert "CseToken" not in request["json"]["SearchProperty"]
    assert request["headers"] == {"Authorization": "Bearer token-one"}


def test_goods_client_numeric_search_uses_goods_id(monkeypatch):
    _install_client(monkeypatch, [_Response(200, [_api_item(82708)])])
    client = ProvisorGoodsClient(base_url="https://example.test", login="user", password="secret")
    asyncio.run(client.search_goods(goods_id=82708, limit=30))
    search = _Client.posts[0]["json"]["SearchProperty"]
    assert search["GoodsId"] == 82708
    assert search["FullName"] == ""


def test_goods_client_401_invalidates_token_and_retries_once(monkeypatch):
    invalidations = _install_client(monkeypatch, [_Response(401, {}), _Response(200, [_api_item(82708)])])
    client = ProvisorGoodsClient(base_url="https://example.test", login="user", password="secret")
    rows = asyncio.run(client.search_goods(full_name="Темпалгин"))
    assert [row.goods_id for row in rows] == [82708]
    assert len(_Client.posts) == 2
    assert invalidations == [True]
    assert _Client.posts[1]["headers"] == {"Authorization": "Bearer token-two"}


def test_goods_client_auth_failure_is_safe_fallback_error(monkeypatch):
    _Client.responses = []
    _Client.posts = []

    async def fail_auth(**_kwargs):
        raise auth.ProvisorAuthError("login failed")

    monkeypatch.setattr(goods_module.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(goods_module, "get_access_token", fail_auth)
    client = ProvisorGoodsClient(base_url="https://example.test", login="user", password="secret")
    with pytest.raises(ProvisorGoodsError, match="authentication failed") as error:
        asyncio.run(client.search_goods(full_name="Темпалгин"))
    assert "secret" not in str(error.value)


def test_goods_client_non_transient_4xx_does_not_request_local_fallback(monkeypatch):
    _install_client(monkeypatch, [_Response(400, {})])
    client = ProvisorGoodsClient(base_url="https://example.test", login="user", password="secret")
    with pytest.raises(ProvisorGoodsRequestError) as error:
        asyncio.run(client.search_goods(full_name="Темпалгин"))
    assert error.value.fallback_allowed is False


@pytest.mark.parametrize(
    "response",
    [
        _Response(503, {}),
        _Response(200, ValueError("bad json")),
        _Response(200, {"items": []}),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("offline"),
    ],
)
def test_goods_client_normalizes_transient_and_malformed_failures(monkeypatch, response):
    _install_client(monkeypatch, [response])
    client = ProvisorGoodsClient(base_url="https://example.test", login="user", password="secret")
    with pytest.raises(ProvisorGoodsError):
        asyncio.run(client.search_goods(full_name="Темпалгин"))


def test_auth_cached_token_is_reused_without_login():
    auth._tokens_by_key.clear()
    key = ("https://example.test", "user")
    auth._tokens_by_key[key] = auth.ProvisorTokens(_jwt(int(time.time()) + 3600), "refresh", int(time.time()) + 3600)

    class NoPost:
        async def post(self, *_args, **_kwargs):
            raise AssertionError("login must not run")

    token = asyncio.run(
        auth.get_access_token(base_url=key[0], login=key[1], password="secret", client=NoPost())
    )
    assert token == auth._tokens_by_key[key].access


def test_auth_expired_token_refreshes_then_relogs_when_refresh_is_rejected():
    auth._tokens_by_key.clear()
    key = ("https://example.test", "user")
    expired = int(time.time()) - 10
    auth._tokens_by_key[key] = auth.ProvisorTokens(_jwt(expired), "refresh-old", expired)

    class AuthClient:
        paths: list[str] = []

        async def post(self, path, *, json=None, **_kwargs):
            self.paths.append(path)
            if path == "/Token/Update":
                return httpx.Response(401, json={"error": "expired"})
            return httpx.Response(
                200,
                json={"accessToken": _jwt(int(time.time()) + 3600), "refreshToken": "refresh-new"},
            )

    client = AuthClient()
    token = asyncio.run(
        auth.get_access_token(base_url=key[0], login=key[1], password="secret", client=client)
    )
    assert client.paths == ["/Token/Update", "/Token/CreateAll"]
    assert token == auth._tokens_by_key[key].access


def test_auth_concurrent_empty_cache_logs_in_once():
    auth._tokens_by_key.clear()

    class AuthClient:
        calls = 0

        async def post(self, path, *, json=None, **_kwargs):
            type(self).calls += 1
            await asyncio.sleep(0.01)
            return httpx.Response(
                200,
                json={"accessToken": _jwt(int(time.time()) + 3600), "refreshToken": "refresh-new"},
            )

    client = AuthClient()

    async def run():
        return await asyncio.gather(
            *[
                auth.get_access_token(
                    base_url="https://example.test", login="user", password="secret", client=client
                )
                for _ in range(10)
            ]
        )

    tokens = asyncio.run(run())
    assert len(set(tokens)) == 1
    assert AuthClient.calls == 1


def test_structured_number_20_ranks_and_number_10_is_hard_rejected():
    db = _session()
    product = Product(code="TEMP-20", name="Темпалгин М 500мг №20 таб", cost=1)
    db.add(product)
    db.flush()
    extra = ProductExtra(product_id=product.id, manufacturer="SOPHARMA")
    db.add(extra)
    db.flush()

    rows = rank_provisor_goods_candidates(
        db=db,
        product=product,
        extra=extra,
        goods=[_candidate(82708, 20), _candidate(82709, 10), _candidate(82710, None)],
        limit=10,
    )

    assert [row["sourceExternalKey"] for row in rows] == ["82708", "82710"]
    assert rows[0]["confidence"] > rows[1]["confidence"]
    assert rows[0]["packageCount"] == 20
    assert rows[0]["manualSuggestion"]["quantityMatch"] is True
    assert all("token" not in key.lower() for row in rows for key in row)


def test_existing_mapping_conflict_is_returned_but_not_selectable():
    db = _session()
    target = Product(code="TEMP-20", name="Темпалгин М 500мг №20 таб", cost=1)
    wrong = Product(code="TEMP-10", name="Темпалгин М 500мг №10 таб", cost=1)
    db.add_all([target, wrong])
    db.flush()
    extra = ProductExtra(product_id=target.id, manufacturer="SOPHARMA")
    db.add_all(
        [
            extra,
            CompetitorCodeMapping(
                platform="provisor",
                source_external_key="82708",
                source_match_key="provisor:82708",
                source_name="Темпалгин М/Таблетки/500мг/20/Sopharma S.A.",
                our_product_id=wrong.id,
                our_sku=wrong.code,
                status="mapped",
                confidence=85.5,
            ),
        ]
    )
    db.flush()

    row = rank_provisor_goods_candidates(
        db=db, product=target, extra=extra, goods=[_candidate(82708)], limit=5
    )[0]

    assert row["mappingConflict"] is True
    assert row["selectable"] is False
    assert row["mappedProductId"] == wrong.id
    assert row["mappedProductSku"] == wrong.code
    assert wrong.provisor_goods_id is None


def test_search_seed_is_clean_and_fallback_is_bounded():
    assert provisor_goods_search_seeds("Темпалгин М 500мг №20 таб") == ["ТЕМПАЛГИН М", "ТЕМПАЛГИН"]


def test_successful_empty_official_response_does_not_invoke_local_search():
    db = _session()
    product = Product(code="EMPTY", name="Темпалгин М 500мг №20 таб", cost=1)
    db.add(product)
    db.commit()

    class EmptyClient:
        calls = 0

        async def search_goods(self, **_kwargs):
            self.calls += 1
            return []

    client = EmptyClient()
    rows = asyncio.run(
        official_product_candidates(db=db, product_id=product.id, limit=5, client=client)
    )
    assert rows == []
    assert client.calls == 2


def test_candidate_endpoint_uses_local_search_only_when_official_service_fails(monkeypatch):
    db = _session()

    async def unavailable(**_kwargs):
        raise ProvisorGoodsError("offline")

    local_calls: list[dict] = []

    def local(**kwargs):
        local_calls.append(kwargs)
        return [{"sourceMatchKey": "provisor:1"}]

    monkeypatch.setattr(main, "official_product_candidates", unavailable)
    monkeypatch.setattr(main, "product_catalog_candidates_for_product", local)
    result = asyncio.run(
        main.get_competitor_code_mappings_product_catalog_candidates(
            product_id=1,
            platform="provisor",
            source=None,
            format_code="",
            formatCode="",
            limit=5,
            db=db,
            current_user=type("User", (), {"role": "admin"})(),
        )
    )
    assert result == [{"sourceMatchKey": "provisor:1", "candidateSource": "local_fallback"}]
    assert len(local_calls) == 1


def test_manual_provisor_search_uses_local_fallback_on_official_5xx(monkeypatch):
    db = _session()

    async def unavailable(**_kwargs):
        raise ProvisorGoodsError("Provisor goods search unavailable: HTTP 503")

    monkeypatch.setattr(main, "official_external_search", unavailable)
    monkeypatch.setattr(main, "_price_format_for_mapping_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        main,
        "search_competitor_items_for_mapping_service",
        lambda **_kwargs: [{"sourceMatchKey": "provisor:2"}],
    )
    result = asyncio.run(
        main.search_competitor_items_for_mapping(
            platform="provisor",
            q="Темпалгин",
            format_code=None,
            limit=30,
            db=db,
            current_user=type("User", (), {"role": "admin"})(),
        )
    )
    assert result == [{"sourceMatchKey": "provisor:2", "candidateSource": "local_fallback"}]
