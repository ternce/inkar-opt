from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict
from decimal import Decimal

from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ....models import (
    CompetitorPriceList,
    CompetitorPriceListItem,
    CompetitorPricePercentile,
    PriceFormat,
    PriceFormatCompetitorAssignment,
    Product,
    ProductExtra,
    ProductRating,
)
from ...competitor_percentiles import (
    KAZAKHSTAN_REGION,
    KAZAKHSTAN_SCOPE,
    PERCENTILES,
    REGIONAL_SCOPE,
    emit_percentile_group_keys,
)
from ...competitor_source_config import MULTI_PRICE_PERCENTILE_MODE, effective_percentile_mode

logger = logging.getLogger(__name__)


def list_percentile_sources(*, db: Session, price_format_code: str | None = None) -> list[dict]:
    """Group stored percentile rows into source-like UI records.

    The percentile engine is already partially present in
    competitor_price_percentiles. Stage 1 only exposes it as a management view.
    """

    allowed_groups_by_format: dict[int, set[tuple[str, str]]] = {}
    stmt = (
        select(
            CompetitorPricePercentile.price_format_id,
            CompetitorPricePercentile.branch_name,
            CompetitorPricePercentile.competitor_name,
            CompetitorPricePercentile.percentile_scope,
            CompetitorPricePercentile.percentile,
            func.count(func.distinct(CompetitorPricePercentile.product_id)).label("sku_count"),
            func.sum(CompetitorPricePercentile.source_count).label("source_count"),
            func.max(CompetitorPricePercentile.updated_at).label("generated_at"),
        )
        .group_by(
            CompetitorPricePercentile.price_format_id,
            CompetitorPricePercentile.branch_name,
            CompetitorPricePercentile.competitor_name,
            CompetitorPricePercentile.percentile_scope,
            CompetitorPricePercentile.percentile,
        )
    )
    if price_format_code:
        pf = db.execute(select(PriceFormat).where(PriceFormat.code == price_format_code.strip())).scalars().first()
        if pf is None:
            return []
        allowed_groups = emit_percentile_group_keys(db=db, price_format_id=int(pf.id))
        if not allowed_groups:
            return []
        allowed_groups_by_format[int(pf.id)] = allowed_groups
        stmt = stmt.where(CompetitorPricePercentile.price_format_id == pf.id)

    rows = db.execute(stmt.order_by(CompetitorPricePercentile.branch_name.asc(), CompetitorPricePercentile.competitor_name.asc())).all()
    out: list[dict] = []
    for row in rows:
        allowed_groups = allowed_groups_by_format.get(int(row.price_format_id))
        if allowed_groups is None:
            allowed_groups = emit_percentile_group_keys(db=db, price_format_id=int(row.price_format_id))
            allowed_groups_by_format[int(row.price_format_id)] = allowed_groups
        if row.percentile_scope == REGIONAL_SCOPE:
            if _group_key(row.branch_name, row.competitor_name) not in allowed_groups:
                continue
        elif row.percentile_scope == KAZAKHSTAN_SCOPE:
            if not any(competitor == str(row.competitor_name or "").strip() for _branch, competitor in allowed_groups):
                continue
        else:
            continue
        generated_at = row.generated_at.isoformat() if row.generated_at else ""
        out.append(
            {
                "id": f"{row.price_format_id}:{row.percentile_scope}:{row.branch_name}:{row.competitor_name}:p{row.percentile}",
                "priceFormatId": row.price_format_id,
                "region": row.branch_name or "Без филиала",
                "competitor": row.competitor_name or "",
                "scope": row.percentile_scope or REGIONAL_SCOPE,
                "percentile": int(row.percentile),
                "name": f"{row.branch_name or 'Без филиала'} — {row.competitor_name or 'Конкурент'} — P{int(row.percentile)}",
                "skuCount": int(row.sku_count or 0),
                "sourceCount": int(row.source_count or 0),
                "generatedAt": generated_at,
                "sourceType": "percentile",
            }
        )
    return out


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _positive_decimal(value: object) -> Decimal | None:
    dec = _as_decimal(value)
    return dec if dec is not None and dec > 0 else None


