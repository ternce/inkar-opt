from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator


logger = logging.getLogger(__name__)


_URI_CREDENTIALS_RE = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^\s:/@]+):[^\s@/]+@", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?P<name>password|passwd|pwd|token|api[_-]?key|authorization)"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<quote>['\"]?)[^\s,;}'\"]+(?P=quote)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


def sanitize_error_message(error: object, *, limit: int = 500) -> str:
    """Keep diagnostic context while preventing common credentials from reaching metadata/logs."""
    value = str(error)
    value = _URI_CREDENTIALS_RE.sub(r"\g<scheme>\g<user>:***@", value)
    value = _BEARER_RE.sub("Bearer ***", value)
    value = _SECRET_VALUE_RE.sub(r"\g<name>\g<separator>***", value)
    return value[:limit]


@dataclass
class WorkflowTimingRecorder:
    workflow_id: int
    price_format_id: int
    selected_source_ids: list[int]
    stages: list[dict] = field(default_factory=list)

    def record(
        self,
        stage: str,
        started_at: float,
        *,
        success: bool,
        input_rows: int | None = None,
        output_rows: int | None = None,
        error: str = "",
    ) -> dict:
        item = {
            "stage": stage,
            "durationMs": round((time.perf_counter() - started_at) * 1000, 3),
            "success": bool(success),
        }
        if input_rows is not None:
            item["inputRows"] = int(input_rows)
        if output_rows is not None:
            item["outputRows"] = int(output_rows)
        if error:
            item["error"] = sanitize_error_message(error)
        self.stages.append(item)
        logger.info(
            "[PRICING_WORKFLOW_STAGE] workflow_id=%s price_format_id=%s stage=%s duration_ms=%s "
            "input_rows=%s output_rows=%s selected_source_ids=%s success=%s error=%s",
            self.workflow_id,
            self.price_format_id,
            stage,
            item["durationMs"],
            input_rows,
            output_rows,
            self.selected_source_ids,
            success,
            item.get("error", ""),
        )
        return item

    @contextmanager
    def stage(
        self,
        name: str,
        *,
        input_rows: int | None = None,
        output_rows: int | None = None,
    ) -> Iterator[None]:
        started_at = time.perf_counter()
        try:
            yield
        except BaseException as exc:
            self.record(name, started_at, success=False, input_rows=input_rows, output_rows=output_rows, error=str(exc))
            raise
        else:
            self.record(name, started_at, success=True, input_rows=input_rows, output_rows=output_rows)

    def extend(self, stages: list[dict]) -> None:
        for stage in stages:
            item = dict(stage)
            if item.get("error"):
                item["error"] = sanitize_error_message(item["error"])
            self.stages.append(item)
            logger.info(
                "[PRICING_WORKFLOW_STAGE] workflow_id=%s price_format_id=%s stage=%s duration_ms=%s "
                "input_rows=%s output_rows=%s selected_source_ids=%s success=%s error=%s",
                self.workflow_id,
                self.price_format_id,
                item.get("stage"),
                item.get("durationMs"),
                item.get("inputRows"),
                item.get("outputRows"),
                self.selected_source_ids,
                item.get("success", True),
                item.get("error", ""),
            )

    def metadata(self) -> dict:
        return {
            "clock": "perf_counter",
            "selectedSourceIds": list(self.selected_source_ids),
            "stages": list(self.stages),
        }
