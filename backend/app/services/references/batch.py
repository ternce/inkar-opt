from __future__ import annotations

from sqlalchemy.orm import Session

from .imports import import_reference_excel
from .ratings import RATING_DATA_TYPES, import_top_rating_excel
from .sources import ReferenceImportSource
from .statuses import import_job_to_dict
from .types import BRANCHES


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

    jobs = []
    for payload in source.get_payloads():
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
        )
        jobs.append(import_job_to_dict(row))

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
