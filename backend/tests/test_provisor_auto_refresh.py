from __future__ import annotations

import asyncio
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import (
    CompetitorPriceList,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    PriceSourceAccount,
    RefreshJob,
    RefreshLock,
)
from backend.app.services import provisor_auto_refresh as svc


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _session_factory(path: Path):
    engine = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_auto_refresh_config_defaults_disabled(monkeypatch):
    import backend.app.config as config

    config.get_settings.cache_clear()
    monkeypatch.delenv("PROVISOR_AUTO_REFRESH_ENABLED", raising=False)
    monkeypatch.delenv("PROVISOR_AUTO_REFRESH_MODE", raising=False)
    settings = config.get_settings()

    assert settings.provisor_auto_refresh_enabled is False
    assert settings.provisor_auto_refresh_mode == "selected"


def test_auto_refresh_config_env_enables_scheduler(monkeypatch):
    import backend.app.config as config

    config.get_settings.cache_clear()
    monkeypatch.setenv("PROVISOR_AUTO_REFRESH_ENABLED", "true")
    monkeypatch.setenv("PROVISOR_AUTO_REFRESH_MODE", "all")
    monkeypatch.setenv("PROVISOR_AUTO_REFRESH_CRON", "0 2 * * *")
    settings = config.get_settings()

    assert settings.provisor_auto_refresh_enabled is True
    assert settings.provisor_auto_refresh_mode == "all"
    assert settings.provisor_auto_refresh_cron == "0 2 * * *"


def test_scheduler_timezone_is_configured_by_default(monkeypatch):
    import backend.app.config as config

    config.get_settings.cache_clear()
    monkeypatch.delenv("PROVISOR_AUTO_REFRESH_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    settings = config.get_settings()

    assert settings.provisor_auto_refresh_timezone == "Asia/Qyzylorda"


def test_lock_prevents_overlapping_refresh_jobs():
    db = _session()
    first, blocker, first_token = svc.try_create_refresh_job(db, mode="selected", requested_by="test")
    second, second_blocker, second_token = svc.try_create_refresh_job(db, mode="selected", requested_by="test")

    assert first is not None
    assert first_token
    assert blocker is None
    assert second is None
    assert second_token is None
    assert second_blocker is not None
    assert second_blocker.id == first.id


def test_global_refresh_lock_allows_only_one_owner(tmp_path):
    Session = _session_factory(tmp_path / "global-lock.db")

    def acquire(source: str):
        db = Session()
        try:
            token = svc.new_owner_token()
            return svc.try_acquire_global_refresh_lock(db, owner_token=token, source=source, requested_by="test"), token
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda source: acquire(source), ["emit", "provisor"]))

    assert sum(1 for acquired, _token in results if acquired) == 1


def test_global_refresh_lock_released_after_finish():
    db = _session()
    job, _, token = svc.try_create_refresh_job(db, mode="selected", requested_by="test")
    assert job is not None and token is not None
    svc.start_job(db, job, total_accounts=1, total_plk=1, metadata={}, owner_token=token)

    assert svc.finish_job(db, job, status="success", message="done", owner_token=token, release_refresh=True, release_global=True)

    next_token = svc.new_owner_token()
    assert svc.try_acquire_global_refresh_lock(db, owner_token=next_token, source="emit", requested_by="test") is True


