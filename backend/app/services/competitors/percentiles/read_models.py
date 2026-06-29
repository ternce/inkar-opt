from __future__ import annotations

import csv
import io
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
from ...competitor_percentiles import KAZAKHSTAN_REGION, KAZAKHSTAN_SCOPE, PERCENTILES, REGIONAL_SCOPE


def list_percentile_sources(*, db: Session, price_format_code: str | None = None) -> list[dict]:
    """Group stored percentile rows into source-like UI records.

    The percentile engine is already partially present in
    competitor_price_percentiles. Stage 1 only exposes it as a management view.
    """

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
        stmt = stmt.where(CompetitorPricePercentile.price_format_id == pf.id)

    rows = db.execute(stmt.order_by(CompetitorPricePercentile.branch_name.asc(), CompetitorPricePercentile.competitor_name.asc())).all()
    out: list[dict] = []
    for row in rows:
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


def _get_price_format(db: Session, price_format_code: str) -> PriceFormat | None:
    return db.execute(select(PriceFormat).where(PriceFormat.code == price_format_code.strip())).scalars().first()


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
    if region.strip() and competitor.strip():
        return region.strip(), competitor.strip()
    groups = list_percentile_groups(db=db, price_format_code=str(pf.code or ""))
    if not groups:
        return region.strip(), competitor.strip()
    first = groups[0]
    return str(first.get("region") or ""), str(first.get("competitor") or "")


def _price_columns_for_group(db: Session, pf: PriceFormat, *, region: str, competitor: str) -> list[dict]:
    if region == KAZAKHSTAN_REGION:
        return []
    rows = (
        db.execute(
            select(CompetitorPriceList)
            .join(
                PriceFormatCompetitorAssignment,
                PriceFormatCompetitorAssignment.competitor_price_list_id == CompetitorPriceList.id,
            )
            .where(PriceFormatCompetitorAssignment.price_format_id == pf.id)
            .where(PriceFormatCompetitorAssignment.is_active.is_(True))
            .where(CompetitorPriceList.branch_name == region)
            .where(CompetitorPriceList.competitor_name == competitor)
            .order_by(CompetitorPriceList.account_login.asc(), CompetitorPriceList.display_name.asc(), CompetitorPriceList.id.asc())
        )
        .scalars()
        .all()
    )
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
    for row in percentile_rows:
        product_id = int(row.product_id)
        percentiles_by_product[product_id][int(row.percentile)] = _as_float(row.value)
        competitor_count_by_product[product_id] = max(
            int(competitor_count_by_product.get(product_id, 0)),
            int(row.source_count or 0),
        )

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
                "status": status,
                "hasPercentile": has_percentile,
                "hasCompetitors": competitor_count > 0,
            }
        )
    return out, price_columns


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
