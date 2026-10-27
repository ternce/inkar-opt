from __future__ import annotations

import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.services import competitor_matching
from backend.app import main as main_module
from backend.app.models import PriceList
from backend.app.services.pricing_workflow import workflow as workflow_module
from backend.app.services.pricing_workflow.locks import (
    PRICE_FORMAT_LOCK_NAMESPACE,
    PricingWorkflowConflict,
    WorkflowLockKey,
    workflow_lock_retry_timeout_seconds,
)
from backend.app.services.pricing_workflow.timing import WorkflowTimingRecorder, sanitize_error_message
from backend.tests.test_pricing_engine_targeted_fixes import (
    _format,
    _product,
    _session,
    _workflow_context,
    _workflow_payload,
    _workflow_price_list_source,
)


class _Lease:
    def acquire(self, **kwargs):
        return None


def test_rebuild_rejects_changed_source_snapshot_before_matching(monkeypatch):
    monkeypatch.setattr(competitor_matching, "repair_manual_vidman_assignment_modes", lambda **kwargs: [])
    monkeypatch.setattr(
        competitor_matching,
        "get_assigned_competitor_price_lists",
        lambda **kwargs: [SimpleNamespace(price_list=SimpleNamespace(id=12))],
    )

    with pytest.raises(ValueError, match=r"expected=\[11\] active=\[12\]"):
        competitor_matching.rebuild_competitor_prices_for_selected(
            db=object(),
            price_format_id=7,
            expected_source_ids=[11],
            lock_lease=_Lease(),
        )


def test_stage_timing_records_success_and_failure_without_payload_data():
    recorder = WorkflowTimingRecorder(workflow_id=10, price_format_id=20, selected_source_ids=[30, 31])
    with recorder.stage("price_calculation", input_rows=200):
        pass
    with pytest.raises(TimeoutError):
        with recorder.stage("analytics_generation"):
            raise TimeoutError("deadline")

    metadata = recorder.metadata()
    assert metadata["selectedSourceIds"] == [30, 31]
    assert metadata["stages"][0]["success"] is True
    assert metadata["stages"][0]["inputRows"] == 200
    assert metadata["stages"][1]["success"] is False
    assert metadata["stages"][1]["error"] == "deadline"
    assert all(stage["durationMs"] >= 0 for stage in metadata["stages"])


def test_timing_uses_monotonic_elapsed_duration(monkeypatch):
    values = iter([100.0, 100.125])
    monkeypatch.setattr(time, "perf_counter", lambda: next(values))
    recorder = WorkflowTimingRecorder(workflow_id=1, price_format_id=2, selected_source_ids=[])
    with recorder.stage("workflow_initialization"):
        pass
    assert recorder.stages == [
        {"stage": "workflow_initialization", "durationMs": 125.0, "success": True}
    ]


def test_timing_error_metadata_redacts_common_credentials():
    message = sanitize_error_message(
        "connect postgresql://worker:secret@db/app password=hunter2 Authorization: Bearer abc.def"
    )
    assert "secret" not in message
    assert "hunter2" not in message
    assert "abc.def" not in message
    assert "worker:***@db" in message


def _workflow_fixture():
    db = _session()
    price_format = _format(db)
    product = _product(db, code="PHASE1-TIMING", cost=100)
    context = _workflow_context(db)
    source = _workflow_price_list_source(db, price_format, product=product, price=Decimal("150"))
    payload = _workflow_payload(context, price_format, source, percentile_sources=[], price_list_number="PHASE1-TIMING-PL")
    return db, payload


def test_matching_failure_is_persisted_with_failed_stage(monkeypatch):
    db, payload = _workflow_fixture()
    monkeypatch.setattr(
        workflow_module,
        "rebuild_competitor_prices_for_selected",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("matching failed")),
    )

    run = workflow_module.create_workflow_run(db=db, payload=payload)

    assert run.status == "error"
    metadata = json.loads(run.run_snapshot_json)["performance"]
    failed = [stage for stage in metadata["stages"] if stage["stage"] == "competitor_matching"]
    assert failed and failed[-1]["success"] is False
    assert failed[-1]["error"] == "matching failed"


