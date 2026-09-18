from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from .imports import ManufacturerBatchState, _apply_manufacturer_fallback, import_reference_excel
from .ratings import RATING_DATA_TYPES, import_top_rating_excel
from .sources import ReferenceImportSource
from .statuses import import_job_to_dict
from .types import BRANCHES

logger = logging.getLogger(__name__)


def branch_ids_for_import(data_type: str, selected_branch_ids: list[str]) -> list[str]:
    if data_type == "rating_global":
        return [branch["id"] for branch in BRANCHES]
    return selected_branch_ids


def import_reference_batch(
    *,
    db: Session,
    source: ReferenceImportSource,
    selected_branch_ids: list[str],
    user_name: str = "",
) -> dict:
    if not selected_branch_ids:
        raise ValueError("branch_ids is required")

    payloads = source.get_payloads()
    manufacturer_batch_state = ManufacturerBatchState()
    jobs = []
    for payload in payloads:
        branch_ids = branch_ids_for_import(payload.data_type, selected_branch_ids)
        if payload.data_type in RATING_DATA_TYPES:
            jobs.append(
                import_top_rating_excel(
                    db=db,
                    data_type=payload.data_type,
                    branch_ids=branch_ids,
                    content=payload.content,
                    filename=payload.filename,
                    user_name=user_name,
                )
            )
            continue
        row = import_reference_excel(
            db=db,
            data_type=payload.data_type,
            branch_ids=branch_ids,
            content=payload.content,
            filename=payload.filename,
            user_name=user_name,
            manufacturer_batch_state=manufacturer_batch_state,
        )
        jobs.append(import_job_to_dict(row))

    if manufacturer_batch_state.products:
        candidates = {
            product_id: manufacturer_batch_state.candidates_by_sku[code]
            for product_id, code in manufacturer_batch_state.products.items()
        }
        _apply_manufacturer_fallback(
            db, manufacturer_batch_state.products, candidates, manufacturer_batch_state.summary,
        )
        db.commit()
    logger.info(
        "[REFERENCE_MANUFACTURER_BATCH_SUMMARY] source_type=%s %s",
        source.source_type, json.dumps(manufacturer_batch_state.summary, sort_keys=True),
    )

    has_error = any(job["status"] == "error" for job in jobs)
    has_partial = any(job["status"] == "partial" for job in jobs)
    status = "error" if has_error else "partial" if has_partial else "success"
    return {
        "status": status,
        "sourceType": source.source_type,
        "jobs": jobs,
        "jobsTotal": len(jobs),
        "jobsSuccess": sum(1 for job in jobs if job["status"] == "success"),
        "jobsPartial": sum(1 for job in jobs if job["status"] == "partial"),
        "jobsError": sum(1 for job in jobs if job["status"] == "error"),
    }
