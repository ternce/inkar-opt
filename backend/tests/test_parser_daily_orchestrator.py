from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db import Base
from app.models import ParserDailyRun, PriceFormat, PriceSourceAccount, RefreshJob, RefreshLock
from app.services.parser_daily_orchestrator import (
    EMIT_RETRYING,
    EMIT_SUSPICIOUS,
    FAILED,
    PENDING,
    PLK_RUNNING,
    SUCCESS,
    SUSPICIOUS,
    advance_daily_run,
    configured_morning_accounts,
    create_or_get_daily_run,
    historical_filial_counts,
    validate_emit_job,
)


def _session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'daily.db').as_posix()}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _config(monkeypatch):
    monkeypatch.setenv("EMIT_FILIAL_IDS", "1106")
    monkeypatch.setenv("EMIT_MIN_FINAL_ROWS", "100")
    monkeypatch.setenv("PARSER_DAILY_BASELINE_WINDOW", "7")
    monkeypatch.setenv("PARSER_DAILY_BASELINE_MIN_SAMPLES", "3")
    monkeypatch.setenv("PARSER_DAILY_BASELINE_RATIO", "0.65")
    monkeypatch.setenv("PARSER_DAILY_SCHEDULE_ENABLED", "false")
    get_settings.cache_clear()
    return get_settings()


def _historical_emit(db, *, filial_id: int, rows: int, day: int):
    db.add(
        RefreshJob(
            source_type="emit",
            status="success",
            finished_at=datetime(2026, 1, day),
            metadata_json=json.dumps(
                {"filials": [{"ok": True, "filial_id": filial_id, "final_rows_saved": rows, "final_inserted_row_count": rows}]}
            ),
        )
    )


def _seed_pipeline_dependencies(db):
    db.add(PriceFormat(code="PF-1", name="PF 1"))
    db.add(
        PriceSourceAccount(
            source_type="provisor",
            login="morning",
            encrypted_password="x",
            is_active=True,
            run_in_morning_pipeline=True,
        )
    )
    db.commit()


def _complete_emit(db, job_id: int, *, status="success", rows=1000, error="", owner_token=""):
    job = db.get(RefreshJob, job_id)
    job.status = status
    job.finished_at = datetime.utcnow()
    job.error_message = error
    job.metadata_json = json.dumps(
        {
            "requested_filials": [1106],
            "owner_token": owner_token,
            "filials": [
                {
                    "ok": status == "success",
                    "filial_id": 1106,
                    "final_rows_saved": rows,
                    "final_inserted_row_count": rows,
                    "final_production_row_count": rows,
                    "error": error,
                }
            ],
        }
    )
    db.commit()


def test_one_daily_run_per_business_date_is_idempotent(tmp_path):
    Session = _session_factory(tmp_path)
    with Session() as db:
        first, created = create_or_get_daily_run(db, business_date=date(2026, 10, 11), requested_by="a")
        second, created_again = create_or_get_daily_run(db, business_date=date(2026, 10, 11), requested_by="b")
        assert created is True
        assert created_again is False
        assert first.id == second.id
        assert db.execute(select(ParserDailyRun)).scalars().all() == [first]


def test_configured_morning_accounts_excludes_inactive_and_non_provisor(tmp_path):
    Session = _session_factory(tmp_path)
    with Session() as db:
        db.add_all(
            [
                PriceSourceAccount(source_type="provisor", login="yes", encrypted_password="x", is_active=True, run_in_morning_pipeline=True),
                PriceSourceAccount(source_type="provisor", login="inactive", encrypted_password="x", is_active=False, run_in_morning_pipeline=True),
                PriceSourceAccount(source_type="provisor", login="off", encrypted_password="x", is_active=True, run_in_morning_pipeline=False),
                PriceSourceAccount(source_type="vidman", login="vidman", encrypted_password="x", is_active=True, run_in_morning_pipeline=True),
            ]
        )
        db.commit()
        assert [row.login for row in configured_morning_accounts(db)] == ["yes"]


