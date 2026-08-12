from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import Base
from backend.app.models import Job, RefreshJob, RefreshLock
from backend.app.services.emit_worker import EmitConfig
from backend.app.services.provisor_auto_refresh import finish_job as finish_refresh_job, release_lock, release_global_refresh_lock
from backend.app.services.refresh_queue import (
    claim_next_queued_refresh_job,
    create_queued_emit_refresh_job,
    create_queued_provisor_auto_refresh_job,
)


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _copy_settings(settings, **updates):
    if hasattr(settings, "model_copy"):
        return settings.model_copy(update=updates)
    return settings.copy(update=updates)


def test_web_price_format_provisor_refresh_queues_without_parser_execution_or_locks(monkeypatch):
    import backend.app.main as main

    Session = _session_factory()
    monkeypatch.setattr(main, "settings", _copy_settings(main.settings, process_role="web", environment="prod"))
    monkeypatch.setattr(main, "schedule_job", lambda *args, **kwargs: pytest.fail("WEB must not schedule parser work"))
    monkeypatch.setattr(main.asyncio, "create_task", lambda *args, **kwargs: pytest.fail("WEB must not create parser tasks"))

    with Session() as db:
        response = asyncio.run(
            main.refresh_competitor_price_lists(
                "OPT",
                {"source": "provisor", "accountIds": [1], "filialIds": [1106]},
                db=db,
            )
        )
        job = db.get(RefreshJob, int(response["job_id"]))
        assert job is not None
        assert job.status == "queued"
        assert job.source_type == "provisor"
        assert db.execute(select(RefreshLock)).scalars().all() == []
        metadata = json.loads(job.metadata_json)
        assert metadata["queue_kind"] == "price_format_refresh"
        assert metadata["format_code"] == "OPT"


def test_web_emit_run_now_queues_without_parser_execution_or_locks(monkeypatch):
    import backend.app.main as main

    Session = _session_factory()
    config = EmitConfig(temp_dir="unused", filial_ids=[1106])
    fake_worker = SimpleNamespace(
        config=config,
        create_job=lambda *args, **kwargs: pytest.fail("WEB must not create execution-owned Emit jobs"),
        run_job=lambda *args, **kwargs: pytest.fail("WEB must not run Emit parser work"),
    )
    monkeypatch.setattr(main, "settings", _copy_settings(main.settings, process_role="web", environment="prod"))
    monkeypatch.setattr(main, "_emit_worker_instance", lambda: fake_worker)
    monkeypatch.setattr(main.asyncio, "create_task", lambda *args, **kwargs: pytest.fail("WEB must not create parser tasks"))

    with Session() as db:
        response = asyncio.run(main.run_emit_refresh_now({"mode": "selected", "filialIds": [1106]}, db=db))
        job = db.get(RefreshJob, int(response["job_id"]))
        assert job is not None
        assert job.status == "queued"
        assert job.source_type == "emit"
        assert db.execute(select(RefreshLock)).scalars().all() == []


def test_worker_claims_queued_provisor_job_and_executes_once(monkeypatch):
    import backend.app.main as main

    Session = _session_factory()
    calls: list[int] = []
    monkeypatch.setattr(main, "SessionLocal", Session)

    async def fake_run(job_id: int, *, mode: str, requested_by: str, owner_token: str | None = None):
        calls.append(job_id)
        with Session() as db:
            job = db.get(RefreshJob, job_id)
            assert job is not None
            finish_refresh_job(
                db,
                job,
                status="success",
                message="ok",
                owner_token=owner_token,
                allowed_statuses={"running"},
                release_refresh=True,
                release_global=True,
            )

    monkeypatch.setattr(main, "_run_provisor_refresh_job", fake_run)
    with Session() as db:
        queued = create_queued_provisor_auto_refresh_job(db, mode="selected", requested_by="manual")
    with Session() as db:
        claimed, token = claim_next_queued_refresh_job(db, worker_id="worker-a")

    assert claimed is not None
    assert token
    asyncio.run(main._execute_claimed_refresh_job(int(claimed.id), owner_token=token))

    with Session() as db:
        saved = db.get(RefreshJob, queued.id)
        assert saved is not None
        assert saved.status == "success"
        assert calls == [queued.id]