def test_calculation_timeout_releases_workflow_and_persists_failed_stage(monkeypatch):
    db, payload = _workflow_fixture()
    monkeypatch.setattr(
        workflow_module,
        "rebuild_competitor_prices_for_selected",
        lambda **kwargs: {
            "_stageTimings": [
                {"stage": "competitor_matching", "durationMs": 1.0, "success": True},
                {"stage": "competitor_price_materialization", "durationMs": 1.0, "success": True},
                {"stage": "excel_audit_generation", "durationMs": 1.0, "success": True},
            ]
        },
    )
    monkeypatch.setattr(
        workflow_module,
        "calculate_prices",
        lambda **kwargs: (_ for _ in ()).throw(TimeoutError("calculation deadline")),
    )

    run = workflow_module.create_workflow_run(db=db, payload=payload)

    assert run.status == "error"
    metadata = json.loads(run.run_snapshot_json)["performance"]
    failed = [stage for stage in metadata["stages"] if stage["stage"] == "price_calculation"]
    assert failed and failed[-1]["success"] is False
    assert failed[-1]["error"] == "calculation deadline"


def test_success_survives_best_effort_timing_metadata_failure(monkeypatch):
    db, payload = _workflow_fixture()
    monkeypatch.setattr(
        workflow_module,
        "rebuild_competitor_prices_for_selected",
        lambda **kwargs: {"_stageTimings": []},
    )
    def fake_calculate_prices(**kwargs):
        db.add(
            PriceList(
                number=kwargs["price_list_number"],
                price_format_id=int(payload["priceFormatId"]),
                activation_date=kwargs.get("activation_date"),
                user=kwargs.get("user") or "test",
            )
        )
        db.flush()
        return 1

    monkeypatch.setattr(workflow_module, "calculate_prices", fake_calculate_prices)
    monkeypatch.setattr(
        workflow_module,
        "_persist_timing_metadata",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("timing write failed")),
    )

    run = workflow_module.create_workflow_run(db=db, payload=payload)

    assert run.status == "success"
    assert run.price_list_id is not None
    assert db.execute(select(PriceList).where(PriceList.id == run.price_list_id)).scalars().first() is not None


def test_workflow_snapshot_records_matching_cache_observability(monkeypatch):
    db, payload = _workflow_fixture()
    cache_metadata = {
        "mode": "required",
        "hit": True,
        "reason": "materialization_unchanged",
        "sourceVersionIds": [101],
        "matchingSnapshotIds": [202],
        "materializationId": 303,
        "materializationFingerprint": "f" * 64,
        "materializationReused": True,
        "materializedRows": 1,
        "cacheLookupDurationMs": 1.25,
        "materializationDurationMs": 0.0,
        "fullRematchInvocationCount": 0,
        "rawSourceRowsRead": 0,
    }
    monkeypatch.setattr(
        workflow_module,
        "rebuild_competitor_prices_for_selected",
        lambda **kwargs: {
            "_stageTimings": [],
            "_matchingCache": dict(cache_metadata),
        },
    )

    def fake_calculate_prices(**kwargs):
        db.add(PriceList(
            number=kwargs["price_list_number"],
            price_format_id=int(payload["priceFormatId"]),
            activation_date=kwargs.get("activation_date"),
            user=kwargs.get("user") or "test",
        ))
        db.flush()
        return 1

    monkeypatch.setattr(workflow_module, "calculate_prices", fake_calculate_prices)
    run = workflow_module.create_workflow_run(db=db, payload=payload)

    assert run.status == "success"
    assert json.loads(run.run_snapshot_json)["matchingCache"] == cache_metadata
    price_list = db.get(PriceList, run.price_list_id)
    assert json.loads(price_list.run_snapshot_json)["matchingCache"] == cache_metadata


