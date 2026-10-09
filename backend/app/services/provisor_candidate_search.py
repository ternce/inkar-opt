from __future__ import annotations

import asyncio

from sqlalchemy.orm import Session

from .competitors.code_mappings import (
    product_catalog_product_for_candidates,
    provisor_goods_search_payloads,
    provisor_goods_search_seeds,
    rank_provisor_goods_candidates,
)
from .provisor_goods import ProvisorGoodsClient, ProvisorGoodsError, configured_provisor_goods_client


async def official_product_candidates(
    *,
    db: Session,
    product_id: int,
    format_code: str = "",
    limit: int = 5,
    client: ProvisorGoodsClient | None = None,
) -> list[dict]:
    row = product_catalog_product_for_candidates(db=db, product_id=product_id, format_code=format_code)
    if row is None:
        return []
    product, extra = row
    db.expunge(product)
    if extra is not None:
        db.expunge(extra)
    # Do not retain a read transaction while waiting on the external API.
    db.rollback()
    api = client or configured_provisor_goods_client()
    async def search():
        goods = []
        for seed in provisor_goods_search_seeds(product.name):
            goods = await api.search_goods(full_name=seed)
            if goods:
                break
        return goods

    try:
        goods = await asyncio.wait_for(search(), timeout=float(getattr(api, "total_timeout_seconds", 15.0)))
    except asyncio.TimeoutError as exc:
        raise ProvisorGoodsError("Provisor goods candidate lookup timed out") from exc
    return rank_provisor_goods_candidates(db=db, product=product, extra=extra, goods=goods, limit=limit)


async def official_external_search(
    *,
    db: Session,
    query: str,
    limit: int,
    client: ProvisorGoodsClient | None = None,
) -> list[dict]:
    api = client or configured_provisor_goods_client()
    normalized = str(query or "").strip()
    if not normalized:
        return []
    try:
        goods = await asyncio.wait_for(
            api.search_goods(goods_id=int(normalized), limit=limit)
            if normalized.isdigit()
            else api.search_goods(full_name=normalized, limit=limit),
            timeout=float(getattr(api, "total_timeout_seconds", 15.0)),
        )
    except asyncio.TimeoutError as exc:
        raise ProvisorGoodsError("Provisor goods manual search timed out") from exc
    return provisor_goods_search_payloads(db=db, goods=goods, limit=limit)
