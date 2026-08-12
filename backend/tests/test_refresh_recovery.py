from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import RefreshJob, RefreshLock
from backend.app.services.db_time import db_now
from backend.app.services.emit_worker import EmitConfig
from backend.app.services.refresh_queue import (
    claim_next_queued_refresh_job,
    create_queued_provisor_auto_refresh_job,
    recover_expired_orphan_locks,
    recover_stale_parser_jobs,
)
from backend.app.services.provisor_auto_refresh import GLOBAL_REFRESH_LOCK_NAME, REFRESH_LOCK_LEASE


def _session_factory(path: Path | None = None):
    if path is None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_lock(db, *, name: str, token: str, expired: bool = False) -> RefreshLock:
    now = db_now(db)
    lock = RefreshLock(
        name=name,
        owner_token=token,
        lock_type="refresh",
        acquired_at=now,
        heartbeat_at=now,
        lease_until=now - timedelta(seconds=1) if expired else now + REFRESH_LOCK_LEASE,
        metadata_json="{}",
    )
    db.add(lock)
    db.commit()
    db.refresh(lock)
    return lock


def _seed_running_job(
    db,
    *,
    source_type: str,
    status: str = "running",
    token: str = "owner-a",
    heartbeat_age_seconds: int = 999,
    metadata_extra: dict | None = None,
) -> RefreshJob:
    now = db_now(db)
    metadata = {
        "owner_token": token,
        "worker_owner_id": f"worker-{token}",
        "worker_pid": 123,
        "claimed_at": (now - timedelta(seconds=heartbeat_age_seconds + 10)).isoformat(),
        "current_stage": status,
    }
    metadata.update(metadata_extra or {})
    job = RefreshJob(
        source_type=source_type,
        mode="selected",
        status=status,
        started_at=now - timedelta(seconds=heartbeat_age_seconds + 20),
        heartbeat_at=now - timedelta(seconds=heartbeat_age_seconds),
        requested_by="test",
        message="running",
        metadata_json=json.dumps(metadata),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_fresh_heartbeat_running_job_is_not_recovered():
    Session = _session_factory()
    with Session() as db:
        job = _seed_running_job(db, source_type="emit", heartbeat_age_seconds=30)
        recovered = recover_stale_parser_jobs(db, stale_heartbeat_seconds=180, emit_config=EmitConfig(temp_dir="unused"))
        db.refresh(job)
        assert recovered == []
        assert job.status == "running"


def test_stale_emit_running_job_becomes_interrupted_and_releases_matching_locks():
    Session = _session_factory()
    with Session() as db:
        job = _seed_running_job(db, source_type="emit", status="parsing", token="emit-token")
        _seed_lock(db, name=GLOBAL_REFRESH_LOCK_NAME, token="emit-token")
        _seed_lock(db, name="emit_refresh", token="emit-token")
        recovered = recover_stale_parser_jobs(db, stale_heartbeat_seconds=180, emit_config=EmitConfig(temp_dir="unused"))
        db.refresh(job)
        assert [row.id for row in recovered] == [job.id]
        assert job.status == "interrupted"
        assert "worker_heartbeat_stale" in job.error_message
        assert db.get(RefreshLock, GLOBAL_REFRESH_LOCK_NAME) is None
        assert db.get(RefreshLock, "emit_refresh") is None


def test_stale_provisor_running_job_becomes_interrupted_and_releases_matching_locks():
    Session = _session_factory()
    with Session() as db:
        job = _seed_running_job(db, source_type="provisor", token="provisor-token")
        _seed_lock(db, name=GLOBAL_REFRESH_LOCK_NAME, token="provisor-token")
        _seed_lock(db, name="provisor_auto_refresh", token="provisor-token")
        recover_stale_parser_jobs(db, stale_heartbeat_seconds=180, emit_config=EmitConfig(temp_dir="unused"))
        db.refresh(job)
        assert job.status == "interrupted"
        assert db.get(RefreshLock, GLOBAL_REFRESH_LOCK_NAME) is None
        assert db.get(RefreshLock, "provisor_auto_refresh") is None


def test_recovery_preserves_unexpired_foreign_lock():
    Session = _session_factory()
    with Session() as db:
        _seed_running_job(db, source_type="emit", token="dead-token")
        _seed_lock(db, name=GLOBAL_REFRESH_LOCK_NAME, token="fresh-token", expired=False)
        recover_stale_parser_jobs(db, stale_heartbeat_seconds=180, emit_config=EmitConfig(temp_dir="unused"))
        lock = db.get(RefreshLock, GLOBAL_REFRESH_LOCK_NAME)
        assert lock is not None
        assert lock.owner_token == "fresh-token"


def test_recovery_releases_expired_foreign_lock():
    Session = _session_factory()
    with Session() as db:
        _seed_running_job(db, source_type="emit", token="dead-token")
        _seed_lock(db, name=GLOBAL_REFRESH_LOCK_NAME, token="old-token", expired=True)
        recover_stale_parser_jobs(db, stale_heartbeat_seconds=180, emit_config=EmitConfig(temp_dir="unused"))
        assert db.get(RefreshLock, GLOBAL_REFRESH_LOCK_NAME) is None


def test_expired_orphan_lock_can_be_cleared():
    Session = _session_factory()
    with Session() as db:
        _seed_lock(db, name=GLOBAL_REFRESH_LOCK_NAME, token="old-token", expired=True)
        assert recover_expired_orphan_locks(db) == [GLOBAL_REFRESH_LOCK_NAME]
        assert db.get(RefreshLock, GLOBAL_REFRESH_LOCK_NAME) is None


def test_active_unexpired_orphan_lock_is_preserved():
    Session = _session_factory()
    with Session() as db:
        _seed_lock(db, name=GLOBAL_REFRESH_LOCK_NAME, token="live-token", expired=False)
        assert recover_expired_orphan_locks(db) == []
        assert db.get(RefreshLock, GLOBAL_REFRESH_LOCK_NAME) is not None


def test_stale_emit_temp_json_and_sqlite_deleted_but_other_job_file_preserved(tmp_path: Path):
    Session = _session_factory()
    temp_json = tmp_path / "emit_1106_dead.json"
    stage_db = tmp_path / "emit_stage_1_1106_dead.sqlite"
    other_json = tmp_path / "emit_1107_live.json"
    for path in (temp_json, stage_db, other_json):
        path.write_text("x", encoding="utf-8")
    with Session() as db:
        _seed_running_job(
            db,
            source_type="emit",
            token="dead-token",
            metadata_extra={"temp_file_path": str(temp_json), "stage_db_path": str(stage_db)},
        )
        _seed_running_job(
            db,
            source_type="emit",
            token="live-token",
            heartbeat_age_seconds=10,
            metadata_extra={"temp_file_path": str(other_json)},
        )
        recover_stale_parser_jobs(db, stale_heartbeat_seconds=180, emit_config=EmitConfig(temp_dir=str(tmp_path)))
        assert not temp_json.exists()
        assert not stage_db.exists()
        assert other_json.exists()


def test_queued_unclaimed_job_is_not_recovered():
    Session = _session_factory()
    with Session() as db:
        job = create_queued_provisor_auto_refresh_job(db, mode="selected", requested_by="test")
        recovered = recover_stale_parser_jobs(db, stale_heartbeat_seconds=30, emit_config=EmitConfig(temp_dir="unused"))
        db.refresh(job)
        assert recovered == []
        assert job.status == "queued"


def test_recovery_is_safe_when_executed_concurrently_twice(tmp_path: Path):
    Session = _session_factory(tmp_path / "recovery.sqlite")
    with Session() as db:
        job = _seed_running_job(db, source_type="provisor", token="token-a")
        job_id = int(job.id)
        _seed_lock(db, name=GLOBAL_REFRESH_LOCK_NAME, token="token-a")

    def recover_once():
        with Session() as db:
            return len(recover_stale_parser_jobs(db, stale_heartbeat_seconds=180, emit_config=EmitConfig(temp_dir="unused")))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: recover_once(), range(2)))

    with Session() as db:
        saved = db.get(RefreshJob, job_id)
        assert saved is not None
        assert saved.status == "interrupted"
        assert sum(results) == 1