def test_batch_all_conflicts_returns_409(monkeypatch):
    db, payload = _workflow_fixture()
    pf = db.get(workflow_module.PriceFormat, payload["priceFormatId"])
    context = db.get(workflow_module.PricingContext, payload["pricingContextId"])
    monkeypatch.setattr(main_module, "_workflow_context_for_branch", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(main_module, "_ensure_price_format_access", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_module, "_format_assignments", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        main_module,
        "create_workflow_run",
        lambda **_kwargs: SimpleNamespace(id=99, status="conflict", error="locked", price_list_number=""),
    )
    user = SimpleNamespace(username="tester")

    with pytest.raises(HTTPException) as caught:
        main_module.post_pricing_workflow_generate_batch(
            payload={"formatCodes": [pf.code]},
            db=db,
            current_user=user,
        )

    assert caught.value.status_code == 409
    assert caught.value.detail["items"][0]["status"] == "conflict"


def test_batch_mixed_conflict_and_success_remains_warning_200_shape(monkeypatch):
    db, payload = _workflow_fixture()
    pf = db.get(workflow_module.PriceFormat, payload["priceFormatId"])
    context = db.get(workflow_module.PricingContext, payload["pricingContextId"])
    monkeypatch.setattr(main_module, "_workflow_context_for_branch", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(main_module, "_ensure_price_format_access", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main_module, "_format_assignments", lambda *_args, **_kwargs: [])
    outcomes = iter(
        [
            SimpleNamespace(id=100, status="conflict", error="locked", price_list_number=""),
            SimpleNamespace(id=101, status="success", error="", price_list_number="PL-101"),
        ]
    )
    monkeypatch.setattr(main_module, "create_workflow_run", lambda **_kwargs: next(outcomes))

    result = main_module.post_pricing_workflow_generate_batch(
        payload={"formatCodes": [pf.code, pf.code]},
        db=db,
        current_user=SimpleNamespace(username="tester"),
    )

    assert result["status"] == "warning"
    assert result["completedFormats"] == 1
    assert result["failedFormats"] == 1
    assert [item["status"] for item in result["items"]] == ["conflict", "success"]


def test_workflow_conflict_status_contains_bounded_wait_diagnostics(monkeypatch):
    db, payload = _workflow_fixture()
    monkeypatch.setenv("MATCH_CACHE_MODE", "disabled")
    monkeypatch.setenv("PRICING_WORKFLOW_LOCK_TIMEOUT_SECONDS", "0.25")

    def reject_lock(self, **kwargs):
        assert kwargs["retry_timeout_seconds"] == pytest.approx(0.25)
        assert kwargs["stage"] == "workflow_initial_lock"
        key = WorkflowLockKey(
            PRICE_FORMAT_LOCK_NAMESPACE,
            int(payload["priceFormatId"]),
            "price_format",
        )
        raise PricingWorkflowConflict(
            key,
            stage=kwargs["stage"],
            waited_seconds=0.25,
            backend_pid=4321,
        )

    monkeypatch.setattr(workflow_module.PricingWorkflowLockLease, "acquire", reject_lock)
    run = workflow_module.create_workflow_run(db=db, payload=payload)

    assert run.status == "conflict"
    assert "stage=workflow_initial_lock" in run.error
    assert "waited_seconds=0.250" in run.error
    assert "backend_pid=4321" in run.error


def test_workflow_lock_timeout_configuration_is_bounded(monkeypatch):
    monkeypatch.setenv("PRICING_WORKFLOW_LOCK_TIMEOUT_SECONDS", "999")
    assert workflow_lock_retry_timeout_seconds() == 60.0
    monkeypatch.setenv("PRICING_WORKFLOW_LOCK_TIMEOUT_SECONDS", "invalid")
    assert workflow_lock_retry_timeout_seconds() == 5.0