def test_generic_refresh_mutex_released_after_exception(monkeypatch, tmp_path):
    import backend.app.main as main

    Session = _session_factory(tmp_path / "generic-wrapper-failure.db")
    with Session() as db:
        token = svc.new_owner_token()
        assert svc.try_acquire_global_refresh_lock(db, owner_token=token, source="provisor", requested_by="test") is True
        job = main.create_job(db=db, job_type="refresh_price_lists:provisor", format_code="FMT", message="test")

        async def fail_refresh(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(main, "_run_refresh_price_lists_job", fail_refresh)
        try:
            asyncio.run(
                main._run_refresh_price_lists_job_with_mutex(
                    db,
                    job,
                    format_code="FMT",
                    payload={"source": "provisor"},
                    global_owner_token=token,
                )
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("Expected wrapped refresh failure")

        next_token = svc.new_owner_token()
        assert svc.try_acquire_global_refresh_lock(db, owner_token=next_token, source="emit", requested_by="test") is True


def test_scheduler_provisor_skips_when_emit_job_active(monkeypatch, tmp_path):
    import backend.app.main as main

    Session = _session_factory(tmp_path / "scheduler-skip.db")
    with Session() as db:
        db.add(
            RefreshJob(
                source_type="emit",
                mode="selected",
                status="running",
                started_at=datetime.utcnow(),
                heartbeat_at=datetime.utcnow(),
                requested_by="test",
            )
        )
        db.commit()

    monkeypatch.setattr(main, "SessionLocal", Session)
    asyncio.run(main._start_provisor_refresh_background(mode="selected", requested_by="scheduler"))

    with Session() as db:
        rows = db.query(RefreshJob).filter(RefreshJob.source_type == "provisor").all()
        assert len(rows) == 1
        assert rows[0].status == "skipped"
        assert "Emit refresh is currently running" in rows[0].message


def test_manual_provisor_returns_409_when_emit_active(monkeypatch, tmp_path):
    import backend.app.main as main

    Session = _session_factory(tmp_path / "manual-provisor-conflict.db")
    with Session() as db:
        db.add(
            RefreshJob(
                source_type="emit",
                mode="selected",
                status="running",
                started_at=datetime.utcnow(),
                heartbeat_at=datetime.utcnow(),
                requested_by="test",
            )
        )
        db.commit()

    monkeypatch.setattr(main, "SessionLocal", Session)
    main.app.dependency_overrides[main.get_db] = lambda: Session()
    try:
        client = TestClient(main.app)
        response = client.post("/api/price-sources/refresh/provisor/auto/run-now", json={"mode": "selected"})
        assert response.status_code == 409
        assert "Emit refresh is currently running" in response.json()["detail"]
    finally:
        main.app.dependency_overrides.clear()


def test_concurrent_run_now_style_creation_only_creates_one_job(tmp_path):
    Session = _session_factory(tmp_path / "locks.db")

    def create():
        db = Session()
        try:
            job, blocker, token = svc.try_create_refresh_job(db, mode="selected", requested_by="manual")
            return bool(job), bool(blocker), bool(token)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))

    assert sum(1 for job_created, _, _ in results if job_created) == 1
    db = Session()
    try:
        assert db.query(RefreshJob).count() == 1
    finally:
        db.close()


def test_scheduler_and_manual_overlap_blocked_by_refresh_lock():
    db = _session()
    scheduled, _, scheduled_token = svc.try_create_refresh_job(db, mode="selected", requested_by="scheduler")
    manual, blocker, manual_token = svc.try_create_refresh_job(db, mode="selected", requested_by="manual")

    assert scheduled is not None
    assert scheduled_token
    assert manual is None
    assert manual_token is None
    assert blocker is not None
    assert blocker.id == scheduled.id


def test_multiple_scheduler_ownership_only_one_wins(tmp_path):
    Session = _session_factory(tmp_path / "scheduler.db")
    db1 = Session()
    db2 = Session()
    try:
        token1 = svc.new_owner_token()
        token2 = svc.new_owner_token()

        assert svc.try_acquire_scheduler_lock(db1, owner_token=token1) is True
        assert svc.try_acquire_scheduler_lock(db2, owner_token=token2) is False
    finally:
        db1.close()
        db2.close()


def test_emit_scheduler_lease_allows_only_one_owner(tmp_path):
    Session = _session_factory(tmp_path / "emit-scheduler.db")
    db1 = Session()
    db2 = Session()
    try:
        token1 = svc.new_owner_token()
        token2 = svc.new_owner_token()

        assert svc.try_acquire_emit_scheduler_lock(db1, owner_token=token1) is True
        assert svc.try_acquire_emit_scheduler_lock(db2, owner_token=token2) is False
    finally:
        db1.close()
        db2.close()