def test_new_queued_job_claimable_after_stale_recovery():
    Session = _session_factory()
    with Session() as db:
        _seed_running_job(db, source_type="provisor", token="dead-token")
        _seed_lock(db, name=GLOBAL_REFRESH_LOCK_NAME, token="dead-token")
        queued = create_queued_provisor_auto_refresh_job(db, mode="selected", requested_by="manual")
        queued_id = int(queued.id)
        recover_stale_parser_jobs(db, stale_heartbeat_seconds=180, emit_config=EmitConfig(temp_dir="unused"))
    with Session() as db:
        claimed, token = claim_next_queued_refresh_job(db, worker_id="worker-b")
        assert claimed is not None
        assert claimed.id == queued_id
        assert token


def test_db_time_lease_logic_ignores_mocked_application_clock_skew(monkeypatch):
    import backend.app.services.refresh_queue as queue

    class SkewedDateTime:
        @classmethod
        def utcnow(cls):
            return datetime(2099, 1, 1)

        @classmethod
        def fromisoformat(cls, value):
            return datetime.fromisoformat(value)

    Session = _session_factory()
    monkeypatch.setattr(queue, "datetime", SkewedDateTime)
    with Session() as db:
        _seed_lock(db, name=GLOBAL_REFRESH_LOCK_NAME, token="live-token", expired=False)
        create_queued_provisor_auto_refresh_job(db, mode="selected", requested_by="manual")
        claimed, token = claim_next_queued_refresh_job(db, worker_id="worker-b")
        assert claimed is None
        assert token is None


def test_worker_polling_survives_temporary_database_exception(monkeypatch):
    import backend.app.worker as worker

    stop_event = asyncio.Event()
    calls = {"count": 0}

    class BrokenSession:
        def __enter__(self):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary db outage")
            stop_event.set()
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(worker, "SessionLocal", lambda: BrokenSession())

    asyncio.run(
        asyncio.wait_for(
            worker._poll_refresh_queue(stop_event=stop_event, worker_id="worker-a", poll_interval=0.01),
            timeout=2,
        )
    )
    assert calls["count"] >= 2


def test_graceful_cancel_marks_current_job_interrupted_and_releases_locks(monkeypatch):
    import backend.app.main as main

    Session = _session_factory()
    monkeypatch.setattr(main, "SessionLocal", Session)

    async def cancelled_run(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(main, "_run_provisor_refresh_job", cancelled_run)
    with Session() as db:
        queued = create_queued_provisor_auto_refresh_job(db, mode="selected", requested_by="manual")
        queued_id = int(queued.id)
    with Session() as db:
        claimed, token = claim_next_queued_refresh_job(db, worker_id="worker-a")

    assert claimed is not None
    assert token
    try:
        asyncio.run(main._execute_claimed_refresh_job(int(claimed.id), owner_token=token))
    except asyncio.CancelledError:
        pass

    with Session() as db:
        saved = db.get(RefreshJob, queued_id)
        assert saved is not None
        assert saved.status == "interrupted"
        assert saved.error_message == "worker_shutdown"
        assert db.execute(select(RefreshLock)).scalars().all() == []
