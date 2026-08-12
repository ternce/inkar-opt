from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def _copy_settings(settings, **updates):
    if hasattr(settings, "model_copy"):
        return settings.model_copy(update=updates)
    return settings.copy(update=updates)


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _patch_startup_dependencies(monkeypatch, main, calls: list[str]) -> None:
    monkeypatch.setattr(main, "init_db", lambda: calls.append("init_db"))
    monkeypatch.setattr(main, "_seed_price_formats_if_missing", lambda: calls.append("seed_price_formats"))
    monkeypatch.setattr(main, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(main, "mark_stale_emit_jobs", lambda *args, **kwargs: calls.append("mark_stale_emit_jobs"))
    monkeypatch.setattr(main, "recover_stale_parser_jobs", lambda *args, **kwargs: calls.append("recover_stale_parser_jobs"))
    monkeypatch.setattr(main, "recover_expired_orphan_locks", lambda *args, **kwargs: calls.append("recover_expired_orphan_locks"))
    monkeypatch.setattr(
        main,
        "resume_pending_percentile_preparations",
        lambda *args, **kwargs: calls.append("resume_percentile_preparations"),
    )
    monkeypatch.setattr(
        main,
        "retry_waiting_percentile_preparations",
        lambda *args, **kwargs: calls.append("retry_percentile_preparations"),
    )
    monkeypatch.setattr(main, "_start_provisor_auto_refresh_scheduler", lambda: calls.append("start_provisor_scheduler"))
    monkeypatch.setattr(main, "_start_emit_refresh_scheduler", lambda: calls.append("start_emit_scheduler"))
    monkeypatch.setattr(main.EmitConfig, "from_settings", lambda settings: SimpleNamespace())


def test_process_role_defaults_to_all(monkeypatch):
    import backend.app.config as config

    config.get_settings.cache_clear()
    monkeypatch.delenv("PROCESS_ROLE", raising=False)

    assert config.get_settings().process_role == "all"


def test_invalid_process_role_is_rejected(monkeypatch):
    import backend.app.config as config

    config.get_settings.cache_clear()
    monkeypatch.setenv("PROCESS_ROLE", "parser")

    with pytest.raises(ValueError, match="PROCESS_ROLE must be one of"):
        config.get_settings()


def test_all_startup_runs_web_initialization_recovery_and_schedulers(monkeypatch):
    import backend.app.main as main

    calls: list[str] = []
    monkeypatch.setattr(main, "settings", _copy_settings(main.settings, process_role="all", environment="prod"))
    _patch_startup_dependencies(monkeypatch, main, calls)

    asyncio.run(main._startup())

    assert calls == [
        "init_db",
        "seed_price_formats",
        "mark_stale_emit_jobs",
        "resume_percentile_preparations",
        "retry_percentile_preparations",
        "start_provisor_scheduler",
        "start_emit_scheduler",
    ]


def test_web_startup_skips_parser_schedulers_and_emit_temp_recovery(monkeypatch):
    import backend.app.main as main

    calls: list[str] = []
    monkeypatch.setattr(main, "settings", _copy_settings(main.settings, process_role="web", environment="prod"))
    _patch_startup_dependencies(monkeypatch, main, calls)

    asyncio.run(main._startup())

    assert calls == [
        "init_db",
        "seed_price_formats",
        "resume_percentile_preparations",
        "retry_percentile_preparations",
    ]
    assert "mark_stale_emit_jobs" not in calls
    assert "start_provisor_scheduler" not in calls
    assert "start_emit_scheduler" not in calls


def test_worker_startup_runs_worker_recovery_and_schedulers_without_init_db(monkeypatch):
    import backend.app.main as main

    calls: list[str] = []
    monkeypatch.setattr(main, "settings", _copy_settings(main.settings, process_role="worker", environment="prod"))
    _patch_startup_dependencies(monkeypatch, main, calls)

    main._worker_startup()

    assert calls == [
        "recover_stale_parser_jobs",
        "recover_expired_orphan_locks",
        "start_provisor_scheduler",
        "start_emit_scheduler",
    ]
    assert "init_db" not in calls
    assert "seed_price_formats" not in calls


def test_fastapi_worker_role_startup_is_rejected(monkeypatch):
    import backend.app.main as main

    monkeypatch.setattr(main, "settings", _copy_settings(main.settings, process_role="worker", environment="prod"))

    with pytest.raises(RuntimeError, match="python -m app.worker"):
        asyncio.run(main._startup())


def test_worker_module_does_not_expose_http_app_or_uvicorn_entrypoint():
    import backend.app.worker as worker

    assert not hasattr(worker, "app")
    assert "uvicorn" not in worker.main.__code__.co_names


def test_role_separation_does_not_modify_pricing_configuration(monkeypatch):
    import backend.app.main as main

    calls: list[str] = []
    original_mode = main.settings.provisor_auto_refresh_mode
    original_filials = list(main.settings.emit_filial_ids)
    monkeypatch.setattr(main, "settings", _copy_settings(main.settings, process_role="web", environment="prod"))
    _patch_startup_dependencies(monkeypatch, main, calls)

    asyncio.run(main._startup())

    assert main.settings.provisor_auto_refresh_mode == original_mode
    assert main.settings.emit_filial_ids == original_filials


def test_role_separation_does_not_modify_competitor_assignments(monkeypatch):
    import backend.app.main as main

    calls: list[str] = []
    monkeypatch.setattr(main, "settings", _copy_settings(main.settings, process_role="web", environment="prod"))
    _patch_startup_dependencies(monkeypatch, main, calls)
    monkeypatch.setattr(
        main,
        "propagate_emit_assignments_to_new_price_format",
        lambda *args, **kwargs: calls.append("propagate_emit_assignments"),
    )

    asyncio.run(main._startup())

    assert "propagate_emit_assignments" not in calls


def test_refresh_endpoints_remain_available_in_web_mode(monkeypatch):
    import backend.app.main as main

    monkeypatch.setattr(main, "settings", _copy_settings(main.settings, process_role="web", environment="prod"))
    paths = {getattr(route, "path", "") for route in main.app.routes}

    assert "/api/price-sources/refresh/status" in paths
    assert "/api/price-sources/refresh/provisor/auto/run-now" in paths
    assert "/api/emit/refresh/status" in paths
    assert "/api/emit/refresh/run-now" in paths
    assert "/api/price-formats/{format_code}/competitor-price-lists/refresh" in paths
    assert "/api/jobs/{job_id}" in paths