def test_worker_claims_queued_emit_job_and_executes_once(monkeypatch):
    import backend.app.main as main

    Session = _session_factory()
    calls: list[int] = []
    monkeypatch.setattr(main, "SessionLocal", Session)

    class FakeEmitWorker:
        async def run_job(self, job_id: int, *, owner_token: str | None = None):
            calls.append(job_id)
            with Session() as db:
                job = db.get(RefreshJob, job_id)
                assert job is not None
                finish_refresh_job(
                    db,
                    job,
                    status="success",
                    message="ok",
                    owner_token=owner_token,
                    allowed_statuses={"running"},
                    release_global=False,
                )
                release_lock(db, name="emit_refresh", owner_token=owner_token or "")
                release_global_refresh_lock(db, owner_token=owner_token or "")

    monkeypatch.setattr(main, "_emit_worker_instance", lambda: FakeEmitWorker())
    with Session() as db:
        queued = create_queued_emit_refresh_job(db, mode="selected", filial_ids=[1106], requested_by="manual")
    with Session() as db:
        claimed, token = claim_next_queued_refresh_job(db, worker_id="worker-a")

    assert claimed is not None
    assert token
    asyncio.run(main._execute_claimed_refresh_job(int(claimed.id), owner_token=token))

    with Session() as db:
        saved = db.get(RefreshJob, queued.id)
        assert saved is not None
        assert saved.status == "success"
        assert calls == [queued.id]


def test_two_concurrent_claim_attempts_cannot_claim_same_job():
    Session = _session_factory()
    with Session() as db:
        queued = create_queued_provisor_auto_refresh_job(db, mode="selected", requested_by="manual")

    def claim(worker_id: str):
        with Session() as db:
            job, _token = claim_next_queued_refresh_job(db, worker_id=worker_id)
            return job.id if job is not None else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["worker-a", "worker-b"]))

    assert results.count(queued.id) == 1
    assert results.count(None) == 1


def test_worker_marks_error_and_releases_locks_when_executor_raises(monkeypatch):
    import backend.app.main as main

    Session = _session_factory()
    monkeypatch.setattr(main, "SessionLocal", Session)

    async def fail_run(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "_run_provisor_refresh_job", fail_run)
    with Session() as db:
        queued = create_queued_provisor_auto_refresh_job(db, mode="selected", requested_by="manual")
    with Session() as db:
        claimed, token = claim_next_queued_refresh_job(db, worker_id="worker-a")

    assert claimed is not None
    assert token
    asyncio.run(main._execute_claimed_refresh_job(int(claimed.id), owner_token=token))

    with Session() as db:
        saved = db.get(RefreshJob, queued.id)
        assert saved is not None
        assert saved.status == "failed"
        assert "boom" in (saved.error_message or "")
        assert db.execute(select(RefreshLock)).scalars().all() == []


def test_existing_active_refresh_prevents_duplicate_web_enqueue(monkeypatch):
    import backend.app.main as main

    Session = _session_factory()
    monkeypatch.setattr(main, "settings", _copy_settings(main.settings, process_role="web", environment="prod"))
    with Session() as db:
        create_queued_provisor_auto_refresh_job(db, mode="selected", requested_by="manual")
        with pytest.raises(main.HTTPException) as exc:
            asyncio.run(main.run_provisor_auto_refresh_now({"mode": "selected"}, db=db))
        assert exc.value.status_code == 409


def test_jobs_endpoint_compatibility_reads_refresh_jobs(monkeypatch):
    import backend.app.main as main

    Session = _session_factory()
    with Session() as db:
        queued = create_queued_provisor_auto_refresh_job(db, mode="selected", requested_by="manual")
        response = main.get_job(str(queued.id), db=db)

    assert response["id"] == str(queued.id)
    assert response["status"] == "queued"
    assert response["type"] == "refresh_jobs:provisor"


def test_process_role_all_keeps_existing_in_process_refresh_path(monkeypatch):
    import backend.app.main as main

    Session = _session_factory()
    scheduled: list[str] = []
    monkeypatch.setattr(main, "settings", _copy_settings(main.settings, process_role="all", environment="prod"))
    monkeypatch.setattr(main, "schedule_job", lambda job_id, operation: scheduled.append(job_id))

    with Session() as db:
        response = asyncio.run(main.refresh_competitor_price_lists("OPT", {"source": "vidman"}, db=db))
        old_job = db.get(Job, response["job_id"])
        queued = db.execute(select(RefreshJob)).scalars().all()

    assert old_job is not None
    assert scheduled == [response["job_id"]]
    assert queued == []
