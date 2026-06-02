from __future__ import annotations

from sqlalchemy.orm import Session

from ..competitor_price_lists import list_competitor_price_lists


def list_competitor_sources(
    *,
    db: Session,
    price_format_code: str,
    account_id: str | None = None,
    region: str | None = None,
) -> list[dict]:
    """Return competitor price lists as source-management rows.

    Reuses the existing list service so selection semantics and visibility
    filtering stay exactly as they are today.
    """

    rows = list_competitor_price_lists(
        db=db,
        price_format_code=price_format_code,
        account_id=account_id,
        region=region,
    )
    out: list[dict] = []
    for row in rows:
        branch = str(row.get("branchName") or row.get("region") or "Без филиала").strip() or "Без филиала"
        competitor = str(row.get("competitorName") or row.get("supplier") or row.get("name") or "").strip()
        login = str(row.get("accountLogin") or row.get("accountId") or "").strip()
        source_name = " — ".join(x for x in [branch, competitor, login] if x)
        status = str(row.get("status") or "ok")
        error_summary = status if status in {"timeout", "auth_error", "error", "stale"} else ""
        out.append(
            {
                **row,
                "sourceName": source_name or str(row.get("name") or row.get("sourceKey") or row.get("id")),
                "sourceKind": row.get("sourceType") or "",
                "lastUpdatedAt": row.get("lastCheckedAt") or row.get("updatedAt") or row.get("sourceUpdatedAt") or "",
                "errorSummary": error_summary,
            }
        )
    return out
