from __future__ import annotations

import asyncio

import pytest

from backend.app import worker as app_worker
from backend.app.services.matching_cache.config import (
    matching_cache_read_allowed_for_price_format,
    matching_cache_read_price_format_ids,
    matching_cache_worker_lease_seconds,
    matching_cache_worker_poll_interval_seconds,
)


def test_read_allowlist_is_optional_and_strict_when_configured(monkeypatch):
    monkeypatch.delenv("MATCH_CACHE_READ_PRICE_FORMAT_IDS", raising=False)
    monkeypatch.delenv("MATCH_CACHE_READ_ALLOW_ALL", raising=False)
    assert matching_cache_read_price_format_ids() == frozenset()
    assert matching_cache_read_allowed_for_price_format(88) is False

    monkeypatch.setenv("MATCH_CACHE_READ_PRICE_FORMAT_IDS", "88, 91,88")
    assert matching_cache_read_price_format_ids() == frozenset({88, 91})
    assert matching_cache_read_allowed_for_price_format(88) is True
    assert matching_cache_read_allowed_for_price_format(89) is False

    monkeypatch.setenv("MATCH_CACHE_READ_ALLOW_ALL", "true")
    assert matching_cache_read_allowed_for_price_format(89) is True

    monkeypatch.setenv("MATCH_CACHE_READ_PRICE_FORMAT_IDS", "88,nope")
    with pytest.raises(ValueError, match="comma-separated"):
        matching_cache_read_price_format_ids()


def test_worker_resource_settings_are_bounded(monkeypatch):
    monkeypatch.setenv("MATCH_CACHE_WORKER_POLL_INTERVAL_SECONDS", "2")
    monkeypatch.setenv("MATCH_CACHE_WORKER_LEASE_SECONDS", "1200")
    assert matching_cache_worker_poll_interval_seconds() == 2
    assert matching_cache_worker_lease_seconds() == 1200

    monkeypatch.setenv("MATCH_CACHE_WORKER_LEASE_SECONDS", "5")
    with pytest.raises(ValueError, match="between 60 and 7200"):
        matching_cache_worker_lease_seconds()


def test_existing_worker_poller_invokes_one_bounded_cache_consumer(monkeypatch):
    stop_event = asyncio.Event()
    calls: list[dict] = []

    async def fake_to_thread(function, **kwargs):
        calls.append({"function": function, **kwargs})
        stop_event.set()
        return "idle"

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    asyncio.run(
        app_worker._poll_matching_cache_queue(
            stop_event=stop_event,
            worker_id="worker-77:matching-cache",
            poll_interval=1.0,
            lease_seconds=900,
        )
    )

    assert len(calls) == 1
    assert calls[0]["function"] is app_worker.run_one_matching_job
    assert calls[0]["session_factory"] is app_worker.SessionLocal
    assert calls[0]["worker_id"] == "worker-77:matching-cache"
    assert calls[0]["lease_seconds"] == 900
