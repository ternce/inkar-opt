from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


SUCCESS_NONZERO = "SUCCESS_NONZERO"
SUCCESS_ZERO = "SUCCESS_ZERO"
SKIPPED_DISABLED = "SKIPPED_DISABLED"
SKIPPED_BY_CONFIGURATION = "SKIPPED_BY_CONFIGURATION"
SKIPPED_SIZE_LIMIT = "SKIPPED_SIZE_LIMIT"
SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
SKIPPED_NO_REQUIRED_IDENTIFIER = "SKIPPED_NO_REQUIRED_IDENTIFIER"
AUTH_FAILED = "AUTH_FAILED"
METADATA_FAILED = "METADATA_FAILED"
DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
DOWNLOAD_TIMEOUT = "DOWNLOAD_TIMEOUT"
HTTP_ERROR = "HTTP_ERROR"
INVALID_RESPONSE = "INVALID_RESPONSE"
EMPTY_RESPONSE = "EMPTY_RESPONSE"
PARSE_FAILED = "PARSE_FAILED"
PARSE_TIMEOUT = "PARSE_TIMEOUT"
DB_REPLACE_FAILED = "DB_REPLACE_FAILED"
CANCELLED = "CANCELLED"
UNKNOWN_FAILED = "UNKNOWN_FAILED"

TERMINAL_OUTCOMES = {
    SUCCESS_NONZERO,
    SUCCESS_ZERO,
    SKIPPED_DISABLED,
    SKIPPED_BY_CONFIGURATION,
    SKIPPED_SIZE_LIMIT,
    SKIPPED_DUPLICATE,
    SKIPPED_NO_REQUIRED_IDENTIFIER,
    AUTH_FAILED,
    METADATA_FAILED,
    DOWNLOAD_FAILED,
    DOWNLOAD_TIMEOUT,
    HTTP_ERROR,
    INVALID_RESPONSE,
    EMPTY_RESPONSE,
    PARSE_FAILED,
    PARSE_TIMEOUT,
    DB_REPLACE_FAILED,
    CANCELLED,
    UNKNOWN_FAILED,
}

_SENSITIVE_RE = re.compile(
    r"(password|passwd|pwd|token|authorization|bearer|cookie|set-cookie|refreshToken|accessToken)",
    re.IGNORECASE,
)


