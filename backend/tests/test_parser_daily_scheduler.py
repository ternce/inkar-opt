from __future__ import annotations

import asyncio
import sys
import types
from datetime import date, datetime, timezone

import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.config import get_settings
from backend.app.db import Base
from backend.app.models import ParserDailyRun, RefreshJob
from backend.app.services.parser_daily_orchestrator import current_business_date


def _session_factory(tmp_path, name="daily-scheduler.db"):
    engine = create_engine(f"sqlite:///{(tmp_path / name).as_posix()}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _settings(**updates):
    values = {
        "process_role": "worker",
        "app_timezone": "Asia/Qyzylorda",
        "parser_daily_schedule_enabled": True,
        "parser_daily_schedule_cron": "0 2 * * *",
        "parser_daily_catchup_max_hours": 6.0,
        "parser_daily_schedule_until_date": None,
        "provisor_auto_refresh_enabled": False,
        "emit_worker_enabled": False,
    }
    values.update(updates)
    return get_settings().model_copy(update=values)


class _FakeJob:
    next_run_time = None


class _FakeScheduler:
    instances: list["_FakeScheduler"] = []

    def __init__(self, timezone):
        self.timezone = timezone
        self.jobs = []
        self.started = False
        self.shutdown_calls = []
        self.__class__.instances.append(self)

    def add_job(self, *args, **kwargs):
        self.jobs.append((args, kwargs))

    def get_job(self, job_id):
        return _FakeJob()

    def start(self):
        self.started = True

    def shutdown(self, wait=False):
        self.shutdown_calls.append(wait)


def _install_fake_scheduler(monkeypatch, main):
    scheduler_module = types.ModuleType("apscheduler.schedulers.asyncio")
    scheduler_module.AsyncIOScheduler = _FakeScheduler
    trigger_module = types.ModuleType("apscheduler.triggers.cron")
    trigger_module.CronTrigger = types.SimpleNamespace(
        from_crontab=lambda cron, timezone=None: ("cron", cron, timezone)
    )
    _FakeScheduler.instances = []
    monkeypatch.setitem(sys.modules, "apscheduler.schedulers.asyncio", scheduler_module)
    monkeypatch.setitem(sys.modules, "apscheduler.triggers.cron", trigger_module)
    monkeypatch.setattr(main, "_run_parser_daily_startup_catchup", lambda _trigger: None)
    monkeypatch.setattr(main, "_parser_daily_scheduler", None)


def test_schedule_disabled_by_default(monkeypatch):
    import backend.app.main as main

    monkeypatch.delenv("PARSER_DAILY_SCHEDULE_ENABLED", raising=False)
    get_settings.cache_clear()
    cfg = get_settings()
    assert cfg.parser_daily_schedule_enabled is False
    monkeypatch.setattr(main, "settings", cfg.model_copy(update={"process_role": "worker"}))
    monkeypatch.setattr(main, "_parser_daily_scheduler", None)
    main._start_parser_daily_scheduler()
    assert main._parser_daily_scheduler is None


def test_enabled_worker_registers_exactly_one_job_and_restart_reconstructs_definition(monkeypatch):
    import backend.app.main as main

    _install_fake_scheduler(monkeypatch, main)
    monkeypatch.setattr(main, "settings", _settings())
    main._start_parser_daily_scheduler()
    main._start_parser_daily_scheduler()
    assert len(_FakeScheduler.instances) == 1
    scheduler = _FakeScheduler.instances[0]
    assert scheduler.started is True
    assert len(scheduler.jobs) == 1
    _, kwargs = scheduler.jobs[0]
    assert kwargs == {
        "trigger": ("cron", "0 2 * * *", "Asia/Qyzylorda"),
        "id": "parser_daily_morning",
        "name": "Parser daily morning orchestration trigger",
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 3600,
        "replace_existing": True,
    }


def test_web_role_does_not_register_parser_scheduler(monkeypatch):
    import backend.app.main as main

    _install_fake_scheduler(monkeypatch, main)
    monkeypatch.setattr(main, "settings", _settings(process_role="web"))
    main._start_parser_daily_scheduler()
    assert _FakeScheduler.instances == []
    assert main._parser_daily_scheduler is None


@pytest.mark.parametrize(
    ("provisor_enabled", "emit_enabled", "expected_flag"),
    [
        (True, False, "PROVISOR_AUTO_REFRESH_ENABLED"),
        (False, True, "EMIT_WORKER_ENABLED"),
    ],
)
def test_legacy_scheduler_conflict_rejects_worker_startup(
    monkeypatch, provisor_enabled, emit_enabled, expected_flag
):
    import backend.app.main as main

    monkeypatch.setattr(
        main,
        "settings",
        _settings(
            provisor_auto_refresh_enabled=provisor_enabled,
            emit_worker_enabled=emit_enabled,
        ),
    )
    with pytest.raises(RuntimeError, match=expected_flag):
        main._worker_startup()


def test_process_role_all_is_explicitly_rejected(monkeypatch):
    import backend.app.main as main

    monkeypatch.setattr(main, "settings", _settings(process_role="all"))
    with pytest.raises(RuntimeError, match="PROCESS_ROLE=all"):
        asyncio.run(main._startup())


def test_scheduled_trigger_creates_then_reuses_today_without_duplicate_child(monkeypatch, tmp_path):
    import backend.app.main as main

    Session = _session_factory(tmp_path)
    fixed_now = datetime(2026, 10, 11, 2, 0)
    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(main, "settings", _settings())
    monkeypatch.setattr(main, "_parser_daily_local_now", lambda now=None: fixed_now.replace(tzinfo=main.ZoneInfo("Asia/Qyzylorda")))

    asyncio.run(main._run_scheduled_parser_daily())
    with Session() as db:
        run = db.execute(select(ParserDailyRun)).scalars().one()
        db.add(RefreshJob(source_type="emit", status="queued", daily_run_id=run.id))
        db.commit()
        run_id = run.id
    asyncio.run(main._run_scheduled_parser_daily())
    with Session() as db:
        assert db.execute(select(func.count(ParserDailyRun.id))).scalar_one() == 1
        assert db.execute(select(func.count(RefreshJob.id))).scalar_one() == 1
        assert db.execute(select(ParserDailyRun.id)).scalar_one() == run_id


def test_business_date_uses_configured_timezone():
    cfg = _settings(app_timezone="Asia/Qyzylorda")
    utc_time = datetime(2026, 10, 10, 21, 30, tzinfo=timezone.utc)
    assert current_business_date(cfg, now=utc_time) == date(2026, 10, 11)


def test_catchup_creates_today_inside_window_and_restart_reuses_it(monkeypatch, tmp_path):
    import backend.app.main as main

    Session = _session_factory(tmp_path)
    cfg = _settings()
    trigger = CronTrigger.from_crontab(cfg.parser_daily_schedule_cron, timezone=cfg.app_timezone)
    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(main, "settings", cfg)
    now = datetime(2026, 10, 11, 5, 0, tzinfo=main.ZoneInfo(cfg.app_timezone))
    main._run_parser_daily_startup_catchup(trigger, now=now)
    main._run_parser_daily_startup_catchup(trigger, now=now)
    with Session() as db:
        rows = db.execute(select(ParserDailyRun)).scalars().all()
        assert len(rows) == 1
        assert rows[0].business_date == date(2026, 10, 11)
        assert rows[0].trigger_source == "catchup"


def test_catchup_outside_window_does_not_create_today_or_yesterday(monkeypatch, tmp_path):
    import backend.app.main as main

    Session = _session_factory(tmp_path)
    cfg = _settings(parser_daily_catchup_max_hours=4.0)
    trigger = CronTrigger.from_crontab(cfg.parser_daily_schedule_cron, timezone=cfg.app_timezone)
    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(main, "settings", cfg)
    now = datetime(2026, 10, 12, 10, 0, tzinfo=main.ZoneInfo(cfg.app_timezone))
    main._run_parser_daily_startup_catchup(trigger, now=now)
    with Session() as db:
        assert db.execute(select(func.count(ParserDailyRun.id))).scalar_one() == 0


def test_catchup_before_scheduled_time_does_not_create_run(monkeypatch, tmp_path):
    import backend.app.main as main

    Session = _session_factory(tmp_path)
    cfg = _settings()
    trigger = CronTrigger.from_crontab(cfg.parser_daily_schedule_cron, timezone=cfg.app_timezone)
    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(main, "settings", cfg)
    now = datetime(2026, 10, 11, 1, 0, tzinfo=main.ZoneInfo(cfg.app_timezone))
    main._run_parser_daily_startup_catchup(trigger, now=now)
    with Session() as db:
        assert db.execute(select(func.count(ParserDailyRun.id))).scalar_one() == 0


def test_until_date_allows_eligible_date_and_blocks_later_date(monkeypatch, tmp_path):
    import backend.app.main as main

    Session = _session_factory(tmp_path)
    cfg = _settings(parser_daily_schedule_until_date=date(2026, 10, 11))
    monkeypatch.setattr(main, "SessionLocal", Session)
    monkeypatch.setattr(main, "settings", cfg)
    allowed = datetime(2026, 10, 11, 2, 0, tzinfo=main.ZoneInfo(cfg.app_timezone))
    blocked = datetime(2026, 10, 12, 2, 0, tzinfo=main.ZoneInfo(cfg.app_timezone))
    row, created, _, reason = main._trigger_parser_daily_run(
        trigger_source="scheduled", requested_by="scheduler", now=allowed
    )
    assert row is not None and created is True and reason == ""
    row, created, _, reason = main._trigger_parser_daily_run(
        trigger_source="scheduled", requested_by="scheduler", now=blocked
    )
    assert row is None and created is False and reason == "after_until_date"
    with Session() as db:
        assert db.execute(select(func.count(ParserDailyRun.id))).scalar_one() == 1


def test_manual_run_now_remains_functional(monkeypatch, tmp_path):
    import backend.app.main as main

    Session = _session_factory(tmp_path)
    monkeypatch.setattr(main, "settings", _settings())
    with Session() as db:
        result = main.run_parser_daily_pipeline_now(
            db=db,
            current_user=types.SimpleNamespace(username="operator", id=7),
        )
        assert result["created"] is True
        assert result["triggerSource"] == "manual"


def teardown_module():
    get_settings.cache_clear()