def test_expired_emit_scheduler_lease_can_be_reacquired(tmp_path):
    Session = _session_factory(tmp_path / "emit-scheduler-expired.db")
    with Session() as db:
        token1 = svc.new_owner_token()
        assert svc.try_acquire_emit_scheduler_lock(db, owner_token=token1) is True
        lock = db.get(RefreshLock, svc.EMIT_SCHEDULER_LOCK_NAME)
        assert lock is not None
        lock.lease_until = datetime.utcnow() - timedelta(seconds=1)
        db.commit()

    with Session() as db:
        token2 = svc.new_owner_token()
        assert svc.try_acquire_emit_scheduler_lock(db, owner_token=token2) is True
        lock = db.get(RefreshLock, svc.EMIT_SCHEDULER_LOCK_NAME)
        assert lock is not None
        db.refresh(lock)
        assert lock.owner_token == token2


def test_emit_scheduler_lease_renewal_keeps_ownership(tmp_path):
    Session = _session_factory(tmp_path / "emit-scheduler-renew.db")
    with Session() as db:
        token1 = svc.new_owner_token()
        token2 = svc.new_owner_token()

        assert svc.try_acquire_emit_scheduler_lock(db, owner_token=token1) is True
        assert svc.renew_emit_scheduler_lock(db, owner_token=token1) is True
        assert svc.try_acquire_emit_scheduler_lock(db, owner_token=token2) is False

        lock = db.get(RefreshLock, svc.EMIT_SCHEDULER_LOCK_NAME)
        assert lock is not None
        assert lock.owner_token == token1
        assert lock.lease_until > datetime.utcnow()


def test_concurrent_emit_scheduler_acquisition_only_one_wins(tmp_path):
    Session = _session_factory(tmp_path / "emit-scheduler-concurrent.db")

    def acquire():
        db = Session()
        try:
            return svc.try_acquire_emit_scheduler_lock(db, owner_token=svc.new_owner_token())
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: acquire(), range(8)))

    assert sum(1 for acquired in results if acquired) == 1


class _FakeEmitConfig:
    enabled = True
    cron = "0 3 * * *"
    timezone = "UTC"


class _FakeScheduler:
    instances: list["_FakeScheduler"] = []

    def __init__(self, timezone):
        self.timezone = timezone
        self.jobs = []
        self.started = False
        self.shutdown_calls: list[bool] = []
        self.__class__.instances.append(self)

    def add_job(self, *args, **kwargs):
        self.jobs.append((args, kwargs))

    def start(self):
        self.started = True

    def shutdown(self, wait=False):
        self.shutdown_calls.append(wait)


class _FakeTask:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def _install_fake_emit_scheduler(monkeypatch, Session):
    import backend.app.main as main

    def fake_create_task(coro):
        if hasattr(coro, "close"):
            coro.close()
        return _FakeTask()

    scheduler_module = types.ModuleType("apscheduler.schedulers.asyncio")
    scheduler_module.AsyncIOScheduler = _FakeScheduler
    trigger_module = types.ModuleType("apscheduler.triggers.cron")
    trigger_module.CronTrigger = types.SimpleNamespace(from_crontab=lambda cron: ("cron", cron))

    _FakeScheduler.instances = []
    monkeypatch.setitem(sys.modules, "apscheduler.schedulers.asyncio", scheduler_module)
    monkeypatch.setitem(sys.modules, "apscheduler.triggers.cron", trigger_module)
    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(main.EmitConfig, "from_settings", staticmethod(lambda _settings: _FakeEmitConfig()))
    monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(main, "_emit_refresh_scheduler", None)
    monkeypatch.setattr(main, "_emit_refresh_scheduler_token", None)
    monkeypatch.setattr(main, "_emit_refresh_scheduler_renew_task", None)
    return main