def test_healthy_emit_enqueues_plk_and_plk_success_finishes(tmp_path, monkeypatch):
    cfg = _config(monkeypatch)
    Session = _session_factory(tmp_path)
    with Session() as db:
        _seed_pipeline_dependencies(db)
        run, _ = create_or_get_daily_run(db, business_date=date(2026, 10, 11), requested_by="admin")
        run = advance_daily_run(db, run.id, settings=cfg)
        assert run.emit_attempt_count == 1
        _complete_emit(db, run.emit_job_id, rows=1000)
        run = advance_daily_run(db, run.id, settings=cfg)
        assert run.status == PLK_RUNNING
        assert run.plk_job_id is not None
        assert db.get(RefreshJob, run.plk_job_id).daily_run_id == run.id
        plk = db.get(RefreshJob, run.plk_job_id)
        plk.status = "success"
        plk.failed_count = 0
        db.commit()
        run = advance_daily_run(db, run.id, settings=cfg)
        assert run.status == SUCCESS


def test_failed_emit_blocks_plk_without_retry_for_auth_failure(tmp_path, monkeypatch):
    cfg = _config(monkeypatch)
    Session = _session_factory(tmp_path)
    with Session() as db:
        _seed_pipeline_dependencies(db)
        run, _ = create_or_get_daily_run(db, business_date=date(2026, 10, 11), requested_by="admin")
        run = advance_daily_run(db, run.id, settings=cfg)
        _complete_emit(db, run.emit_job_id, status="error", error="HTTP 401 unauthorized")
        run = advance_daily_run(db, run.id, settings=cfg)
        assert run.status == FAILED
        assert run.plk_job_id is None
        assert run.emit_attempt_count == 1


def test_partial_emit_retries_once_with_new_refresh_job_then_stops(tmp_path, monkeypatch):
    cfg = _config(monkeypatch)
    Session = _session_factory(tmp_path)
    with Session() as db:
        _seed_pipeline_dependencies(db)
        run, _ = create_or_get_daily_run(db, business_date=date(2026, 10, 11), requested_by="admin")
        run = advance_daily_run(db, run.id, settings=cfg)
        first_job_id = run.emit_job_id
        _complete_emit(db, first_job_id, status="partial_success", error="timeout")
        run = advance_daily_run(db, run.id, settings=cfg)
        assert run.status == EMIT_RETRYING
        assert run.emit_attempt_count == 2
        assert run.emit_job_id != first_job_id
        _complete_emit(db, run.emit_job_id, status="partial_success", error="timeout")
        run = advance_daily_run(db, run.id, settings=cfg)
        assert run.status == SUSPICIOUS
        assert run.plk_job_id is None
        assert len(db.execute(select(RefreshJob).where(RefreshJob.daily_run_id == run.id)).scalars().all()) == 2


def test_low_row_count_does_not_release_owner_lock(tmp_path, monkeypatch):
    cfg = _config(monkeypatch)
    Session = _session_factory(tmp_path)
    with Session() as db:
        _seed_pipeline_dependencies(db)
        run, _ = create_or_get_daily_run(db, business_date=date(2026, 10, 11), requested_by="admin")
        run = advance_daily_run(db, run.id, settings=cfg)
        _complete_emit(db, run.emit_job_id, rows=10, owner_token="owner-a")
        db.add(
            RefreshLock(
                name="emit_refresh",
                owner_token="owner-a",
                lock_type="refresh",
                acquired_at=datetime.utcnow(),
                heartbeat_at=datetime.utcnow(),
                lease_until=datetime.utcnow() + timedelta(hours=1),
                metadata_json="{}",
            )
        )
        db.commit()
        run = advance_daily_run(db, run.id, settings=cfg)
        assert run.status == EMIT_SUSPICIOUS
        assert run.emit_attempt_count == 1
        assert db.get(RefreshLock, "emit_refresh") is not None


