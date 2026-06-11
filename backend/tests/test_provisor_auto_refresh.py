from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import (
    CompetitorPriceList,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    PriceSourceAccount,
    RefreshJob,
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