def test_emit_scheduler_registers_job_only_when_lease_owned(monkeypatch, tmp_path):
    Session = _session_factory(tmp_path / "emit-scheduler-start.db")
    main = _install_fake_emit_scheduler(monkeypatch, Session)

    main._start_emit_refresh_scheduler()

    assert len(_FakeScheduler.instances) == 1
    scheduler = _FakeScheduler.instances[0]
    assert scheduler.started is True
    assert scheduler.timezone == "UTC"
    assert len(scheduler.jobs) == 1
    assert scheduler.jobs[0][1]["id"] == "emit_refresh"
    assert main._emit_refresh_scheduler is scheduler
    assert main._emit_refresh_scheduler_token is not None


def test_emit_scheduler_does_not_register_without_lease(monkeypatch, tmp_path):
    Session = _session_factory(tmp_path / "emit-scheduler-denied.db")
    with Session() as db:
        assert svc.try_acquire_emit_scheduler_lock(db, owner_token="other-owner") is True
    main = _install_fake_emit_scheduler(monkeypatch, Session)

    main._start_emit_refresh_scheduler()

    assert _FakeScheduler.instances == []
    assert main._emit_refresh_scheduler is None
    assert main._emit_refresh_scheduler_token is None


def test_emit_scheduler_does_not_duplicate_jobs(monkeypatch, tmp_path):
    Session = _session_factory(tmp_path / "emit-scheduler-duplicate.db")
    main = _install_fake_emit_scheduler(monkeypatch, Session)

    main._start_emit_refresh_scheduler()
    main._start_emit_refresh_scheduler()

    assert len(_FakeScheduler.instances) == 1
    assert len(_FakeScheduler.instances[0].jobs) == 1


def test_emit_scheduler_starts_after_previous_owner_expires(monkeypatch, tmp_path):
    Session = _session_factory(tmp_path / "emit-scheduler-expired-start.db")
    with Session() as db:
        assert svc.try_acquire_emit_scheduler_lock(db, owner_token="old-owner") is True
        lock = db.get(RefreshLock, svc.EMIT_SCHEDULER_LOCK_NAME)
        assert lock is not None
        lock.lease_until = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    main = _install_fake_emit_scheduler(monkeypatch, Session)

    main._start_emit_refresh_scheduler()

    assert len(_FakeScheduler.instances) == 1
    assert main._emit_refresh_scheduler_token is not None
    assert main._emit_refresh_scheduler_token != "old-owner"


def test_status_endpoint_shape_for_running_job():
    db = _session()
    job, _, token = svc.try_create_refresh_job(db, mode="selected", requested_by="test")
    assert job is not None
    svc.start_job(db, job, total_accounts=3, total_plk=108, metadata={}, owner_token=token)
    job.processed_accounts = 1
    job.processed_plk = 20
    job.success_count = 18
    job.failed_count = 2
    db.commit()

    status = svc.refresh_job_to_status(svc.latest_refresh_job(db))

    assert status["status"] == "running"
    assert status["source_type"] == "provisor"
    assert status["processed_accounts"] == 1
    assert status["total_plk"] == 108
    assert status["failed_count"] == 2


def test_skipped_overlap_record_does_not_hide_running_status():
    db = _session()
    job, _, token = svc.try_create_refresh_job(db, mode="selected", requested_by="test")
    assert job is not None
    svc.start_job(db, job, total_accounts=1, total_plk=1, metadata={}, owner_token=token)
    svc.create_skipped_job(db, mode="selected", requested_by="test", message="Provisor refresh is already running.")

    status = svc.refresh_job_to_status(svc.latest_refresh_job(db))

    assert status["status"] == "running"
    assert status["message"] == "Refreshing Provisor PLK..."


def test_heartbeat_uses_separate_session(monkeypatch, tmp_path):
    import backend.app.main as main

    Session = _session_factory(tmp_path / "heartbeat.db")
    db = Session()
    try:
        job, _, token = svc.try_create_refresh_job(db, mode="selected", requested_by="test")
        assert job is not None and token is not None
        svc.start_job(db, job, total_accounts=1, total_plk=1, metadata={}, owner_token=token)
        original_session_id = id(db)
        created_session_ids: list[int] = []

        def session_local():
            session = Session()
            created_session_ids.append(id(session))
            return session

        monkeypatch.setattr(main, "SessionLocal", session_local)

        assert main._heartbeat_provisor_refresh_job(int(job.id), token, "beat") is True
        assert created_session_ids
        assert all(session_id != original_session_id for session_id in created_session_ids)
    finally:
        db.close()