def _percentile(values: list[Decimal], percentile: int) -> Decimal | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    pos = (Decimal(percentile) / Decimal(100)) * Decimal(len(ordered) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = pos - Decimal(lower)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _get_price_format(db: Session, price_format_code: str) -> PriceFormat | None:
    return db.execute(select(PriceFormat).where(PriceFormat.code == price_format_code.strip())).scalars().first()


def _assigned_rows_for_group(
    *,
    db: Session,
    pf: PriceFormat,
    region: str,
    competitor: str,
) -> list[tuple[CompetitorPriceList, PriceFormatCompetitorAssignment]]:
    rows = db.execute(
        select(CompetitorPriceList, PriceFormatCompetitorAssignment)
        .join(
            PriceFormatCompetitorAssignment,
            PriceFormatCompetitorAssignment.competitor_price_list_id == CompetitorPriceList.id,
        )
        .where(PriceFormatCompetitorAssignment.price_format_id == pf.id)
        .where(PriceFormatCompetitorAssignment.is_active.is_(True))
        .where(CompetitorPriceList.branch_name == region)
        .where(CompetitorPriceList.competitor_name == competitor)
        .order_by(CompetitorPriceList.id.asc())
    ).all()
    return [
        (row, assignment)
        for row, assignment in rows
        if effective_percentile_mode(row, assignment.percentile_mode) == MULTI_PRICE_PERCENTILE_MODE
    ]


def _ratings_by_product(db: Session, product_ids: list[int], branch_id: str) -> dict[int, dict[str, int | None]]:
    if not product_ids:
        return {}
    rows = (
        db.execute(
            select(ProductRating)
            .where(ProductRating.product_id.in_(product_ids))
            .where(
                (ProductRating.rating_type == "global")
                | ((ProductRating.rating_type == "local") & (ProductRating.branch_id == branch_id))
            )
            .order_by(ProductRating.updated_at.desc(), ProductRating.id.desc())
        )
        .scalars()
        .all()
    )
    out: dict[int, dict[str, int | None]] = {}
    for row in rows:
        product_id = int(row.product_id)
        bucket = out.setdefault(product_id, {"global": None, "local": None})
        key = "local" if row.rating_type == "local" else "global"
        if bucket[key] is None:
            bucket[key] = int(row.rating) if row.rating is not None else None
    return out


def _group_key(region: object, competitor: object) -> tuple[str, str]:
    return str(region or "").strip(), str(competitor or "").strip()


def list_percentile_groups(*, db: Session, price_format_code: str) -> list[dict]:
    pf = _get_price_format(db, price_format_code)
    if pf is None:
        return []
    allowed_groups = emit_percentile_group_keys(db=db, price_format_id=int(pf.id))
    if not allowed_groups:
        return []
    rows = (
        db.execute(
            select(
                CompetitorPricePercentile.branch_name,
                CompetitorPricePercentile.competitor_name,
                CompetitorPricePercentile.percentile_scope,
                func.count(func.distinct(CompetitorPricePercentile.product_id)).label("sku_count"),
                func.sum(CompetitorPricePercentile.source_count).label("source_count"),
                func.max(CompetitorPricePercentile.updated_at).label("generated_at"),
            )
            .where(CompetitorPricePercentile.price_format_id == pf.id)
            .group_by(
                CompetitorPricePercentile.branch_name,
                CompetitorPricePercentile.competitor_name,
                CompetitorPricePercentile.percentile_scope,
            )
            .order_by(CompetitorPricePercentile.branch_name.asc(), CompetitorPricePercentile.competitor_name.asc())
        )
        .all()
    )
    groups: list[dict] = []
    for row in rows:
        region, competitor = _group_key(row.branch_name, row.competitor_name)
        scope = str(row.percentile_scope or REGIONAL_SCOPE)
        if scope == REGIONAL_SCOPE:
            if (region, competitor) not in allowed_groups:
                continue
        elif scope == KAZAKHSTAN_SCOPE:
            if not any(allowed_competitor == competitor for _branch, allowed_competitor in allowed_groups):
                continue
        else:
            continue
        groups.append(
            {
                "id": f"{scope}::{region}::{competitor}",
                "region": region,
                "competitor": competitor,
                "scope": scope,
                "name": f"{region or 'Без филиала'} — {competitor or 'Конкурент'}",
                "skuCount": int(row.sku_count or 0),
                "sourceCount": int(row.source_count or 0),
                "generatedAt": row.generated_at.isoformat() if row.generated_at else "",
            }
        )
    return groups


def _selected_group(db: Session, pf: PriceFormat, region: str = "", competitor: str = "") -> tuple[str, str]:
    requested_region = region.strip()
    requested_competitor = competitor.strip()
    groups = list_percentile_groups(db=db, price_format_code=str(pf.code or ""))
    if requested_region and requested_competitor:
        if any(
            str(group.get("region") or "") == requested_region
            and str(group.get("competitor") or "") == requested_competitor
            for group in groups
        ):
            return requested_region, requested_competitor
        if not groups:
            return requested_region, requested_competitor
    if requested_region:
        region_groups = [group for group in groups if str(group.get("region") or "") == requested_region]
        if region_groups:
            first_region_group = region_groups[0]
            return str(first_region_group.get("region") or ""), str(first_region_group.get("competitor") or "")
    if not groups:
        return region.strip(), competitor.strip()
    first = groups[0]
    return str(first.get("region") or ""), str(first.get("competitor") or "")


def _price_columns_for_group(db: Session, pf: PriceFormat, *, region: str, competitor: str) -> list[dict]:
    if region == KAZAKHSTAN_REGION:
        return []
    rows = [
        row
        for row, _assignment in _assigned_rows_for_group(db=db, pf=pf, region=region, competitor=competitor)
    ]
    rows.sort(key=lambda row: (row.account_login or "", row.display_name or "", int(row.id)))
    seen: dict[str, int] = {}
    columns: list[dict] = []
    for row in rows:
        base_label = (
            row.account_login
            or row.display_name
            or row.supplier
            or row.source_key
            or f"{row.source_type}:{row.id}"
        )
        count = seen.get(base_label, 0) + 1
        seen[base_label] = count
        label = base_label if count == 1 else f"{base_label} ({count})"
        columns.append({"id": int(row.id), "label": label})
    return columns


def _prices_by_product_for_columns(db: Session, product_ids: list[int], columns: list[dict]) -> dict[int, dict[int, float]]:
    if not product_ids or not columns:
        return {}
    list_ids = [int(column["id"]) for column in columns]
    rows = (
        db.execute(
            select(
                CompetitorPriceListItem.price_list_id,
                CompetitorPriceListItem.product_id,
                CompetitorPriceListItem.distributor_price,
            )
            .where(CompetitorPriceListItem.price_list_id.in_(list_ids))
            .where(CompetitorPriceListItem.product_id.in_(product_ids))
            .where(CompetitorPriceListItem.distributor_price.is_not(None))
            .order_by(CompetitorPriceListItem.price_list_id.asc(), CompetitorPriceListItem.id.asc())
        )
        .all()
    )
    out: dict[int, dict[int, Decimal]] = {}
    for row in rows:
        product_id = int(row.product_id or 0)
        price_list_id = int(row.price_list_id or 0)
        price = _as_decimal(row.distributor_price)
        if not product_id or not price_list_id or price is None or price <= 0:
            continue
        out.setdefault(product_id, {})[price_list_id] = price
    return {
        product_id: {price_list_id: float(price) for price_list_id, price in prices.items()}
        for product_id, prices in out.items()
    }


def _build_percentile_browser_rows(*, db: Session, pf: PriceFormat, region: str, competitor: str) -> tuple[list[dict], list[dict]]:
    product_rows = (
        db.execute(
            select(Product, ProductExtra)
            .outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
            .order_by(Product.code.asc())
        )
        .all()
    )
    product_ids = [int(product.id) for product, _extra in product_rows]
    percentile_rows = (
        db.execute(
            select(CompetitorPricePercentile)
            .where(CompetitorPricePercentile.price_format_id == pf.id)
            .where(CompetitorPricePercentile.branch_name == region)
            .where(CompetitorPricePercentile.competitor_name == competitor)
            .where(
                CompetitorPricePercentile.percentile_scope
                == (KAZAKHSTAN_SCOPE if region == KAZAKHSTAN_REGION else REGIONAL_SCOPE)
            )
            .where(CompetitorPricePercentile.product_id.in_(product_ids))
        )
        .scalars()
        .all()
        if product_ids
        else []
    )
    percentiles_by_product: dict[int, dict[int, float | None]] = defaultdict(dict)
    competitor_count_by_product: dict[int, int] = defaultdict(int)
    used_price_count_by_product: dict[int, int] = defaultdict(int)
    status_by_product: dict[int, str] = {}
    for row in percentile_rows:
        product_id = int(row.product_id)
        percentiles_by_product[product_id][int(row.percentile)] = _as_float(row.value)
        competitor_count_by_product[product_id] = max(
            int(competitor_count_by_product.get(product_id, 0)),
            int(row.source_count or 0),
        )
        used_price_count_by_product[product_id] = max(
            int(used_price_count_by_product.get(product_id, 0)),
            int(getattr(row, "used_price_count", 0) or getattr(row, "price_count", 0) or row.source_count or 0),
        )
        if product_id not in status_by_product and getattr(row, "status", ""):
            status_by_product[product_id] = str(row.status or "")

    ratings = _ratings_by_product(db, product_ids, str(pf.branch or ""))
    price_columns = _price_columns_for_group(db, pf, region=region, competitor=competitor)
    prices_by_product = _prices_by_product_for_columns(db, product_ids, price_columns)
    out: list[dict] = []
    for product, extra in product_rows:
        product_id = int(product.id)
        percentile_values = {
            str(percentile): percentiles_by_product.get(product_id, {}).get(percentile)
            for percentile in PERCENTILES
        }
        competitor_count = int(competitor_count_by_product.get(product_id, 0))
        used_price_count = int(used_price_count_by_product.get(product_id, 0))
        has_percentile = any(value is not None for value in percentile_values.values())
        branch_prices = {
            str(column["id"]): prices_by_product.get(product_id, {}).get(int(column["id"]))
            for column in price_columns
        }
        status = "Рассчитан" if has_percentile and competitor_count > 0 else "Нет данных"
        rating = ratings.get(product_id, {})
        out.append(
            {
                "productId": product_id,
                "sku": product.code or "",
                "productName": product.name or "",
                "manufacturer": extra.manufacturer if extra else "",
                "globalRating": rating.get("global"),
                "localRating": rating.get("local"),
                "percentiles": percentile_values,
                "branchPrices": branch_prices,
                "competitorCount": competitor_count,
                "usedPriceCount": used_price_count,
                "calculationStatus": status_by_product.get(product_id, ""),
                "status": status,
                "hasPercentile": has_percentile,
                "hasCompetitors": competitor_count > 0,
            }
        )
    return out, price_columns


def percentile_trace(
    *,
    db: Session,
    price_format_code: str,
    region: str,
    competitor: str,
    sku: str,
) -> dict:
    pf = _get_price_format(db, price_format_code)
    if pf is None:
        return {"found": False, "reason": "price_format_not_found"}
    product = db.execute(select(Product).where(Product.code == sku.strip())).scalars().first()
    if product is None:
        return {"found": False, "reason": "product_not_found"}
    allowed_groups = emit_percentile_group_keys(db=db, price_format_id=int(pf.id))
    if not allowed_groups:
        return {"found": False, "reason": "no_emit_percentile_source_assigned"}
    if region == KAZAKHSTAN_REGION:
        if not any(allowed_competitor == competitor for _branch, allowed_competitor in allowed_groups):
            return {"found": False, "reason": "not_emit_percentile_group"}
        rows = (
            db.execute(
                select(CompetitorPricePercentile)
                .where(CompetitorPricePercentile.price_format_id == pf.id)
                .where(CompetitorPricePercentile.product_id == product.id)
                .where(CompetitorPricePercentile.branch_name == KAZAKHSTAN_REGION)
                .where(CompetitorPricePercentile.competitor_name == competitor)
                .where(CompetitorPricePercentile.percentile_scope == KAZAKHSTAN_SCOPE)
            )
            .scalars()
            .all()
        )
        return {
            "found": True,
            "scope": KAZAKHSTAN_SCOPE,
            "competitor": competitor,
            "region": region,
            "sku": product.code or "",
            "productName": product.name or "",
            "sourceAccountIds": [],
            "rawPricesUsed": [],
            "sortedPrices": [],
            "priceCount": max((int(getattr(row, "price_count", 0) or row.source_count or 0) for row in rows), default=0),
            "usedPriceCount": max((int(getattr(row, "used_price_count", 0) or row.source_count or 0) for row in rows), default=0),
            "status": next((str(row.status or "") for row in rows if getattr(row, "status", "")), ""),
            "percentiles": {str(row.percentile): _as_float(row.value) for row in rows},
            "note": "Kazakhstan percentiles are calculated from regional percentile rows.",
        }

    assigned_rows = _assigned_rows_for_group(db=db, pf=pf, region=region, competitor=competitor)
    if not assigned_rows:
        return {"found": False, "reason": "not_emit_percentile_group"}
    price_list_ids = [int(row.id) for row, _assignment in assigned_rows]
    modes = {
        int(row.id): effective_percentile_mode(row, assignment.percentile_mode)
        for row, assignment in assigned_rows
    }
    item_rows = (
        db.execute(
            select(CompetitorPriceListItem)
            .where(CompetitorPriceListItem.price_list_id.in_(price_list_ids))
            .where(CompetitorPriceListItem.product_id == product.id)
            .order_by(CompetitorPriceListItem.price_list_id.asc(), CompetitorPriceListItem.id.asc())
        )
        .scalars()
        .all()
        if price_list_ids
        else []
    )
    raw_prices: list[dict] = []
    latest_by_list: dict[int, dict] = {}
    raw_rows_count = 0
    for item in item_rows:
        raw_rows_count += 1
        price = _positive_decimal(item.distributor_price)
        if price is None:
            continue
        entry = {
            "sourceId": int(item.price_list_id),
            "itemId": int(item.id),
            "productId": int(item.product_id or 0),
            "goodsId": int(item.provisor_goods_id) if item.provisor_goods_id is not None else None,
            "filialId": int(item.filial_id) if item.filial_id is not None else None,
            "price": float(price),
        }
        if modes.get(int(item.price_list_id)) == MULTI_PRICE_PERCENTILE_MODE:
            raw_prices.append(entry)
        else:
            latest_by_list[int(item.price_list_id)] = entry
    raw_prices.extend(latest_by_list.values())
    raw_prices.sort(key=lambda row: (int(row["sourceId"]), int(row["itemId"])))
    values = [Decimal(str(row["price"])) for row in raw_prices]
    sorted_values = sorted(values)
    percentiles = {
        str(percentile): (float(value) if value is not None else None)
        for percentile, value in ((pct, _percentile(sorted_values, pct)) for pct in PERCENTILES)
    }
    item_prices_in_db = [
        float(price)
        for price in (
            _positive_decimal(item.distributor_price)
            for item in item_rows
        )
        if price is not None
    ]
    payload = {
        "found": True,
        "scope": REGIONAL_SCOPE,
        "competitor": competitor,
        "region": region,
        "sku": product.code or "",
        "productId": int(product.id),
        "productName": product.name or "",
        "sourceAccountIds": [
            {
                "sourceId": int(row.id),
                "accountId": row.account_id or "",
                "accountLogin": row.account_login or "",
                "percentileMode": modes.get(int(row.id), ""),
            }
            for row, _assignment in assigned_rows
        ],
        "rawRowsCount": raw_rows_count,
        "raw_rows_found": raw_rows_count,
        "raw_prices": item_prices_in_db,
        "items_saved_count": len(item_rows),
        "item_prices_in_db": item_prices_in_db,
        "rawPricesUsed": raw_prices,
        "sortedPrices": [float(value) for value in sorted_values],
        "prices_passed_to_percentile_calculation": [float(value) for value in values],
        "priceCount": len(raw_prices),
        "usedPriceCount": len(raw_prices),
        "used_price_count": len(raw_prices),
        "status": "Calculated from one price" if len(raw_prices) == 1 else ("Calculated" if raw_prices else "No data"),
        "percentiles": percentiles,
        "calculated_percentiles": percentiles,
    }
    if str(product.code or "").strip() == "163571" or str(product.provisor_goods_id or "").strip() == "163571":
        logger.info("[EMIT_TRACE] stage=percentile_browser trace=%s", payload)
    return payload


def percentile_coverage_audit(
    *,
    db: Session,
    price_format_code: str,
    region: str,
    competitor: str,
) -> dict:
    pf = _get_price_format(db, price_format_code)
    if pf is None:
        return {"found": False, "reason": "price_format_not_found"}
    assigned_rows = _assigned_rows_for_group(db=db, pf=pf, region=region, competitor=competitor)
    price_list_ids = [int(row.id) for row, _assignment in assigned_rows]
    products_total = int(db.scalar(select(func.count(Product.id))) or 0)
    products_with_goods_id = int(
        db.scalar(select(func.count(Product.id)).where(Product.provisor_goods_id.is_not(None))) or 0
    )
    items = (
        db.execute(
            select(CompetitorPriceListItem)
            .where(CompetitorPriceListItem.price_list_id.in_(price_list_ids))
            .order_by(CompetitorPriceListItem.price_list_id.asc(), CompetitorPriceListItem.id.asc())
        )
        .scalars()
        .all()
        if price_list_ids
        else []
    )
    raw_rows_imported = len(items)
    goods_ids = {int(item.provisor_goods_id) for item in items if item.provisor_goods_id is not None}
    matched_product_ids = {int(item.product_id) for item in items if item.product_id is not None}
    positive_matched_product_ids = {
        int(item.product_id)
        for item in items
        if item.product_id is not None and _positive_decimal(item.distributor_price) is not None
    }
    percentile_product_ids = set(
        int(product_id)
        for product_id in db.execute(
            select(CompetitorPricePercentile.product_id)
            .where(CompetitorPricePercentile.price_format_id == pf.id)
            .where(CompetitorPricePercentile.branch_name == region)
            .where(CompetitorPricePercentile.competitor_name == competitor)
            .where(CompetitorPricePercentile.percentile_scope == REGIONAL_SCOPE)
            .where(CompetitorPricePercentile.value.is_not(None))
            .group_by(CompetitorPricePercentile.product_id)
        ).scalars()
    )
    stored_percentile_products = len(percentile_product_ids)
    rows_without_goods_id = sum(1 for item in items if item.provisor_goods_id is None)
    rows_without_product_id = sum(1 for item in items if item.product_id is None)
    rows_non_positive_price = sum(1 for item in items if item.distributor_price is None or _positive_decimal(item.distributor_price) is None)
    positive_matched_not_stored = sorted(positive_matched_product_ids - percentile_product_ids)
    return {
        "found": True,
        "priceFormatCode": pf.code,
        "region": region,
        "competitor": competitor,
        "activeAssignments": len(assigned_rows),
        "sourcePriceListIds": price_list_ids,
        "counts": {
            "totalCatalogProducts": products_total,
            "productsWithGoodsId": products_with_goods_id,
            "rawEmitRowsImported": raw_rows_imported,
            "distinctGoodsIdInEmitRows": len(goods_ids),
            "distinctMatchedProductIdsFromEmitRows": len(matched_product_ids),
            "distinctMatchedProductIdsWithPositivePrice": len(positive_matched_product_ids),
            "productsPassedIntoPercentileCalculation": len(positive_matched_product_ids),
            "productsWithStoredPercentileRows": stored_percentile_products,
            "productsShownAsNoData": max(0, products_total - stored_percentile_products),
        },
        "dropReasons": {
            "noGoodsIdRows": rows_without_goods_id,
            "noProductIdRows": rows_without_product_id,
            "nonPositivePriceRows": rows_non_positive_price,
            "activeAssignmentFilterDroppedAllRows": raw_rows_imported == 0 and len(assigned_rows) == 0,
            "regionOrCompetitorFilterDroppedAllRows": raw_rows_imported == 0 and len(assigned_rows) > 0,
            "positiveMatchedProductsMissingStoredPercentiles": len(positive_matched_not_stored),
            "positiveMatchedProductIdsMissingStoredPercentilesSample": positive_matched_not_stored[:25],
        },
    }


def _apply_percentile_filters(
    rows: list[dict],
    *,
    q: str = "",
    percentile_filter: str = "all",
    competitor_filter: str = "all",
) -> list[dict]:
    query = q.strip().casefold()
    out = rows
    if query:
        out = [
            row
            for row in out
            if query in str(row.get("sku") or "").casefold()
            or query in str(row.get("productName") or "").casefold()
        ]
    if percentile_filter == "has_percentile":
        out = [row for row in out if row.get("hasPercentile")]
    elif percentile_filter == "no_percentile":
        out = [row for row in out if not row.get("hasPercentile")]
    if competitor_filter == "has_competitors":
        out = [row for row in out if row.get("hasCompetitors")]
    elif competitor_filter == "no_competitors":
        out = [row for row in out if not row.get("hasCompetitors")]
    return out


def _summary_for_percentile_rows(rows: list[dict]) -> dict:
    total = len(rows)
    with_percentile = sum(1 for row in rows if row.get("hasPercentile"))
    with_competitors = sum(1 for row in rows if row.get("hasCompetitors"))
    return {
        "totalProducts": total,
        "productsWithPercentile": with_percentile,
        "productsWithoutPercentile": max(0, total - with_percentile),
        "productsWithCompetitors": with_competitors,
        "productsWithoutCompetitors": max(0, total - with_competitors),
        "coveragePercent": round((with_percentile / total) * 100, 2) if total else 0,
    }


def list_percentile_product_rows(
    *,
    db: Session,
    price_format_code: str,
    region: str = "",
    competitor: str = "",
    q: str = "",
    percentile_filter: str = "all",
    competitor_filter: str = "all",
    sort: str = "sku",
    direction: str = "asc",
    page: int = 1,
    page_size: int = 100,
) -> dict:
    pf = _get_price_format(db, price_format_code)
    if pf is None:
        return {"items": [], "summary": _summary_for_percentile_rows([]), "total": 0, "page": page, "pageSize": page_size, "pageCount": 0, "groups": [], "priceColumns": [], "percentiles": list(PERCENTILES)}
    selected_region, selected_competitor = _selected_group(db, pf, region=region, competitor=competitor)
    groups = list_percentile_groups(db=db, price_format_code=price_format_code)
    all_rows, price_columns = _build_percentile_browser_rows(db=db, pf=pf, region=selected_region, competitor=selected_competitor)
    summary = _summary_for_percentile_rows(all_rows)
    filtered = _apply_percentile_filters(
        all_rows,
        q=q,
        percentile_filter=percentile_filter,
        competitor_filter=competitor_filter,
    )
    sort_key = {
        "sku": lambda row: str(row.get("sku") or ""),
        "name": lambda row: str(row.get("productName") or ""),
        "percentile": lambda row: (row.get("percentiles", {}).get(str(PERCENTILES[0])) is None, row.get("percentiles", {}).get(str(PERCENTILES[0])) or 0),
        "competitor_count": lambda row: row.get("competitorCount") or 0,
        "status": lambda row: str(row.get("status") or ""),
    }.get(sort, lambda row: str(row.get("sku") or ""))
    filtered.sort(key=sort_key, reverse=direction == "desc")
    total = len(filtered)
    page = max(1, page)
    page_size = max(1, page_size)
    start = (page - 1) * page_size
    items = filtered[start : start + page_size]
    return {
        "items": items,
        "summary": summary,
        "total": total,
        "page": page,
        "pageSize": page_size,
        "pageCount": (total + page_size - 1) // page_size if total else 0,
        "percentiles": list(PERCENTILES),
        "groups": groups,
        "selectedRegion": selected_region,
        "selectedCompetitor": selected_competitor,
        "priceColumns": price_columns,
    }


def export_percentile_product_rows(
    *,
    db: Session,
    price_format_code: str,
    fmt: str,
    region: str = "",
    competitor: str = "",
    q: str = "",
    percentile_filter: str = "all",
    competitor_filter: str = "all",
    sort: str = "sku",
    direction: str = "asc",
) -> tuple[str, bytes, str]:
    payload = list_percentile_product_rows(
        db=db,
        price_format_code=price_format_code,
        region=region,
        competitor=competitor,
        q=q,
        percentile_filter=percentile_filter,
        competitor_filter=competitor_filter,
        sort=sort,
        direction=direction,
        page=1,
        page_size=10**9,
    )
    rows = payload["items"]
    price_columns = payload.get("priceColumns") or []
    selected_competitor = str(payload.get("selectedCompetitor") or competitor or "").strip()
    export_columns: list[tuple[str, str]] = [
        ("sku", "Код"),
        ("productName", "Название"),
    ]
    export_columns.extend((f"branch:{column['id']}", str(column.get("label") or column["id"])) for column in price_columns)
    export_columns.extend((f"percentile:{percentile}", f"Персентиль {percentile}_{selected_competitor}") for percentile in PERCENTILES)
    export_columns.extend(
        [
            ("competitorCount", "Competitor price count"),
            ("status", "Status"),
        ]
    )
    safe_code = price_format_code.strip() or "format"
    safe_region = str(payload.get("selectedRegion") or region or "region").replace("/", "_")
    safe_competitor = selected_competitor.replace("/", "_") or "competitor"

    def _export_value(row: dict, key: str) -> object:
        if key.startswith("branch:"):
            return (row.get("branchPrices") or {}).get(key.removeprefix("branch:"))
        if key.startswith("percentile:"):
            return (row.get("percentiles") or {}).get(key.removeprefix("percentile:"))
        return row.get(key)

    if fmt == "xlsx":
        wb = Workbook()
        ws = wb.active
        ws.title = "Percentiles"
        ws.append([label for _key, label in export_columns])
        for row in rows:
            ws.append([_export_value(row, key) for key, _label in export_columns])
        buffer = io.BytesIO()
        wb.save(buffer)
        return f"percentiles_{safe_code}_{safe_region}_{safe_competitor}.xlsx", buffer.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([label for _key, label in export_columns])
    for row in rows:
        writer.writerow([_export_value(row, key) for key, _label in export_columns])
    return f"percentiles_{safe_code}_{safe_region}_{safe_competitor}.csv", output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"
