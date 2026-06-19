from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import PricingContext
from ...timezone import now_kz_naive
from ..references.types import BRANCHES


DEFAULT_CHANNELS = ("Розница", "Опт", "Аптека")


def context_to_dict(row: PricingContext) -> dict:
    return {
        "id": row.id,
        "branchId": row.branch_id,
        "region": row.region,
        "salesChannel": row.sales_channel,
        "name": row.name,
        "isActive": bool(row.is_active),
    }


def ensure_default_contexts(*, db: Session) -> None:
    existing = int(db.execute(select(PricingContext.id).limit(1)).scalar() or 0)
    if existing:
        return
    now = now_kz_naive()
    for branch in BRANCHES:
        for channel in DEFAULT_CHANNELS:
            db.add(
                PricingContext(
                    branch_id=str(branch["id"]),
                    region=str(branch["name"]),
                    sales_channel=channel,
                    name=f'{branch["name"]} / {channel}',
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
            )
    db.commit()


def list_contexts(*, db: Session) -> list[dict]:
    ensure_default_contexts(db=db)
    rows = (
        db.execute(
            select(PricingContext)
            .where(PricingContext.is_active.is_(True))
            .order_by(PricingContext.region.asc(), PricingContext.sales_channel.asc(), PricingContext.id.asc())
        )
        .scalars()
        .all()
    )
    return [context_to_dict(row) for row in rows]