def test_stale_resolved_job_cannot_be_overwritten_by_old_runner():
    db = _session()
    job, _, token = svc.try_create_refresh_job(db, mode="selected", requested_by="test")
    assert job is not None and token is not None
    svc.start_job(db, job, total_accounts=1, total_plk=1, metadata={}, owner_token=token)
    job.status = "stale"
    db.commit()

    resolved = svc.finish_job(
        db,
        job,
        status="failed",
        message="resolved",
        owner_token=token,
        allowed_statuses={"stale"},
        release_refresh=True,
    )
    old_finish = svc.finish_job(
        db,
        job,
        status="success",
        message="old runner done",
        owner_token=token,
        allowed_statuses={"running"},
        release_refresh=True,
    )

    db.refresh(job)
    assert resolved is True
    assert old_finish is False
    assert job.status == "failed"


def test_scheduler_shutdown_calls_scheduler_shutdown(monkeypatch):
    import backend.app.main as main

    class FakeScheduler:
        def __init__(self):
            self.calls: list[bool] = []

        def shutdown(self, wait=False):
            self.calls.append(wait)

    class FakeTask:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    scheduler = FakeScheduler()
    task = FakeTask()
    monkeypatch.setattr(main, "_provisor_auto_refresh_scheduler", scheduler)
    monkeypatch.setattr(main, "_provisor_auto_refresh_scheduler_token", None)
    monkeypatch.setattr(main, "_provisor_auto_refresh_scheduler_renew_task", task)

    main._shutdown_provisor_auto_refresh_scheduler()

    assert scheduler.calls == [False]
    assert task.cancelled is True
    assert main._provisor_auto_refresh_scheduler is None


def test_stale_job_detection_marks_running_job_stale():
    db = _session()
    stale_time = datetime.utcnow() - timedelta(minutes=11)
    job = RefreshJob(
        source_type="provisor",
        mode="selected",
        status="running",
        started_at=stale_time,
        heartbeat_at=stale_time,
        message="Refreshing Provisor PLK...",
    )
    db.add(job)
    db.commit()

    blocker = svc.active_or_stale_refresh_job(db)

    assert blocker is not None
    assert blocker.status == "stale"
    assert blocker.error_message


def test_selected_mode_returns_no_targets_when_no_selected_plk():
    db = _session()
    db.add(PriceFormat(code="FMT", name="Format"))
    db.commit()

    assert svc.selected_refresh_targets(db) == {}


def test_selected_mode_uses_assigned_active_provisor_plk():
    db = _session()
    pf = PriceFormat(code="FMT", name="Format")
    db.add(pf)
    db.flush()
    cpl = CompetitorPriceList(
        price_format_id=pf.id,
        source_type="provisor",
        source_key="3:128",
        account_id="3",
        external_price_list_id="128",
        display_name="Filial 128",
    )
    db.add(cpl)
    db.flush()
    db.add(PriceFormatCompetitorAssignment(price_format_id=pf.id, competitor_price_list_id=cpl.id, is_active=True))
    db.commit()

    assert svc.selected_refresh_targets(db) == {"FMT": {"3": {"128"}}}


def test_full_mode_targets_active_accounts_and_excluded_filials_env(monkeypatch):
    db = _session()
    db.add_all(
        [
            PriceFormat(code="FMT", name="Format"),
            PriceSourceAccount(id=3, source_type="provisor", login="A", encrypted_password="x", is_active=True),
            PriceSourceAccount(id=4, source_type="provisor", login="B", encrypted_password="x", is_active=False),
        ]
    )
    db.commit()
    monkeypatch.setenv("PROVISOR_EXCLUDED_FILIAL_IDS", "1052,1106")
    import backend.app.main as main

    assert svc.all_refresh_targets(db) == {"FMT": {"3": set()}}
    assert main._provisor_excluded_filial_ids() == {"1052", "1106"}
