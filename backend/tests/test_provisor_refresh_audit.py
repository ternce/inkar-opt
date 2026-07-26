from __future__ import annotations

import logging

from backend.app.services.provisor_refresh_audit import (
    DOWNLOAD_TIMEOUT,
    SKIPPED_SIZE_LIMIT,
    SUCCESS_NONZERO,
    SUCCESS_ZERO,
    ProvisorRefreshAudit,
    sanitize_for_log,
)


def test_audit_requires_terminal_outcome_for_every_discovered_filial(caplog):
    audit = ProvisorRefreshAudit(
        refresh_id="test",
        trigger_source="provisor",
        requested_account_ids=[3],
        eligible_account_ids=[3],
        accounts_total=1,
    )
    audit.account_start(account_id=3, account_label="login", account_index=1)
    audit.discovered(account_id=3, account_label="login", filial_id=128, filial_name="A", filial_index=1, filials_total=3)
    audit.discovered(account_id=3, account_label="login", filial_id=129, filial_name="B", filial_index=2, filials_total=3)
    audit.discovered(account_id=3, account_label="login", filial_id=130, filial_name="C", filial_index=3, filials_total=3)
    audit.filial_start(account_id=3, filial_id=128)
    audit.result(account_id=3, filial_id=128, outcome=SUCCESS_NONZERO, reason_code="saved", raw_rows=2, valid_rows=2, inserted_rows=2)
    audit.result(account_id=3, filial_id=129, outcome=SKIPPED_SIZE_LIMIT, reason_code="excluded_emit_or_heavy_filial")

    with caplog.at_level(logging.ERROR):
        summary = audit.log_summary()

    assert summary["filials_discovered"] == 3
    assert summary["filials_with_terminal_outcome"] == 3
    assert summary["unknown_outcome"] == 1
    assert summary["invariant_ok"] is True
    assert "[PROVISOR_AUDIT_INVARIANT_FAILED]" not in caplog.text


def test_audit_classifies_zero_size_and_timeout_separately():
    audit = ProvisorRefreshAudit(
        refresh_id="test",
        trigger_source="provisor",
        requested_account_ids=[],
        eligible_account_ids=[3],
        accounts_total=1,
    )
    for index, filial_id in enumerate((128, 129, 130), start=1):
        audit.discovered(account_id=3, account_label="login", filial_id=filial_id, filial_name=str(filial_id), filial_index=index, filials_total=3)
    audit.result(account_id=3, filial_id=128, outcome=SUCCESS_ZERO, reason_code="saved_zero_rows")
    audit.result(account_id=3, filial_id=129, outcome=SKIPPED_SIZE_LIMIT, reason_code="excluded_emit_or_heavy_filial")
    audit.result(account_id=3, filial_id=130, outcome=DOWNLOAD_TIMEOUT, reason_code="timeout_120s")

    summary = audit.summary()

    assert summary["success_zero"] == 1
    assert summary["skipped"] == 1
    assert summary["timed_out"] == 1
    assert summary["failed"] == 0
    assert summary["failure_skip_reasons"]["saved_zero_rows"] == 1
    assert summary["failure_skip_reasons"]["excluded_emit_or_heavy_filial"] == 1
    assert summary["failure_skip_reasons"]["timeout_120s"] == 1


def test_audit_secret_sanitizer_masks_credentials_and_tokens():
    payload = {
        "login": "safe-login",
        "password": "secret",
        "Authorization": "Bearer abc",
        "nested": {"accessToken": "abc", "value": "ok"},
    }

    sanitized = sanitize_for_log(payload)

    assert sanitized["login"] == "safe-login"
    assert sanitized["password"] == "***"
    assert sanitized["Authorization"] == "***"
    assert sanitized["nested"]["accessToken"] == "***"
    assert sanitized["nested"]["value"] == "ok"
