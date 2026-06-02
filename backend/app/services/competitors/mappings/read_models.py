from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....models import Product, ProductExtra, ProductSubstituteMatch


def _like(value: str) -> str:
    return f"%{value.strip()}%"


def list_competitor_mappings(*, db: Session, search: str | None = None, limit: int = 200) -> list[dict]:
    """Return current primary goodsId and manual substitute mappings.

    Does not introduce new persistence. Primary Provisor goodsId stays on
    products.provisor_goods_id; manual mappings stay in product_substitute_matches.
    """

    limit = max(1, min(int(limit or 200), 1000))
    search_text = (search or "").strip()
    out: list[dict] = []

    product_stmt = select(Product, ProductExtra).join(ProductExtra, ProductExtra.product_id == Product.id, isouter=True)
    if search_text:
        like = _like(search_text)
        product_stmt = product_stmt.where((Product.code.ilike(like)) | (Product.name.ilike(like)) | (ProductExtra.manufacturer.ilike(like)))
    product_stmt = product_stmt.where(Product.provisor_goods_id.is_not(None)).order_by(Product.code.asc()).limit(limit)
    for product, extra in db.execute(product_stmt).all():
        out.append(
            {
                "id": f"primary:{product.id}",
                "kind": "primary",
                "productId": int(product.id),
                "sku": product.code,
                "productName": product.name,
                "productManufacturer": (extra.manufacturer if extra else "") or "",
                "competitorGoodsId": int(product.provisor_goods_id) if product.provisor_goods_id is not None else None,
                "distributorGoodsId": product.code,
                "competitorName": product.name,
                "competitorProducer": (extra.manufacturer if extra else "") or "",
                "matchType": "provisor_goods_id",
                "source": "provisor",
                "createdAt": product.created_at.isoformat() if product.created_at else "",
                "canDelete": False,
            }
        )

    substitute_stmt = (
        select(ProductSubstituteMatch, Product, ProductExtra)
        .join(Product, Product.id == ProductSubstituteMatch.product_id)
        .join(ProductExtra, ProductExtra.product_id == Product.id, isouter=True)
        .order_by(ProductSubstituteMatch.created_at.desc(), ProductSubstituteMatch.id.desc())
        .limit(limit)
    )
    if search_text:
        like = _like(search_text)
        substitute_stmt = substitute_stmt.where(
            (Product.code.ilike(like))
            | (Product.name.ilike(like))
            | (ProductExtra.manufacturer.ilike(like))
            | (ProductSubstituteMatch.source_name.ilike(like))
            | (ProductSubstituteMatch.source_manufacturer.ilike(like))
            | (ProductSubstituteMatch.source_distributor_goods_id.ilike(like))
        )

    for row, product, extra in db.execute(substitute_stmt).all():
        out.append(
            {
                "id": f"substitute:{row.id}",
                "kind": "substitute",
                "mappingId": int(row.id),
                "productId": int(product.id),
                "sku": product.code,
                "productName": product.name,
                "productManufacturer": (extra.manufacturer if extra else "") or "",
                "competitorGoodsId": int(row.source_goods_id) if row.source_goods_id is not None else None,
                "distributorGoodsId": row.source_distributor_goods_id or "",
                "competitorName": row.source_name or "",
                "competitorProducer": row.source_manufacturer or "",
                "matchType": "provisor_manual_substitute",
                "source": row.source_type or "provisor",
                "createdAt": row.created_at.isoformat() if row.created_at else "",
                "status": row.status,
                "canDelete": True,
            }
        )

    return out[:limit]


def delete_substitute_mapping(*, db: Session, mapping_id: int) -> None:
    row = db.get(ProductSubstituteMatch, int(mapping_id))
    if row is None:
        raise ValueError("mapping not found")
    db.delete(row)
    db.commit()
