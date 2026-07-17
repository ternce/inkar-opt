from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import ReferenceImportJob, ReferenceUpdateStatus
from ...timezone import local_iso
from .types import BRANCHES, REFERENCE_TYPES


READINESS_COLUMNS = ["stock", "cost", "rating_global", "rating_local", "products", "holdings", "counterparties", "delivery_points"]


def status_to_dict(row: ReferenceUpdateStatus) -> dict:
    today = date.today()
    freshness = "missing"
    if row.status == "running":
        freshness = "running"
    elif row.status == "error":
        freshness = "error"
    elif row.last_updated_at and row.last_updated_at.date() == today:
        freshness = "fresh"
    elif row.last_updated_at:
        freshness = "stale"
    return {
        "id": row.id,
        "branchId": row.branch_id,
        "branchName": row.branch_name,
        "dataType": row.data_type,
        "lastUpdatedAt": local_iso(row.last_updated_at) if row.last_updated_at else "",
        "rowsCount": row.rows_count,
        "status": row.status,
        "freshness": freshness,
        "error": row.error,
        "currentImportStatus": row.current_import_status or "",
        "currentImportStartedAt": local_iso(row.current_import_started_at) if row.current_import_started_at else "",
        "currentImportFinishedAt": local_iso(row.current_import_finished_at) if row.current_import_finished_at else "",
        "lastSuccessfulSnapshotId": row.last_successful_import_job_id,
        "lastSuccessfulImportAt": local_iso(row.last_updated_at) if row.last_updated_at else "",
        "activeSnapshotProductCount": row.active_snapshot_product_count or row.rows_count,
    }


def list_reference_statuses(*, db: Session) -> list[dict]:
    rows = db.execute(select(ReferenceUpdateStatus)).scalars().all()
    by_key = {(row.branch_id, row.data_type): status_to_dict(row) for row in rows}
    out: list[dict] = []
    for branch in BRANCHES:
        for data_type in REFERENCE_TYPES:
            key = (branch["id"], data_type["code"])
            out.append(
                by_key.get(
                    key,
                    {
                        "id": None,
                        "branchId": branch["id"],
                        "branchName": branch["name"],
                        "dataType": data_type["code"],
                        "lastUpdatedAt": "",
                        "rowsCount": 0,
                        "status": "missing",
                        "freshness": "missing",
                        "error": "",
                        "currentImportStatus": "",
                        "currentImportStartedAt": "",
                        "currentImportFinishedAt": "",
                        "lastSuccessfulSnapshotId": None,
                        "lastSuccessfulImportAt": "",
                        "activeSnapshotProductCount": 0,
                    },
                )
            )
    return out


def reference_readiness_matrix(*, db: Session) -> dict:
    statuses = list_reference_statuses(db=db)
    by_key = {(row["branchId"], row["dataType"]): row for row in statuses}
    type_by_code = {row["code"]: row for row in REFERENCE_TYPES}
    return {
        "columns": [
            {"code": code, "name": type_by_code.get(code, {"name": code})["name"]}
            for code in READINESS_COLUMNS
        ],
        "rows": [
            {
                "branchId": branch["id"],
                "branchName": branch["name"],
                "cells": {
                    code: by_key.get(
                        (branch["id"], code),
                        {
                            "id": None,
                            "branchId": branch["id"],
                            "branchName": branch["name"],
                            "dataType": code,
                            "lastUpdatedAt": "",
                            "rowsCount": 0,
                            "status": "missing",
                            "freshness": "missing",
                            "error": "",
                            "currentImportStatus": "",
                            "currentImportStartedAt": "",
                            "currentImportFinishedAt": "",
                            "lastSuccessfulSnapshotId": None,
                            "lastSuccessfulImportAt": "",
                            "activeSnapshotProductCount": 0,
                        },
                    )
                    for code in READINESS_COLUMNS
                },
            }
            for branch in BRANCHES
        ],
    }


def import_job_to_dict(row: ReferenceImportJob) -> dict:
    return {
        "id": row.id,
        "dataType": row.data_type,
        "branchIds": row.branch_ids_json,
        "filename": row.filename,
        "sourceType": row.source_type,
        "status": row.status,
        "rowsTotal": row.rows_total,
        "rowsSuccess": row.rows_success,
        "rowsFailed": row.rows_failed,
        "error": row.error,
        "log": row.log_json,
        "createdAt": local_iso(row.created_at) if row.created_at else "",
        "startedAt": local_iso(row.started_at) if row.started_at else "",
        "finishedAt": local_iso(row.finished_at) if row.finished_at else "",
        "userName": row.user_name,
    }


def list_reference_imports(*, db: Session, limit: int = 100) -> list[dict]:
    rows = (
        db.execute(select(ReferenceImportJob).order_by(ReferenceImportJob.created_at.desc(), ReferenceImportJob.id.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [import_job_to_dict(row) for row in rows]
