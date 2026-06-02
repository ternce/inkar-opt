from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CalculatedPrice, CompetitorPrice, CompetitorPriceList, PriceFormat, PriceList, Product, ProductExtra
from .competitor_assignments import get_assigned_competitor_price_lists
from .regions import allowed_provisor_source_names_for_city_id, city_id_from_branch


def _as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None

def get_products_with_competitor_top5(
    *,
    db: Session,
    price_format_code: str | None,
    price_list_number: str | None = None,
    region_id: int | None = None,
    product_id: int | None = None,
) -> list[dict]:
    products_stmt = (
        select(Product, ProductExtra)
        .join(ProductExtra, ProductExtra.product_id == Product.id, isouter=True)
        .order_by(Product.code.asc())
    )
    if product_id is not None:
        products_stmt = products_stmt.where(Product.id == product_id)
    products = db.execute(products_stmt).all()

    pf: PriceFormat | None = None
    if price_format_code:
        pf = db.execute(select(PriceFormat).where(PriceFormat.code == price_format_code)).scalars().first()

    coeff_by_source: dict[str, Decimal] = {}
    label_by_source: dict[str, str] = {}
    selected_sources: list[str] = []
    # Keep the best (lowest) computed price per source.
    prices_by_product_id: dict[int, dict[str, Decimal]] = defaultdict(dict)
    price_meta_by_product_id: dict[int, dict[str, dict]] = defaultdict(dict)
    model_price_by_product_id: dict[int, float] = {}
    model_log_by_product_id: dict[int, str] = {}

    pl_id: int | None = None

    if pf is not None:
        effective_city_id = region_id if region_id is not None else city_id_from_branch(pf.branch)
        allowed_provisor_sources = allowed_provisor_source_names_for_city_id(effective_city_id)

        # Resolve price list for model prices. If none is passed, use the latest
        # generated list for this price format so the table survives page refresh.
        if isinstance(price_list_number, str) and price_list_number.strip():
            pl = (
                db.execute(select(PriceList).where(PriceList.number == price_list_number.strip()))
                .scalars()
                .first()
            )
            if pl is not None and int(pl.price_format_id) == int(pf.id):
                pl_id = int(pl.id)
        else:
            pl = (
                db.execute(
                    select(PriceList)
                    .where(PriceList.price_format_id == pf.id)
                    .order_by(PriceList.created_at.desc(), PriceList.id.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if pl is not None:
                pl_id = int(pl.id)
                price_list_number = pl.number

        if pl_id is not None:
            rows = (
                db.execute(select(CalculatedPrice).where(CalculatedPrice.price_list_id == pl_id))
                .scalars()
                .all()
            )
            for r in rows:
                try:
                    model_price_by_product_id[int(r.product_id)] = float(r.final_price)
                    model_log_by_product_id[int(r.product_id)] = r.applied_reason or ""
                except Exception:
                    continue

        cfg_rows = (
            db.execute(
                select(CompetitorPrice)
                .where(CompetitorPrice.price_format_id == pf.id)
                .where(CompetitorPrice.product_id.is_(None))
            )
            .scalars()
            .all()
        )
        selected_list_rows = [item.price_list for item in get_assigned_competitor_price_lists(db=db, price_format_id=int(pf.id))]
        explicitly_selected_sources = {f"{row.source_type}:{row.source_key}" for row in selected_list_rows}
        for cfg in cfg_rows:
            src = (cfg.source_name or "").strip()
            if not src:
                continue
            if (
                allowed_provisor_sources is not None
                and src.startswith("provisor:")
                and src not in explicitly_selected_sources
                and src not in allowed_provisor_sources
            ):
                continue
            coeff_by_source[src] = _as_decimal(cfg.coefficient) or Decimal("1")
            selected_sources.append(src)

        if selected_sources:
            for pl_row in selected_list_rows:
                src = f"{pl_row.source_type}:{pl_row.source_key}"
                label_by_source[src] = pl_row.display_name or pl_row.supplier or src

        price_rows_stmt = (
            select(CompetitorPrice)
            .where(CompetitorPrice.price_format_id == pf.id)
            .where(CompetitorPrice.product_id.is_not(None))
        )
        if product_id is not None:
            price_rows_stmt = price_rows_stmt.where(CompetitorPrice.product_id == product_id)
        price_rows = db.execute(price_rows_stmt).scalars().all()
        for row in price_rows:
            if row.product_id is None:
                continue
            src = (row.source_name or "").strip()
            if (
                allowed_provisor_sources is not None
                and src.startswith("provisor:")
                and src not in explicitly_selected_sources
                and src not in allowed_provisor_sources
            ):
                continue
            sp = _as_decimal(row.source_price)
            if sp is None or sp <= 0:
                continue
            coeff = coeff_by_source.get(src, Decimal("1"))
            computed = sp * coeff
            pid = int(row.product_id)
            prev = prices_by_product_id[pid].get(src)
            if prev is None or computed < prev:
                prices_by_product_id[pid][src] = computed
                price_meta_by_product_id[pid][src] = {
                    "sourceType": src.split(":", 1)[0],
                    "matchType": row.match_type or "",
                    "matchKind": "substitute" if row.match_type == "provisor_manual_substitute" else "primary",
                    "sourceGoodsId": int(row.source_goods_id) if row.source_goods_id is not None else None,
                    "sourceDistributorGoodsId": row.source_distributor_goods_id or "",
                    "sourceManufacturer": row.source_manufacturer or "",
                    "sourceItemId": int(row.source_item_id) if row.source_item_id is not None else None,
                }

    out: list[dict] = []
    for p, extra in products:
        by_source = prices_by_product_id.get(p.id, {})
        meta_by_source = price_meta_by_product_id.get(p.id, {})
        prices_sorted = sorted(by_source.values())
        top5 = [float(x) for x in prices_sorted[:5]]
        prices_by_source_out = {
            src: float(by_source[src])
            for src in selected_sources
            if src in by_source
        }
        sorted_competitor_prices = [
            {
                "source": src,
                "label": label_by_source.get(src, src),
                "price": float(price),
                **meta_by_source.get(src, {}),
            }
            for src, price in sorted(by_source.items(), key=lambda item: (item[1], label_by_source.get(item[0], item[0])))
        ]
        competitors_out = {
            label_by_source.get(src, src): float(by_source[src])
            for src in selected_sources
            if src in by_source
        }

        model_price = model_price_by_product_id.get(int(p.id))

        out.append(
            {
                "sku": p.code,
                "productId": int(p.id),
                "name": p.name,
                "topRank": int(p.top_rank) if p.top_rank is not None else None,
                "stock": float(extra.stock) if extra is not None and extra.stock is not None else None,
                "manufacturer": (extra.manufacturer if extra is not None else "") or "",
                "provisorGoodsId": int(p.provisor_goods_id) if p.provisor_goods_id is not None else None,
                "costPrice": float(p.cost),
                "competitorPrices": top5,
                "sortedCompetitorPrices": sorted_competitor_prices,
                "competitorColumns": [
                    {"source": src, "label": label_by_source.get(src, src)}
                    for src in selected_sources
                ],
                "competitorPricesBySource": prices_by_source_out,
                "competitorPriceMetaBySource": {
                    src: meta_by_source[src]
                    for src in selected_sources
                    if src in meta_by_source
                },
                "competitors": competitors_out,
                "modelPrice": model_price,
                "modelLog": model_log_by_product_id.get(int(p.id), ""),
                "modelPriceListNumber": price_list_number or "",
            }
        )

    return out