def test_historical_baseline_is_per_filial_latest_seven_healthy(tmp_path):
    Session = _session_factory(tmp_path)
    with Session() as db:
        for index, rows in enumerate([100, 200, 300, 400, 500, 600, 700, 800], start=1):
            db.add(
                RefreshJob(
                    source_type="emit",
                    status="success",
                    finished_at=datetime(2026, 1, index),
                    metadata_json=json.dumps({"filials": [{"ok": True, "filial_id": 1106, "final_rows_saved": rows, "final_inserted_row_count": rows}]}),
                )
            )
        db.add(
            RefreshJob(
                source_type="emit",
                status="success",
                finished_at=datetime(2026, 2, 1),
                metadata_json=json.dumps({"filials": [{"ok": True, "filial_id": 9999, "final_rows_saved": 9999, "final_inserted_row_count": 9999}]}),
            )
        )
        db.commit()
        counts = historical_filial_counts(db, filial_id=1106, exclude_job_id=None, window=7, minimum=100)
        assert counts == [800, 700, 600, 500, 400, 300, 200]


def test_65_percent_baseline_threshold_and_account_specific_history(tmp_path, monkeypatch):
    cfg = _config(monkeypatch)
    Session = _session_factory(tmp_path)
    with Session() as db:
        for day in (1, 2, 3):
            _historical_emit(db, filial_id=1106, rows=1000, day=day)
            _historical_emit(db, filial_id=9999, rows=10000, day=day + 3)
        db.commit()
        current = RefreshJob(source_type="emit", status="success", metadata_json="{}")
        db.add(current)
        db.flush()
        _complete_emit(db, current.id, rows=640)
        current = db.get(RefreshJob, current.id)
        validation = validate_emit_job(db, current, settings=cfg)
        assert validation.suspicious is True
        assert validation.retryable is True
        assert "640<650.00" in validation.reason


def test_fewer_than_three_baseline_samples_does_not_fail(tmp_path, monkeypatch):
    cfg = _config(monkeypatch)
    Session = _session_factory(tmp_path)
    with Session() as db:
        _historical_emit(db, filial_id=1106, rows=1000, day=1)
        _historical_emit(db, filial_id=1106, rows=1000, day=2)
        db.commit()
        current = RefreshJob(source_type="emit", status="success", metadata_json="{}")
        db.add(current)
        db.flush()
        _complete_emit(db, current.id, rows=100)
        current = db.get(RefreshJob, current.id)
        validation = validate_emit_job(db, current, settings=cfg)
        assert validation.healthy is True
        assert validation.details["baselines"]["1106"]["confidence"] == "insufficient"


def test_mixed_plk_failure_cannot_finalize_daily_success(tmp_path, monkeypatch):
    cfg = _config(monkeypatch)
    Session = _session_factory(tmp_path)
    with Session() as db:
        _seed_pipeline_dependencies(db)
        run, _ = create_or_get_daily_run(db, business_date=date(2026, 10, 11), requested_by="admin")
        run = advance_daily_run(db, run.id, settings=cfg)
        _complete_emit(db, run.emit_job_id, rows=1000)
        run = advance_daily_run(db, run.id, settings=cfg)
        plk = db.get(RefreshJob, run.plk_job_id)
        plk.status = "partial_success"
        plk.success_count = 1
        plk.failed_count = 1
        db.commit()
        run = advance_daily_run(db, run.id, settings=cfg)
        assert run.status == FAILED


def test_new_session_resumes_persisted_pending_run(tmp_path, monkeypatch):
    cfg = _config(monkeypatch)
    Session = _session_factory(tmp_path)
    with Session() as db:
        run, _ = create_or_get_daily_run(db, business_date=date(2026, 10, 11), requested_by="admin")
        run_id = run.id
        assert run.status == PENDING
    with Session() as restarted_db:
        resumed = advance_daily_run(restarted_db, run_id, settings=cfg)
        assert resumed.emit_job_id is not None
        assert resumed.emit_attempt_count == 1


def teardown_module():
    get_settings.cache_clear()