def sanitize_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): ("***" if _SENSITIVE_RE.search(str(k)) else sanitize_for_log(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_for_log(v) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_log(v) for v in value)
    text = str(value)
    if _SENSITIVE_RE.search(text):
        return "***"
    return value


def classify_exception(exc: BaseException) -> tuple[str, str, int | None]:
    name = exc.__class__.__name__
    text = str(exc)
    lowered = f"{name} {text}".lower()
    http_status: int | None = None
    match = re.search(r"http\s+(\d{3})", text, re.IGNORECASE)
    if match:
        http_status = int(match.group(1))
    if "timeout" in lowered:
        return DOWNLOAD_TIMEOUT, name, http_status
    if "cancel" in lowered:
        return CANCELLED, name, http_status
    if "token/" in lowered or "auth" in lowered or "credential" in lowered or http_status in {401, 403}:
        return AUTH_FAILED, name, http_status
    if http_status is not None:
        return HTTP_ERROR, name, http_status
    if "invalid json" in lowered or "non-list json" in lowered or "invalid response" in lowered:
        return INVALID_RESPONSE, name, http_status
    if "parse" in lowered or "json" in lowered:
        return PARSE_FAILED, name, http_status
    return UNKNOWN_FAILED, name, http_status


def _seconds(started_at: float | None) -> float:
    if started_at is None:
        return 0.0
    return round(time.perf_counter() - started_at, 3)


def _safe_name(value: object) -> str:
    return str(value or "").replace("\n", " ").replace("\r", " ")[:160]


@dataclass
class ProvisorFilialAudit:
    account_id: int
    account_label: str
    filial_id: str
    filial_name: str
    filial_index: int = 0
    filials_total: int = 0
    discovered_at: float = field(default_factory=time.perf_counter)
    started_at: float | None = None
    finished_at: float | None = None
    outcome: str = ""
    reason_code: str = ""
    http_status: int | None = None
    attempts: int = 1
    response_size_mb: float = 0.0
    raw_rows: int = 0
    valid_rows: int = 0
    positive_price_rows: int = 0
    zero_price_rows: int = 0
    duplicate_rows_removed: int = 0
    inserted_rows: int = 0
    previous_rows: int = 0
    download_elapsed_sec: float = 0.0
    parse_elapsed_sec: float = 0.0
    normalization_elapsed_sec: float = 0.0
    db_elapsed_sec: float = 0.0

    @property
    def total_elapsed_sec(self) -> float:
        end = self.finished_at or time.perf_counter()
        start = self.started_at or self.discovered_at
        return round(end - start, 3)

    @property
    def slowest_stage(self) -> str:
        stages = {
            "download": self.download_elapsed_sec,
            "parse": self.parse_elapsed_sec,
            "normalization": self.normalization_elapsed_sec,
            "database": self.db_elapsed_sec,
        }
        name, value = max(stages.items(), key=lambda item: item[1])
        return name if value > 0 else "unknown"


class ProvisorRefreshAudit:
    def __init__(
        self,
        *,
        refresh_id: str,
        trigger_source: str,
        requested_account_ids: list[int],
        eligible_account_ids: list[int],
        accounts_total: int,
        heartbeat_seconds: float = 60.0,
    ) -> None:
        self.refresh_id = str(refresh_id or "")
        self.trigger_source = str(trigger_source or "")
        self.requested_account_ids = list(requested_account_ids)
        self.eligible_account_ids = list(eligible_account_ids)
        self.accounts_total = int(accounts_total)
        self.heartbeat_seconds = heartbeat_seconds
        self.started_at = time.perf_counter()
        self._last_progress_at = self.started_at
        self._accounts_started: set[int] = set()
        self._accounts_completed: set[int] = set()
        self._accounts_failed: set[int] = set()
        self._account_started_at: dict[int, float] = {}
        self._account_labels: dict[int, str] = {}
        self._filials: dict[tuple[int, str], ProvisorFilialAudit] = {}
        self._reason_counts: Counter[str] = Counter()
        self._timing_totals: Counter[str] = Counter()

    def account_start(self, *, account_id: int, account_label: str, account_index: int) -> None:
        self._accounts_started.add(int(account_id))
        self._account_started_at[int(account_id)] = time.perf_counter()
        self._account_labels[int(account_id)] = _safe_name(account_label)
        logger.info(
            "[PROVISOR_ACCOUNT_START] refresh_id=%s account_id=%s account_label=%s account_index=%s accounts_total=%s",
            self.refresh_id,
            int(account_id),
            _safe_name(account_label),
            int(account_index),
            self.accounts_total,
        )

    def discovery(self, *, account_id: int, filials_discovered: int, discovery_elapsed_sec: float) -> None:
        self._timing_totals["filial_discovery"] += float(discovery_elapsed_sec or 0)
        logger.info(
            "[PROVISOR_FILIAL_DISCOVERY] refresh_id=%s account_id=%s filials_discovered=%s discovery_elapsed_sec=%s",
            self.refresh_id,
            int(account_id),
            int(filials_discovered),
            round(float(discovery_elapsed_sec or 0), 3),
        )

    def discovered(
        self,
        *,
        account_id: int,
        account_label: str,
        filial_id: object,
        filial_name: object,
        filial_index: int,
        filials_total: int,
    ) -> None:
        key = (int(account_id), str(filial_id or ""))
        self._filials[key] = ProvisorFilialAudit(
            account_id=int(account_id),
            account_label=_safe_name(account_label),
            filial_id=key[1],
            filial_name=_safe_name(filial_name),
            filial_index=int(filial_index),
            filials_total=int(filials_total),
        )

    def filial_start(self, *, account_id: int, filial_id: object) -> None:
        row = self._filials.get((int(account_id), str(filial_id or "")))
        if row is None:
            return
        row.started_at = time.perf_counter()
        logger.info(
            "[PROVISOR_FILIAL_START] refresh_id=%s account_id=%s filial_id=%s filial_name=%s filial_index=%s filials_total=%s",
            self.refresh_id,
            row.account_id,
            row.filial_id,
            row.filial_name,
            row.filial_index,
            row.filials_total,
        )

    def result(
        self,
        *,
        account_id: int,
        filial_id: object,
        outcome: str,
        reason_code: str = "",
        http_status: int | None = None,
        attempts: int = 1,
        response_size_mb: float = 0.0,
        raw_rows: int = 0,
        valid_rows: int = 0,
        positive_price_rows: int = 0,
        zero_price_rows: int = 0,
        duplicate_rows_removed: int = 0,
        inserted_rows: int = 0,
        previous_rows: int = 0,
        download_elapsed_sec: float = 0.0,
        parse_elapsed_sec: float = 0.0,
        normalization_elapsed_sec: float = 0.0,
        db_elapsed_sec: float = 0.0,
    ) -> None:
        if outcome not in TERMINAL_OUTCOMES:
            outcome = UNKNOWN_FAILED
        key = (int(account_id), str(filial_id or ""))
        row = self._filials.get(key)
        if row is None:
            row = ProvisorFilialAudit(
                account_id=int(account_id),
                account_label=self._account_labels.get(int(account_id), ""),
                filial_id=key[1],
                filial_name=key[1],
            )
            self._filials[key] = row
        if row.outcome:
            return
        row.finished_at = time.perf_counter()
        row.outcome = outcome
        row.reason_code = str(reason_code or outcome).strip()
        row.http_status = http_status
        row.attempts = int(attempts or 1)
        row.response_size_mb = round(float(response_size_mb or 0), 3)
        row.raw_rows = int(raw_rows or 0)
        row.valid_rows = int(valid_rows or 0)
        row.positive_price_rows = int(positive_price_rows or 0)
        row.zero_price_rows = int(zero_price_rows or 0)
        row.duplicate_rows_removed = int(duplicate_rows_removed or 0)
        row.inserted_rows = int(inserted_rows or 0)
        row.previous_rows = int(previous_rows or 0)
        row.download_elapsed_sec = round(float(download_elapsed_sec or 0), 3)
        row.parse_elapsed_sec = round(float(parse_elapsed_sec or 0), 3)
        row.normalization_elapsed_sec = round(float(normalization_elapsed_sec or 0), 3)
        row.db_elapsed_sec = round(float(db_elapsed_sec or 0), 3)
        self._reason_counts[row.reason_code] += 1
        self._timing_totals["download"] += row.download_elapsed_sec
        self._timing_totals["parse"] += row.parse_elapsed_sec
        self._timing_totals["normalization"] += row.normalization_elapsed_sec
        self._timing_totals["database"] += row.db_elapsed_sec
        logger.info(
            "[PROVISOR_FILIAL_RESULT] refresh_id=%s account_id=%s filial_id=%s outcome=%s reason_code=%s http_status=%s attempts=%s response_size_mb=%s raw_rows=%s valid_rows=%s positive_price_rows=%s zero_price_rows=%s duplicate_rows_removed=%s inserted_rows=%s previous_rows=%s download_elapsed_sec=%s parse_elapsed_sec=%s db_elapsed_sec=%s filial_total_elapsed_sec=%s",
            self.refresh_id,
            row.account_id,
            row.filial_id,
            row.outcome,
            row.reason_code,
            row.http_status or "",
            row.attempts,
            row.response_size_mb,
            row.raw_rows,
            row.valid_rows,
            row.positive_price_rows,
            row.zero_price_rows,
            row.duplicate_rows_removed,
            row.inserted_rows,
            row.previous_rows,
            row.download_elapsed_sec,
            row.parse_elapsed_sec,
            row.db_elapsed_sec,
            row.total_elapsed_sec,
        )

    def account_summary(self, *, account_id: int, failed: bool = False) -> None:
        account_id = int(account_id)
        if failed:
            self._accounts_failed.add(account_id)
        self._accounts_completed.add(account_id)
        rows = [row for row in self._filials.values() if row.account_id == account_id]
        counts = self._counts(rows)
        logger.info(
            "[PROVISOR_ACCOUNT_SUMMARY] refresh_id=%s account_id=%s filials_discovered=%s filials_attempted=%s success_nonzero=%s success_zero=%s skipped=%s failed=%s timed_out=%s inserted_rows_total=%s account_total_elapsed_sec=%s",
            self.refresh_id,
            account_id,
            len(rows),
            sum(1 for row in rows if row.started_at is not None),
            counts["success_nonzero"],
            counts["success_zero"],
            counts["skipped"],
            counts["failed"],
            counts["timed_out"],
            sum(row.inserted_rows for row in rows),
            _seconds(self._account_started_at.get(account_id)),
        )

    def maybe_progress(self, *, current_account: object = "", current_filial: object = "") -> None:
        now = time.perf_counter()
        if now - self._last_progress_at < self.heartbeat_seconds:
            return
        self._last_progress_at = now
        summary = self.summary(log_invariant=False)
        logger.info(
            "[PROVISOR_REFRESH_PROGRESS] refresh_id=%s current_account=%s accounts_completed=%s accounts_total=%s current_filial=%s filials_completed=%s filials_total=%s elapsed_sec=%s success_nonzero=%s success_zero=%s skipped=%s failed=%s timed_out=%s",
            self.refresh_id,
            current_account,
            summary["accounts_completed"],
            self.accounts_total,
            current_filial,
            summary["filials_with_terminal_outcome"],
            summary["filials_discovered"],
            summary["refresh_total_elapsed_sec"],
            summary["success_nonzero"],
            summary["success_zero"],
            summary["skipped"],
            summary["failed"],
            summary["timed_out"],
        )

    def finalize_missing(self, *, reason_code: str = "account_aborted_before_filial_attempt") -> None:
        for row in list(self._filials.values()):
            if not row.outcome:
                self.result(
                    account_id=row.account_id,
                    filial_id=row.filial_id,
                    outcome=UNKNOWN_FAILED,
                    reason_code=reason_code,
                )

    def summary(self, *, log_invariant: bool = True) -> dict[str, Any]:
        rows = list(self._filials.values())
        counts = self._counts(rows)
        filials_attempted = sum(1 for row in rows if row.started_at is not None)
        filials_with_terminal_outcome = sum(1 for row in rows if row.outcome)
        filials_not_attempted = max(0, len(rows) - filials_attempted)
        unknown = sum(1 for row in rows if row.outcome == UNKNOWN_FAILED or not row.outcome)
        data = {
            "refresh_id": self.refresh_id,
            "trigger_source": self.trigger_source,
            "requested_account_ids": self.requested_account_ids,
            "eligible_accounts": len(self.eligible_account_ids),
            "accounts_attempted": len(self._accounts_started),
            "accounts_completed": len(self._accounts_completed),
            "accounts_failed": len(self._accounts_failed),
            "filials_discovered": len(rows),
            "filials_attempted": filials_attempted,
            "filials_not_attempted": filials_not_attempted,
            "filials_with_terminal_outcome": filials_with_terminal_outcome,
            "success_nonzero": counts["success_nonzero"],
            "success_zero": counts["success_zero"],
            "skipped": counts["skipped"],
            "failed": counts["failed"],
            "timed_out": counts["timed_out"],
            "unknown_outcome": unknown,
            "raw_rows_total": sum(row.raw_rows for row in rows),
            "valid_rows_total": sum(row.valid_rows for row in rows),
            "inserted_rows_total": sum(row.inserted_rows for row in rows),
            "authentication_total_sec": round(self._timing_totals["authentication"], 3),
            "filial_discovery_total_sec": round(self._timing_totals["filial_discovery"], 3),
            "download_elapsed_sec_total": round(self._timing_totals["download"], 3),
            "parse_elapsed_sec_total": round(self._timing_totals["parse"], 3),
            "normalization_elapsed_sec_total": round(self._timing_totals["normalization"], 3),
            "db_elapsed_sec_total": round(self._timing_totals["database"], 3),
            "refresh_total_elapsed_sec": _seconds(self.started_at),
            "failure_skip_reasons": dict(sorted(self._reason_counts.items())),
            "slowest_filials": [
                {
                    "account_id": row.account_id,
                    "filial_id": row.filial_id,
                    "filial_name": row.filial_name,
                    "outcome": row.outcome or UNKNOWN_FAILED,
                    "response_size_mb": row.response_size_mb,
                    "raw_rows": row.raw_rows,
                    "inserted_rows": row.inserted_rows,
                    "total_elapsed_sec": row.total_elapsed_sec,
                    "slowest_stage": row.slowest_stage,
                }
                for row in sorted(rows, key=lambda item: item.total_elapsed_sec, reverse=True)[:10]
            ],
        }
        invariant_ok = (
            data["filials_discovered"] == data["filials_attempted"] + data["filials_not_attempted"]
            and data["filials_with_terminal_outcome"] == data["filials_discovered"]
        )
        data["invariant_ok"] = invariant_ok
        if log_invariant and not invariant_ok:
            logger.error(
                "[PROVISOR_AUDIT_INVARIANT_FAILED] refresh_id=%s filials_discovered=%s filials_attempted=%s filials_not_attempted=%s filials_with_terminal_outcome=%s",
                self.refresh_id,
                data["filials_discovered"],
                data["filials_attempted"],
                data["filials_not_attempted"],
                data["filials_with_terminal_outcome"],
            )
        return data

    def log_summary(self) -> dict[str, Any]:
        self.finalize_missing()
        data = self.summary()
        logger.info(
            "[PROVISOR_REFRESH_SUMMARY] refresh_id=%s trigger_source=%s requested_account_ids=%s eligible_accounts=%s accounts_attempted=%s accounts_completed=%s accounts_failed=%s filials_discovered=%s filials_attempted=%s filials_not_attempted=%s success_nonzero=%s success_zero=%s skipped=%s failed=%s timed_out=%s unknown_outcome=%s raw_rows_total=%s valid_rows_total=%s inserted_rows_total=%s download_elapsed_sec_total=%s parse_elapsed_sec_total=%s normalization_elapsed_sec_total=%s db_elapsed_sec_total=%s refresh_total_elapsed_sec=%s",
            data["refresh_id"],
            data["trigger_source"],
            data["requested_account_ids"],
            data["eligible_accounts"],
            data["accounts_attempted"],
            data["accounts_completed"],
            data["accounts_failed"],
            data["filials_discovered"],
            data["filials_attempted"],
            data["filials_not_attempted"],
            data["success_nonzero"],
            data["success_zero"],
            data["skipped"],
            data["failed"],
            data["timed_out"],
            data["unknown_outcome"],
            data["raw_rows_total"],
            data["valid_rows_total"],
            data["inserted_rows_total"],
            data["download_elapsed_sec_total"],
            data["parse_elapsed_sec_total"],
            data["normalization_elapsed_sec_total"],
            data["db_elapsed_sec_total"],
            data["refresh_total_elapsed_sec"],
        )
        logger.info(
            "\n======================================================\n"
            "Provisor Refresh Performance Summary\n"
            "======================================================\n\n"
            "Accounts\n--------\n"
            "eligible: %s\nattempted: %s\ncompleted: %s\nfailed: %s\n\n"
            "Filials\n-------\n"
            "discovered: %s\nattempted: %s\nnot_attempted: %s\nsuccess_nonzero: %s\nsuccess_zero: %s\nskipped: %s\nfailed: %s\ntimed_out: %s\nunknown_outcome: %s\n\n"
            "Rows\n----\nraw_rows: %s\nvalid_rows: %s\ninserted_rows: %s\n\n"
            "Timing\n------\n"
            "authentication_total_sec: %s\nfilial_discovery_total_sec: %s\ndownload_total_sec: %s\nparse_total_sec: %s\nnormalization_total_sec: %s\ndatabase_total_sec: %s\nrefresh_total_sec: %s\n\n"
            "Slowest filials\n---------------\n%s\n\n"
            "Failure/skip reasons\n--------------------\n%s\n"
            "======================================================",
            data["eligible_accounts"],
            data["accounts_attempted"],
            data["accounts_completed"],
            data["accounts_failed"],
            data["filials_discovered"],
            data["filials_attempted"],
            data["filials_not_attempted"],
            data["success_nonzero"],
            data["success_zero"],
            data["skipped"],
            data["failed"],
            data["timed_out"],
            data["unknown_outcome"],
            data["raw_rows_total"],
            data["valid_rows_total"],
            data["inserted_rows_total"],
            data["authentication_total_sec"],
            data["filial_discovery_total_sec"],
            data["download_elapsed_sec_total"],
            data["parse_elapsed_sec_total"],
            data["normalization_elapsed_sec_total"],
            data["db_elapsed_sec_total"],
            data["refresh_total_elapsed_sec"],
            data["slowest_filials"],
            data["failure_skip_reasons"],
        )
        return data

    @staticmethod
    def _counts(rows: list[ProvisorFilialAudit]) -> dict[str, int]:
        skipped_outcomes = {
            SKIPPED_DISABLED,
            SKIPPED_BY_CONFIGURATION,
            SKIPPED_SIZE_LIMIT,
            SKIPPED_DUPLICATE,
            SKIPPED_NO_REQUIRED_IDENTIFIER,
        }
        failed_outcomes = {
            AUTH_FAILED,
            METADATA_FAILED,
            DOWNLOAD_FAILED,
            HTTP_ERROR,
            INVALID_RESPONSE,
            EMPTY_RESPONSE,
            PARSE_FAILED,
            DB_REPLACE_FAILED,
            CANCELLED,
            UNKNOWN_FAILED,
        }
        return {
            "success_nonzero": sum(1 for row in rows if row.outcome == SUCCESS_NONZERO),
            "success_zero": sum(1 for row in rows if row.outcome == SUCCESS_ZERO),
            "skipped": sum(1 for row in rows if row.outcome in skipped_outcomes),
            "failed": sum(1 for row in rows if row.outcome in failed_outcomes),
            "timed_out": sum(1 for row in rows if row.outcome in {DOWNLOAD_TIMEOUT, PARSE_TIMEOUT}),
        }
