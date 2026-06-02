from __future__ import annotations

import os

from sqlalchemy import func, select

from app.db import init_db, SessionLocal
from app.models import CompetitorPrice, MarkupRange, PriceFormat, Product


def main() -> None:
    init_db()

    with SessionLocal() as db:
        counts = {
            "products": db.scalar(select(func.count()).select_from(Product)) or 0,
            "price_formats": db.scalar(select(func.count()).select_from(PriceFormat)) or 0,
            "markup_ranges": db.scalar(select(func.count()).select_from(MarkupRange)) or 0,
            "competitor_configs": db.scalar(
                select(func.count())
                .select_from(CompetitorPrice)
                .where(CompetitorPrice.product_id.is_(None))
            )
            or 0,
            "competitor_prices": db.scalar(
                select(func.count())
                .select_from(CompetitorPrice)
                .where(CompetitorPrice.product_id.is_not(None))
            )
            or 0,
        }

    print("DATABASE_URL=", os.getenv("DATABASE_URL"))
    print(counts)


if __name__ == "__main__":
    main()
