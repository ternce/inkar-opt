from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict
from decimal import Decimal

from openpyxl import Workbook
from sqlalchemy import case, func, select
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
    RegularCompetitorPricePercentile,
)
from ...competitor_percentiles import (
    KAZAKHSTAN_REGION,
    KAZAKHSTAN_SCOPE,
    PERCENTILES,
    REGIONAL_SCOPE,
    REGULAR_COMPETITOR_SCOPE,
    emit_percentile_group_keys,
    percentile_inc_linear,
    regular_competitor_identity,
)
from ...competitor_assignments import competitor_price_list_read_options
from ...competitors.identity import canonical_regular_competitor_identity, regular_competitor_display_name
from ...competitor_source_config import (
    MULTI_PRICE_PERCENTILE_MODE,
    canonical_competitor_source_key,
    default_percentile_mode_for_source,
    effective_percentile_mode,
)
from .sources import (
    PERCENTILE_SOURCE_COMPETITOR,
    PERCENTILE_SOURCE_DEFAULT,
    PERCENTILE_SOURCE_EMIT,
    get_percentile_provider,
)

logger = logging.getLogger(__name__)


def list_percentile_sources(
    *,
    db: Session,
    price_format_code: str | None = None,
    percentile_source: str = PERCENTILE_SOURCE_DEFAULT,
    include_ineligible: bool = False,
    source_ids: set[str] | None = None,
    assigned_regular_identities: set[str] | None = None,
) -> list[dict]:
    """Group stored percentile rows into source-like UI records.

    The percentile engine is already partially present in
    competitor_price_percentiles. Stage 1 only exposes it as a management view.
    """

    provider = get_percentile_provider(percentile_source)
    if provider.key == PERCENTILE_SOURCE_COMPETITOR:
        return _list_competitor_percentile_sources(
            db=db,
            price_format_code=price_format_code,
            include_ineligible=include_ineligible,
            source_ids=source_ids,
            assigned_regular_identities=assigned_regular_identities,
        )

    allowed_groups_by_format: dict[int, set[tuple[str, str, str]]] = {}
    stmt = (
        select(
            CompetitorPricePercentile.price_format_id,
            CompetitorPricePercentile.source_type,
            CompetitorPricePercentile.source_key,
            CompetitorPricePercentile.competitor_price_list_id,
            CompetitorPricePercentile.branch_name,
            CompetitorPricePercentile.competitor_name,
            CompetitorPricePercentile.percentile_scope,
            CompetitorPricePercentile.percentile,
            func.count(func.distinct(CompetitorPricePercentile.product_id)).label("sku_count"),
            func.sum(CompetitorPricePercentile.source_count).label("source_count"),
            func.max(CompetitorPricePercentile.updated_at).label("generated_at"),
        )
        .where(provider.row_filter())
        .group_by(
            CompetitorPricePercentile.price_format_id,
            CompetitorPricePercentile.source_type,
            CompetitorPricePercentile.source_key,
            CompetitorPricePercentile.competitor_price_list_id,
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
        if not allowed_groups and not include_ineligible:
            return []
        allowed_groups_by_format[int(pf.id)] = allowed_groups
        stmt = stmt.where(CompetitorPricePercentile.price_format_id == pf.id)

    rows = db.execute(stmt.order_by(CompetitorPricePercentile.branch_name.asc(), CompetitorPricePercentile.competitor_name.asc())).all()
    requested_source_ids = {str(item or "").strip() for item in (source_ids or set()) if str(item or "").strip()}
    out: list[dict] = []
    for row in rows:
        allowed_groups = allowed_groups_by_format.get(int(row.price_format_id))
        if allowed_groups is None:
            allowed_groups = emit_percentile_group_keys(db=db, price_format_id=int(row.price_format_id))
            allowed_groups_by_format[int(row.price_format_id)] = allowed_groups
        eligible_for_pricing = False
        if row.percentile_scope == REGIONAL_SCOPE:
            eligible_for_pricing = _group_key(row.branch_name, row.competitor_name, row.source_key) in allowed_groups
        elif row.percentile_scope == KAZAKHSTAN_SCOPE:
            eligible_for_pricing = any(competitor == str(row.competitor_name or "").strip() for _branch, competitor, _source_key in allowed_groups)
        if not eligible_for_pricing and not include_ineligible:
            continue
        generated_at = row.generated_at.isoformat() if row.generated_at else ""
        source_id = provider.source_id(
            price_format_id=row.price_format_id,
            scope=row.percentile_scope,
            source_key=row.source_key,
            region=row.branch_name,
            competitor=row.competitor_name,
            percentile=row.percentile,
        )
        if requested_source_ids and source_id not in requested_source_ids:
            continue
        out.append(
            {
                "id": source_id,
                "percentileSource": PERCENTILE_SOURCE_EMIT,
                "priceFormatId": row.price_format_id,
                "competitorPriceListId": row.competitor_price_list_id,
                "sourceKey": row.source_key or "",
                "percentileSourceType": row.source_type or "",
                "region": row.branch_name or "Без филиала",
                "competitor": row.competitor_name or "",
                "scope": row.percentile_scope or REGIONAL_SCOPE,
                "percentile": int(row.percentile),
                "name": f"{row.branch_name or 'Без филиала'} — {row.competitor_name or 'Конкурент'} — P{int(row.percentile)}",
                "skuCount": int(row.sku_count or 0),
                "sourceCount": int(row.source_count or 0),
                "generatedAt": generated_at,
                "sourceType": "percentile",
                "eligibleForPricing": eligible_for_pricing,
                "pricingEligibilityReason": "" if eligible_for_pricing else "no_active_physical_emit_assignment",
            }
        )
    return out


def _list_competitor_percentile_sources(
    *,
    db: Session,
    price_format_code: str | None = None,
    include_ineligible: bool = False,
    source_ids: set[str] | None = None,
    assigned_regular_identities: set[str] | None = None,
) -> list[dict]:
    provider = get_percentile_provider(PERCENTILE_SOURCE_COMPETITOR)
    pf: PriceFormat | None = None
    allowed_identities: set[str] | None = None
    if price_format_code:
        pf = db.execute(select(PriceFormat).where(PriceFormat.code == price_format_code.strip())).scalars().first()
        if pf is None:
            return []
        allowed_identities = (
            assigned_regular_identities
            if assigned_regular_identities is not None
            else _assigned_regular_identities(db=db, price_format_id=int(pf.id))
        )
    requested_source_ids = {str(item or "").strip() for item in (source_ids or set()) if str(item or "").strip()}
    requested_regular_identities = _regular_identities_from_source_ids(
        requested_source_ids,
        price_format_id=int(pf.id) if pf is not None else None,
    )
    metadata = _regular_source_metadata(
        db=db,
        price_format_id=int(pf.id) if pf is not None else None,
        assigned_regular_identities=allowed_identities,
        requested_identities=requested_regular_identities,
    )
    stmt = (
        select(
            RegularCompetitorPricePercentile.competitor_identity,
            func.min(RegularCompetitorPricePercentile.competitor_name).label("competitor_name"),
            RegularCompetitorPricePercentile.percentile,
            func.count(func.distinct(RegularCompetitorPricePercentile.product_id)).label("sku_count"),
            func.sum(RegularCompetitorPricePercentile.source_count).label("source_count"),
            func.max(RegularCompetitorPricePercentile.calculated_at).label("generated_at"),
        )
        .group_by(
            RegularCompetitorPricePercentile.competitor_identity,
            RegularCompetitorPricePercentile.percentile,
        )
    )
    if requested_regular_identities:
        stmt = stmt.where(RegularCompetitorPricePercentile.competitor_identity.in_(requested_regular_identities))
    rows = db.execute(stmt.order_by(func.min(RegularCompetitorPricePercentile.competitor_name).asc())).all()
    stored_identities = {str(row.competitor_identity or "").strip() for row in rows if str(row.competitor_identity or "").strip()}
    out: list[dict] = []
    for row in rows:
        competitor = str(row.competitor_name or "").strip()
        if not competitor:
            competitor = regular_competitor_display_name(row.competitor_identity, row.competitor_identity)
        source_key = str(row.competitor_identity or "").strip()
        if not source_key:
            continue
        if _is_obsolete_regular_identity(source_key, stored_identities):
            continue
        price_format_id = int(pf.id) if pf is not None else 0
        eligible_for_pricing = allowed_identities is None or source_key in allowed_identities
        if not eligible_for_pricing and not include_ineligible:
            continue
        generated_at = row.generated_at.isoformat() if row.generated_at else ""
        source_meta = metadata.get(source_key, {})
        # Regular competitor rows are global datasets stored in
        # regular_competitor_price_percentiles. Availability must not depend on
        # a physical regional PLK still being assigned to this price format.
        eligible_for_pricing = True
        source_id = provider.source_id(
            price_format_id=price_format_id,
            scope=REGULAR_COMPETITOR_SCOPE,
            source_key=source_key,
            region="",
            competitor=competitor,
            percentile=row.percentile,
        )
        if requested_source_ids and source_id not in requested_source_ids:
            continue
        out.append(
            {
                "apiIdentity": f"regular:{source_key}",
                "id": source_id,
                "percentileSource": PERCENTILE_SOURCE_COMPETITOR,
                "priceFormatId": price_format_id,
                "competitorPriceListId": None,
                "sourceKey": source_key,
                "percentileSourceType": "regular_competitor",
                "region": "",
                "competitor": competitor,
                "scope": REGULAR_COMPETITOR_SCOPE,
                "percentile": int(row.percentile),
                "name": f"{competitor or 'Конкурент'} — P{int(row.percentile)}",
                "skuCount": int(row.sku_count or 0),
                "sourceCount": int(row.source_count or 0),
                "generatedAt": generated_at,
                "sourceType": "percentile",
                "eligibleForPricing": eligible_for_pricing,
                "pricingEligibilityReason": "",
                "assignedToPriceFormat": bool(source_meta.get("assignedToPriceFormat")),
                "physicalPriceListCount": int(source_meta.get("physicalPriceListCount") or 0),
                "regionsIncluded": source_meta.get("regionsIncluded") or [],
                "accountsIncluded": source_meta.get("accountsIncluded") or [],
                "matchedRows": int(source_meta.get("matchedRows") or 0),
                "canonicalDisplayName": regular_competitor_display_name(source_key, competitor),
            }
        )
    return out


def _regular_identities_from_source_ids(source_ids: set[str], price_format_id: int | None) -> set[str]:
    if not source_ids or not price_format_id:
        return set()
    prefix = f"{PERCENTILE_SOURCE_COMPETITOR}:{price_format_id}:{REGULAR_COMPETITOR_SCOPE}:"
    out: set[str] = set()
    for source_id in source_ids:
        text = str(source_id or "").strip()
        if not text.startswith(prefix) or ":p" not in text:
            continue
        before_percentile = text.removeprefix(prefix).rsplit(":p", 1)[0]
        source_key = before_percentile.split("::", 1)[0]
        canonical = canonical_regular_competitor_identity(source_key)
        if canonical:
            out.add(canonical)
    return out


def _is_obsolete_regular_identity(stored_identity: object, stored_identities: set[str]) -> bool:
    identity = str(stored_identity or "").strip()
    canonical = canonical_regular_competitor_identity(identity)
    return bool(canonical and canonical != identity and canonical in stored_identities)


def _regular_source_metadata(
    *,
    db: Session,
    price_format_id: int | None = None,
    assigned_regular_identities: set[str] | None = None,
    requested_identities: set[str] | None = None,
) -> dict[str, dict]:
    rows = (
        db.execute(
            select(CompetitorPriceList)
            .options(competitor_price_list_read_options())
            .order_by(CompetitorPriceList.id.asc())
        )
        .scalars()
        .all()
    )
    metadata_rows: list[CompetitorPriceList] = []
    for row in rows:
        identity = regular_competitor_identity(row)
        if (
            not identity
            or (requested_identities and identity not in requested_identities)
            or canonical_competitor_source_key(row).startswith("emit:")
            or default_percentile_mode_for_source(row) == MULTI_PRICE_PERCENTILE_MODE
        ):
            continue
        metadata_rows.append(row)
    ids = [int(row.id) for row in metadata_rows]
    item_counts = dict(
        db.execute(
            select(CompetitorPriceListItem.price_list_id, func.count())
            .where(CompetitorPriceListItem.price_list_id.in_(ids))
            .where(CompetitorPriceListItem.distributor_price.is_not(None))
            .where(CompetitorPriceListItem.distributor_price > 0)
            .where(
                (CompetitorPriceListItem.product_id.is_not(None))
                | (CompetitorPriceListItem.provisor_goods_id.is_not(None))
                | (CompetitorPriceListItem.matched_sku != "")
                | (CompetitorPriceListItem.distributor_goods_id != "")
            )
            .group_by(CompetitorPriceListItem.price_list_id)
        ).all()
    ) if ids else {}
    assigned = (
        assigned_regular_identities
        if assigned_regular_identities is not None
        else (_assigned_regular_identities(db=db, price_format_id=price_format_id) if price_format_id else set())
    )
    out: dict[str, dict] = {}
    for row in metadata_rows:
        identity = regular_competitor_identity(row)
        meta = out.setdefault(
            identity,
            {
                "physicalPriceListIds": set(),
                "regionsIncluded": set(),
                "accountsIncluded": set(),
                "matchedRows": 0,
                "assignedToPriceFormat": identity in assigned,
            },
        )
        meta["physicalPriceListIds"].add(int(row.id))
        region = str(row.branch_name or row.region or "").strip()
        if region:
            meta["regionsIncluded"].add(region)
        account = str(row.account_login or row.account_id or "").strip()
        if account:
            meta["accountsIncluded"].add(account)
        meta["matchedRows"] += int(item_counts.get(int(row.id), 0) or 0)
    return {
        identity: {
            "physicalPriceListCount": len(meta["physicalPriceListIds"]),
            "regionsIncluded": sorted(meta["regionsIncluded"]),
            "accountsIncluded": sorted(meta["accountsIncluded"]),
            "matchedRows": int(meta["matchedRows"] or 0),
            "assignedToPriceFormat": bool(meta["assignedToPriceFormat"]),
        }
        for identity, meta in out.items()
    }


def _assigned_regular_identities(*, db: Session, price_format_id: int | None) -> set[str]:
    if not price_format_id:
        return set()
    assigned = (
        db.execute(
            select(CompetitorPriceList, PriceFormatCompetitorAssignment)
            .options(competitor_price_list_read_options())
            .join(
                PriceFormatCompetitorAssignment,
                PriceFormatCompetitorAssignment.competitor_price_list_id == CompetitorPriceList.id,
            )
            .where(PriceFormatCompetitorAssignment.price_format_id == price_format_id)
            .where(PriceFormatCompetitorAssignment.is_active.is_(True))
        )
        .all()
    )
    return {
        identity
        for row, _assignment in assigned
        for identity in (regular_competitor_identity(row),)
        if identity
        and not canonical_competitor_source_key(row).startswith("emit:")
        and default_percentile_mode_for_source(row) != MULTI_PRICE_PERCENTILE_MODE
    }


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


def _get_price_format(db: Session, price_format_code: str) -> PriceFormat | None:
    return db.execute(select(PriceFormat).where(PriceFormat.code == price_format_code.strip())).scalars().first()


def _assigned_rows_for_group(
    *,
    db: Session,
    pf: PriceFormat,
    region: str,
    competitor: str,
    source_key: str = "",
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
    requested_source_key = str(source_key or "").strip()
    if requested_source_key:
        rows = [(row, assignment) for row, assignment in rows if canonical_competitor_source_key(row) == requested_source_key]
    return [
        (row, assignment)
        for row, assignment in rows
        if effective_percentile_mode(row, assignment.percentile_mode) == MULTI_PRICE_PERCENTILE_MODE
        and canonical_competitor_source_key(row)
    ]


def _competitor_percentile_group_keys(*, db: Session, price_format_id: int) -> set[tuple[str, str]]:
    rows = (
        db.execute(
            select(CompetitorPriceList, PriceFormatCompetitorAssignment)
            .join(
                PriceFormatCompetitorAssignment,
                PriceFormatCompetitorAssignment.competitor_price_list_id == CompetitorPriceList.id,
            )
            .where(PriceFormatCompetitorAssignment.price_format_id == price_format_id)
            .where(PriceFormatCompetitorAssignment.is_active.is_(True))
            .order_by(CompetitorPriceList.id.asc())
        )
        .all()
    )
    return {
        (competitor, source_key)
        for row, assignment in rows
        for competitor in (str(row.competitor_name or "").strip(),)
        for source_key in (canonical_competitor_source_key(row),)
        if competitor
        and source_key
        and effective_percentile_mode(row, assignment.percentile_mode) == MULTI_PRICE_PERCENTILE_MODE
    }


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


def _group_key(region: object, competitor: object, source_key: object = "") -> tuple[str, str, str]:
    return str(region or "").strip(), str(competitor or "").strip(), str(source_key or "").strip()


def list_percentile_groups(
    *,
    db: Session,
    price_format_code: str,
    percentile_source: str = PERCENTILE_SOURCE_DEFAULT,
) -> list[dict]:
    pf = _get_price_format(db, price_format_code)
    if pf is None:
        return []
    provider = get_percentile_provider(percentile_source)
    if provider.key == PERCENTILE_SOURCE_COMPETITOR:
        return _list_competitor_percentile_groups(db=db, pf=pf)
    allowed_groups = emit_percentile_group_keys(db=db, price_format_id=int(pf.id))
    if not allowed_groups:
        return []
    rows = (
        db.execute(
            select(
                CompetitorPricePercentile.branch_name,
                CompetitorPricePercentile.competitor_name,
                CompetitorPricePercentile.source_key,
                CompetitorPricePercentile.percentile_scope,
                func.count(func.distinct(CompetitorPricePercentile.product_id)).label("sku_count"),
                func.sum(CompetitorPricePercentile.source_count).label("source_count"),
                func.max(CompetitorPricePercentile.updated_at).label("generated_at"),
            )
            .where(CompetitorPricePercentile.price_format_id == pf.id)
            .where(provider.row_filter())
            .group_by(
                CompetitorPricePercentile.branch_name,
                CompetitorPricePercentile.competitor_name,
                CompetitorPricePercentile.source_key,
                CompetitorPricePercentile.percentile_scope,
            )
            .order_by(CompetitorPricePercentile.branch_name.asc(), CompetitorPricePercentile.competitor_name.asc())
        )
        .all()
    )
    groups: list[dict] = []
    for row in rows:
        region, competitor, source_key = _group_key(row.branch_name, row.competitor_name, row.source_key)
        scope = str(row.percentile_scope or REGIONAL_SCOPE)
        if scope == REGIONAL_SCOPE:
            if (region, competitor, source_key) not in allowed_groups:
                continue
        elif scope == KAZAKHSTAN_SCOPE:
            if not any(allowed_competitor == competitor for _branch, allowed_competitor, _source_key in allowed_groups):
                continue
        else:
            continue
        groups.append(
            {
                "id": f"{scope}::{source_key}::{region}::{competitor}",
                "sourceKey": source_key,
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


def _list_competitor_percentile_groups(*, db: Session, pf: PriceFormat) -> list[dict]:
    metadata = _regular_source_metadata(db=db, price_format_id=int(pf.id))
    rows = (
        db.execute(
            select(
                RegularCompetitorPricePercentile.competitor_identity,
                func.min(RegularCompetitorPricePercentile.competitor_name).label("competitor_name"),
                func.count(func.distinct(RegularCompetitorPricePercentile.product_id)).label("sku_count"),
                func.sum(RegularCompetitorPricePercentile.source_count).label("source_count"),
                func.max(RegularCompetitorPricePercentile.calculated_at).label("generated_at"),
            )
            .group_by(
                RegularCompetitorPricePercentile.competitor_identity,
            )
            .order_by(func.min(RegularCompetitorPricePercentile.competitor_name).asc())
        )
        .all()
    )
    stored_identities = {str(row.competitor_identity or "").strip() for row in rows if str(row.competitor_identity or "").strip()}
    groups: list[dict] = []
    for row in rows:
        source_key = str(row.competitor_identity or "").strip()
        if not source_key:
            continue
        if _is_obsolete_regular_identity(source_key, stored_identities):
            continue
        competitor = str(row.competitor_name or "").strip() or regular_competitor_display_name(source_key, source_key)
        source_meta = metadata.get(source_key, {})
        groups.append(
            {
                "id": f"regular:{source_key}",
                "apiIdentity": f"regular:{source_key}",
                "sourceKey": source_key,
                "region": "",
                "competitor": competitor,
                "scope": REGULAR_COMPETITOR_SCOPE,
                "name": competitor,
                "skuCount": int(row.sku_count or 0),
                "sourceCount": int(row.source_count or 0),
                "generatedAt": row.generated_at.isoformat() if row.generated_at else "",
                "assignedToPriceFormat": bool(source_meta.get("assignedToPriceFormat")),
                "physicalPriceListCount": int(source_meta.get("physicalPriceListCount") or 0),
                "regionsIncluded": source_meta.get("regionsIncluded") or [],
                "accountsIncluded": source_meta.get("accountsIncluded") or [],
            }
        )
    return groups


def _selected_group(
    db: Session,
    pf: PriceFormat,
    region: str = "",
    competitor: str = "",
    source_key: str = "",
    percentile_source: str = PERCENTILE_SOURCE_DEFAULT,
) -> tuple[str, str, str]:
    requested_region = region.strip()
    requested_competitor = competitor.strip()
    requested_source_key = source_key.strip()
    groups = list_percentile_groups(
        db=db,
        price_format_code=str(pf.code or ""),
        percentile_source=percentile_source,
    )
    if get_percentile_provider(percentile_source).key == PERCENTILE_SOURCE_COMPETITOR:
        if requested_competitor:
            match = next(
                (
                    group
                    for group in groups
                    if str(group.get("competitor") or "") == requested_competitor
                    and (not requested_source_key or str(group.get("sourceKey") or "") == requested_source_key)
                ),
                None,
            )
            if match is not None:
                return "", str(match.get("competitor") or ""), str(match.get("sourceKey") or "")
            return "", "", ""
        if groups:
            first = groups[0]
            return "", str(first.get("competitor") or ""), str(first.get("sourceKey") or "")
        return "", "", ""
    if requested_source_key:
        for group in groups:
            if str(group.get("sourceKey") or "") == requested_source_key:
                return (
                    str(group.get("region") or ""),
                    str(group.get("competitor") or ""),
                    str(group.get("sourceKey") or ""),
                )
    if requested_region and requested_competitor:
        match = next(
            (
                group
                for group in groups
                if str(group.get("region") or "") == requested_region
                and str(group.get("competitor") or "") == requested_competitor
            ),
            None,
        )
        if match is not None:
            return requested_region, requested_competitor, requested_source_key
        if not groups:
            return requested_region, requested_competitor, requested_source_key
    if requested_region:
        region_groups = [group for group in groups if str(group.get("region") or "") == requested_region]
        if region_groups:
            first_region_group = region_groups[0]
            return str(first_region_group.get("region") or ""), str(first_region_group.get("competitor") or ""), requested_source_key
    if not groups:
        return region.strip(), competitor.strip(), source_key.strip()
    first = groups[0]
    return str(first.get("region") or ""), str(first.get("competitor") or ""), requested_source_key


def _price_columns_for_group(db: Session, pf: PriceFormat, *, region: str, competitor: str, source_key: str = "") -> list[dict]:
    if region == KAZAKHSTAN_REGION:
        return []
    rows = [
        row
        for row, _assignment in _assigned_rows_for_group(db=db, pf=pf, region=region, competitor=competitor, source_key=source_key)
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


def _available_percentiles_for_group(
    *,
    db: Session,
    pf: PriceFormat,
    region: str,
    competitor: str,
    source_key: str,
    percentile_source: str = PERCENTILE_SOURCE_DEFAULT,
) -> list[int]:
    provider = get_percentile_provider(percentile_source)
    if provider.key == PERCENTILE_SOURCE_COMPETITOR:
        stmt = (
            select(CompetitorPricePercentile.percentile)
            .where(CompetitorPricePercentile.price_format_id == pf.id)
            .where(provider.row_filter())
            .where(CompetitorPricePercentile.competitor_name == competitor)
        )
        requested_source_key = str(source_key or "").strip()
        if requested_source_key:
            stmt = stmt.where(CompetitorPricePercentile.source_key == requested_source_key)
        levels = [
            int(value)
            for value in db.execute(
                stmt.group_by(CompetitorPricePercentile.percentile).order_by(CompetitorPricePercentile.percentile.asc())
            ).scalars()
        ]
        return levels or list(PERCENTILES)
    scope = KAZAKHSTAN_SCOPE if region == KAZAKHSTAN_REGION else REGIONAL_SCOPE
    stmt = (
        select(CompetitorPricePercentile.percentile)
        .where(CompetitorPricePercentile.price_format_id == pf.id)
        .where(CompetitorPricePercentile.branch_name == region)
        .where(CompetitorPricePercentile.competitor_name == competitor)
        .where(CompetitorPricePercentile.percentile_scope == scope)
    )
    requested_source_key = str(source_key or "").strip()
    if requested_source_key:
        stmt = stmt.where(CompetitorPricePercentile.source_key == requested_source_key)
    levels = [
        int(value)
        for value in db.execute(
            stmt.group_by(CompetitorPricePercentile.percentile).order_by(CompetitorPricePercentile.percentile.asc())
        ).scalars()
    ]
    return levels or list(PERCENTILES)


def _build_percentile_browser_rows(
    *,
    db: Session,
    pf: PriceFormat,
    region: str,
    competitor: str,
    source_key: str = "",
    percentile_numbers: list[int] | None = None,
    percentile_source: str = PERCENTILE_SOURCE_DEFAULT,
    product_rows: list[tuple[Product, ProductExtra | None]] | None = None,
) -> tuple[list[dict], list[dict]]:
    if product_rows is None:
        product_rows = (
            db.execute(
                select(Product, ProductExtra)
                .outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
                .order_by(Product.code.asc())
            )
            .all()
        )
    product_ids = [int(product.id) for product, _extra in product_rows]
    provider = get_percentile_provider(percentile_source)
    percentile_stmt = (
        select(CompetitorPricePercentile)
        .where(CompetitorPricePercentile.price_format_id == pf.id)
        .where(provider.row_filter())
        .where(CompetitorPricePercentile.competitor_name == competitor)
        .where(CompetitorPricePercentile.product_id.in_(product_ids))
    )
    if provider.regional:
        percentile_stmt = percentile_stmt.where(CompetitorPricePercentile.branch_name == region).where(
            CompetitorPricePercentile.percentile_scope
            == (KAZAKHSTAN_SCOPE if region == KAZAKHSTAN_REGION else REGIONAL_SCOPE)
        )
    if str(source_key or "").strip():
        percentile_stmt = percentile_stmt.where(CompetitorPricePercentile.source_key == str(source_key or "").strip())
    percentile_rows = (
        db.execute(percentile_stmt)
        .scalars()
        .all()
        if product_ids
        else []
    )
    percentiles_by_product: dict[int, dict[int, float | None]] = defaultdict(dict)
    percentile_values_by_product: dict[int, dict[int, list[Decimal]]] = defaultdict(lambda: defaultdict(list))
    aggregate_stored_percentiles = not str(source_key or "").strip() or provider.key == PERCENTILE_SOURCE_COMPETITOR
    competitor_count_by_product: dict[int, int] = defaultdict(int)
    used_price_count_by_product: dict[int, int] = defaultdict(int)
    status_by_product: dict[int, str] = {}
    for row in percentile_rows:
        product_id = int(row.product_id)
        row_value = _as_decimal(row.value)
        if aggregate_stored_percentiles and row_value is not None:
            percentile_values_by_product[product_id][int(row.percentile)].append(row_value)
        else:
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
    if aggregate_stored_percentiles:
        for product_id, by_percentile in percentile_values_by_product.items():
            for percentile, values in by_percentile.items():
                value = percentile_inc_linear(values, percentile)
                percentiles_by_product[product_id][percentile] = float(value) if value is not None else None

    ratings = _ratings_by_product(db, product_ids, str(pf.branch or ""))
    price_columns = (
        _price_columns_for_group(db, pf, region=region, competitor=competitor, source_key=source_key)
        if provider.regional
        else []
    )
    prices_by_product = _prices_by_product_for_columns(db, product_ids, price_columns)
    out: list[dict] = []
    for product, extra in product_rows:
        product_id = int(product.id)
        percentile_values = {
            str(percentile): percentiles_by_product.get(product_id, {}).get(percentile)
            for percentile in (percentile_numbers or list(PERCENTILES))
        }
        branch_prices = {
            str(column["id"]): prices_by_product.get(product_id, {}).get(int(column["id"]))
            for column in price_columns
        }
        aggregate_count = sum(1 for value in branch_prices.values() if value is not None) if aggregate_stored_percentiles else 0
        competitor_count = aggregate_count or int(competitor_count_by_product.get(product_id, 0))
        used_price_count = aggregate_count or int(used_price_count_by_product.get(product_id, 0))
        has_percentile = any(value is not None for value in percentile_values.values())
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
    source_key: str = "",
    percentile_source: str = PERCENTILE_SOURCE_DEFAULT,
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
    requested_source_key = str(source_key or "").strip()
    if region == KAZAKHSTAN_REGION:
        if not any(allowed_competitor == competitor for _branch, allowed_competitor, _source_key in allowed_groups):
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

    assigned_rows = _assigned_rows_for_group(db=db, pf=pf, region=region, competitor=competitor, source_key=requested_source_key)
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
        for percentile, value in ((pct, percentile_inc_linear(sorted_values, pct)) for pct in PERCENTILES)
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
            "sourceKey": requested_source_key,
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
    source_key: str = "",
    percentile_source: str = PERCENTILE_SOURCE_DEFAULT,
) -> dict:
    pf = _get_price_format(db, price_format_code)
    if pf is None:
        return {"found": False, "reason": "price_format_not_found"}
    requested_source_key = str(source_key or "").strip()
    assigned_rows = _assigned_rows_for_group(db=db, pf=pf, region=region, competitor=competitor, source_key=requested_source_key)
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
    percentile_stmt = (
        select(CompetitorPricePercentile.product_id)
        .where(CompetitorPricePercentile.price_format_id == pf.id)
        .where(CompetitorPricePercentile.branch_name == region)
        .where(CompetitorPricePercentile.competitor_name == competitor)
        .where(CompetitorPricePercentile.percentile_scope == REGIONAL_SCOPE)
        .where(CompetitorPricePercentile.value.is_not(None))
    )
    if requested_source_key:
        percentile_stmt = percentile_stmt.where(CompetitorPricePercentile.source_key == requested_source_key)
    percentile_product_ids = set(
        int(product_id)
        for product_id in db.execute(percentile_stmt.group_by(CompetitorPricePercentile.product_id)).scalars()
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
        "sourceKey": requested_source_key,
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


def _percentile_browser_aggregate_subquery(
    *,
    pf: PriceFormat,
    provider,
    region: str,
    competitor: str,
    source_key: str,
):
    stmt = (
        select(
            CompetitorPricePercentile.product_id.label("product_id"),
            func.max(
                case((CompetitorPricePercentile.value.is_not(None), 1), else_=0)
            ).label("has_percentile"),
            func.max(
                case((CompetitorPricePercentile.source_count > 0, 1), else_=0)
            ).label("has_competitors"),
            func.max(
                case(
                    (CompetitorPricePercentile.percentile == int(PERCENTILES[0]), CompetitorPricePercentile.value),
                    else_=None,
                )
            ).label("first_percentile_value"),
            func.max(CompetitorPricePercentile.source_count).label("competitor_count"),
        )
        .where(CompetitorPricePercentile.price_format_id == pf.id)
        .where(provider.row_filter())
        .where(CompetitorPricePercentile.competitor_name == competitor)
    )
    if provider.regional:
        stmt = stmt.where(CompetitorPricePercentile.branch_name == region).where(
            CompetitorPricePercentile.percentile_scope
            == (KAZAKHSTAN_SCOPE if region == KAZAKHSTAN_REGION else REGIONAL_SCOPE)
        )
    requested_source_key = str(source_key or "").strip()
    if requested_source_key:
        stmt = stmt.where(CompetitorPricePercentile.source_key == requested_source_key)
    return stmt.group_by(CompetitorPricePercentile.product_id).subquery()


def _percentile_browser_summary(db: Session, percentile_agg) -> dict:
    total = int(db.scalar(select(func.count(Product.id))) or 0)
    with_percentile = int(
        db.scalar(select(func.count()).select_from(percentile_agg).where(percentile_agg.c.has_percentile > 0)) or 0
    )
    with_competitors = int(
        db.scalar(select(func.count()).select_from(percentile_agg).where(percentile_agg.c.has_competitors > 0)) or 0
    )
    return {
        "totalProducts": total,
        "productsWithPercentile": with_percentile,
        "productsWithoutPercentile": max(0, total - with_percentile),
        "productsWithCompetitors": with_competitors,
        "productsWithoutCompetitors": max(0, total - with_competitors),
        "coveragePercent": round((with_percentile / total) * 100, 2) if total else 0,
    }


def _select_regular_group(
    *,
    groups: list[dict],
    api_identity: object = "",
    competitor: object = "",
    source_key: object = "",
) -> dict | None:
    if not groups:
        return None
    by_api_identity = {str(group.get("apiIdentity") or group.get("id") or "").strip(): group for group in groups}
    requested_api_identity = str(api_identity or "").strip()
    if requested_api_identity and not requested_api_identity.startswith("regular:"):
        requested_api_identity = f"regular:{canonical_regular_competitor_identity(requested_api_identity)}"
    if requested_api_identity in by_api_identity:
        return by_api_identity[requested_api_identity]

    requested_competitor = str(competitor or "").strip()
    if requested_competitor:
        requested_identity = canonical_regular_competitor_identity(requested_competitor)
        for group in groups:
            if str(group.get("sourceKey") or "").strip() == requested_identity:
                return group
        for group in groups:
            if str(group.get("competitor") or "").strip() == requested_competitor:
                return group

    requested_source_key = canonical_regular_competitor_identity(str(source_key or "").strip())
    if requested_source_key:
        for group in groups:
            if str(group.get("sourceKey") or "").strip() == requested_source_key:
                return group
    return groups[0]


def list_percentile_product_rows(
    *,
    db: Session,
    price_format_code: str,
    region: str = "",
    competitor: str = "",
    source_key: str = "",
    api_identity: str = "",
    group_id: str = "",
    q: str = "",
    percentile_filter: str = "all",
    competitor_filter: str = "all",
    sort: str = "sku",
    direction: str = "asc",
    page: int = 1,
    page_size: int = 100,
    percentile_source: str = PERCENTILE_SOURCE_DEFAULT,
) -> dict:
    pf = _get_price_format(db, price_format_code)
    provider = get_percentile_provider(percentile_source)
    if pf is None:
        return {"items": [], "summary": _summary_for_percentile_rows([]), "total": 0, "page": page, "pageSize": page_size, "pageCount": 0, "groups": [], "priceColumns": [], "percentiles": list(PERCENTILES), "percentileSource": provider.key}
    if provider.key == PERCENTILE_SOURCE_COMPETITOR:
        groups = list_percentile_groups(db=db, price_format_code=price_format_code, percentile_source=provider.key)
        selected_group = _select_regular_group(
            groups=groups,
            api_identity=api_identity or group_id,
            competitor=competitor,
            source_key=source_key,
        )
        selected_source_key = str(selected_group.get("sourceKey") or "").strip() if selected_group else ""
        selected_competitor = str(selected_group.get("competitor") or "").strip() if selected_group else ""
        selected_api_identity = str((selected_group.get("apiIdentity") or f"regular:{selected_source_key}") if selected_source_key else "")
        if selected_source_key and selected_group is None:
            return {
                "items": [],
                "summary": _summary_for_percentile_rows([]),
                "total": 0,
                "page": page,
                "pageSize": page_size,
                "pageCount": 0,
                "groups": [],
                "priceColumns": [],
                "percentiles": list(PERCENTILES),
                "percentileSource": provider.key,
                "selectedApiIdentity": "",
            }
        pct_rows = (
            db.execute(
                select(RegularCompetitorPricePercentile, Product, ProductExtra)
                .join(Product, Product.id == RegularCompetitorPricePercentile.product_id)
                .outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
                .where(RegularCompetitorPricePercentile.competitor_identity == selected_source_key)
                .order_by(Product.code.asc(), RegularCompetitorPricePercentile.percentile.asc())
            )
            .all()
            if selected_source_key
            else []
        )
        by_product: dict[int, dict] = {}
        for pct_row, product, extra in pct_rows:
            item = by_product.setdefault(
                int(product.id),
                {
                    "productId": int(product.id),
                    "sku": product.code or "",
                    "name": product.name or "",
                    "manufacturer": getattr(extra, "manufacturer", "") if extra is not None else "",
                    "percentiles": {},
                    "competitorCount": int(pct_row.source_count or 0),
                    "sampleCount": int(pct_row.sample_count or 0),
                    "minPrice": _as_float(pct_row.min_price),
                    "maxPrice": _as_float(pct_row.max_price),
                    "status": "calculated" if pct_row.value is not None else "no_data",
                },
            )
            item["percentiles"][str(int(pct_row.percentile))] = _as_float(pct_row.value)
        items = list(by_product.values())
        total = len(items)
        start = (max(1, page) - 1) * max(1, page_size)
        end = start + max(1, page_size)
        visible = items[start:end]
        summary = {
            "totalProducts": total,
            "productsWithPercentile": total,
            "productsWithoutPercentile": 0,
            "productsWithCompetitors": total,
            "productsWithoutCompetitors": 0,
            "coveragePercent": 100 if total else 0,
        }
        return {
            "items": visible,
            "summary": summary,
            "total": total,
            "page": page,
            "pageSize": page_size,
            "pageCount": (total + max(1, page_size) - 1) // max(1, page_size),
            "groups": groups,
            "priceColumns": [],
            "percentiles": list(PERCENTILES),
            "percentileSource": provider.key,
            "selectedRegion": "",
            "selectedCompetitor": selected_competitor,
            "selectedSourceKey": selected_source_key,
            "selectedApiIdentity": selected_api_identity,
        }
    selected_region, selected_competitor, selected_source_key = _selected_group(
        db,
        pf,
        region=region if provider.regional else "",
        competitor=competitor,
        source_key=source_key,
        percentile_source=provider.key,
    )
    groups = list_percentile_groups(db=db, price_format_code=price_format_code, percentile_source=provider.key)
    available_percentiles = _available_percentiles_for_group(
        db=db,
        pf=pf,
        region=selected_region,
        competitor=selected_competitor,
        source_key=selected_source_key,
        percentile_source=provider.key,
    )
    percentile_agg = _percentile_browser_aggregate_subquery(
        pf=pf,
        provider=provider,
        region=selected_region,
        competitor=selected_competitor,
        source_key=selected_source_key,
    )
    page = max(1, page)
    page_size = max(1, page_size)
    product_filters = []
    query = q.strip()
    if query:
        like = f"%{query}%"
        product_filters.append((Product.code.ilike(like)) | (Product.name.ilike(like)))
    if percentile_filter == "has_percentile":
        product_filters.append(percentile_agg.c.has_percentile > 0)
    elif percentile_filter == "no_percentile":
        product_filters.append((percentile_agg.c.has_percentile.is_(None)) | (percentile_agg.c.has_percentile <= 0))
    if competitor_filter == "has_competitors":
        product_filters.append(percentile_agg.c.has_competitors > 0)
    elif competitor_filter == "no_competitors":
        product_filters.append((percentile_agg.c.has_competitors.is_(None)) | (percentile_agg.c.has_competitors <= 0))

    count_stmt = (
        select(func.count(func.distinct(Product.id)))
        .select_from(Product)
        .outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
        .outerjoin(percentile_agg, percentile_agg.c.product_id == Product.id)
    )
    product_stmt = (
        select(Product, ProductExtra)
        .outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
        .outerjoin(percentile_agg, percentile_agg.c.product_id == Product.id)
    )
    for condition in product_filters:
        count_stmt = count_stmt.where(condition)
        product_stmt = product_stmt.where(condition)

    sort_column = {
        "sku": Product.code,
        "name": Product.name,
        "percentile": percentile_agg.c.first_percentile_value,
        "competitor_count": percentile_agg.c.competitor_count,
        "status": percentile_agg.c.has_percentile,
    }.get(sort, Product.code)
    if direction == "desc":
        product_stmt = product_stmt.order_by(sort_column.desc().nullslast(), Product.code.asc())
    else:
        product_stmt = product_stmt.order_by(sort_column.asc().nullslast(), Product.code.asc())
    total = int(db.scalar(count_stmt) or 0)
    product_rows = db.execute(product_stmt.limit(page_size).offset((page - 1) * page_size)).all()
    items, price_columns = _build_percentile_browser_rows(
        db=db,
        pf=pf,
        region=selected_region,
        competitor=selected_competitor,
        source_key=selected_source_key,
        percentile_numbers=available_percentiles,
        percentile_source=provider.key,
        product_rows=product_rows,
    )
    summary = _percentile_browser_summary(db, percentile_agg)
    return {
        "items": items,
        "summary": summary,
        "total": total,
        "page": page,
        "pageSize": page_size,
        "pageCount": (total + page_size - 1) // page_size if total else 0,
        "percentiles": available_percentiles,
        "groups": groups,
        "selectedRegion": selected_region,
        "selectedCompetitor": selected_competitor,
        "selectedSourceKey": selected_source_key,
        "selectedApiIdentity": "",
        "percentileSource": provider.key,
        "percentileSourceLabel": provider.label,
        "requiresCompetitor": provider.requires_competitor,
        "regional": provider.regional,
        "priceColumns": price_columns,
    }


def export_percentile_product_rows(
    *,
    db: Session,
    price_format_code: str,
    fmt: str,
    region: str = "",
    competitor: str = "",
    source_key: str = "",
    api_identity: str = "",
    group_id: str = "",
    q: str = "",
    percentile_filter: str = "all",
    competitor_filter: str = "all",
    sort: str = "sku",
    direction: str = "asc",
    percentile_source: str = PERCENTILE_SOURCE_DEFAULT,
) -> tuple[str, bytes, str]:
    payload = list_percentile_product_rows(
        db=db,
        price_format_code=price_format_code,
        region=region,
        competitor=competitor,
        source_key=source_key,
        api_identity=api_identity,
        group_id=group_id,
        q=q,
        percentile_filter=percentile_filter,
        competitor_filter=competitor_filter,
        sort=sort,
        direction=direction,
        percentile_source=percentile_source,
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
    export_percentiles = [int(item) for item in (payload.get("percentiles") or list(PERCENTILES))]
    export_columns.extend((f"percentile:{percentile}", f"Персентиль {percentile}_{selected_competitor}") for percentile in export_percentiles)
    export_columns.extend(
        [
            ("competitorCount", "Количество цен конкурентов"),
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
        ws.title = "Персентили"
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
