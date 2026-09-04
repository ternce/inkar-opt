from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import (
    CompetitorPriceList,
    CompetitorPriceListItem,
    VidmanAccount,
    VidmanCanonicalProduct,
    VidmanCompetitorPriceListSource,
    VidmanImportPage,
    VidmanImportRun,
    VidmanPriceList,
    VidmanProductMatch,
    VidmanRawCanonicalLink,
    VidmanRawItem,
)
from .competitor_matching import rebuild_competitor_prices_for_selected
from .competitor_persist import _ensure_price_format
from .competitor_price_lists import _replace_legacy_price_rows_for_list, sync_selected_competitor_configs
from .competitor_assignments import selected_price_format_ids_for_competitor_price_list
from .competitor_read_models import refresh_price_list_item_counters
from .competitor_source_config import canonical_competitor_source_key
from .percentile_preparation import enqueue_percentile_preparation
from .vidman_product_matching import AUTO_MATCHED, MANUALLY_APPROVED


TRUSTED_VIDMAN_PRICE_STATUSES = frozenset({AUTO_MATCHED, MANUALLY_APPROVED})
VIDMAN_PRICE_SOURCE_TYPE = "vidman"


@dataclass(frozen=True)
class VidmanBuildMappingPreview:
    raw_item_id: int
    canonical_product_id: int
    product_id: int
    status: str
    raw_name: str
    raw_manufacturer: str
    raw_price: str
    price: Decimal
    stock: str


@dataclass
class VidmanBuildSummary:
    account_id: int
    main_id: int
    import_run_id: int | None
    price_format_code: str
    dry_run: bool
    source_active: bool
    source_key: str
    competitor_price_list_id: int | None = None
    total_snapshot_rows: int = 0
    trusted_match_rows: int = 0
    valid_price_rows: int = 0
    invalid_price_rows: int = 0
    unresolved_match_rows: int = 0
    duplicate_equivalent_rows: int = 0
    conflicting_duplicate_rows: int = 0
    rows_to_publish: int = 0
    rows_written: int = 0
    previous_items_count: int = 0
    previous_matched_positive_items_count: int = 0
    preserved_previous_snapshot: bool = False
    skipped_reason: str = ""
    warnings: list[str] = field(default_factory=list)
    preview: list[VidmanBuildMappingPreview] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "main_id": self.main_id,
            "import_run_id": self.import_run_id,
            "price_format_code": self.price_format_code,
            "dry_run": self.dry_run,
            "source_active": self.source_active,
            "source_key": self.source_key,
            "competitor_price_list_id": self.competitor_price_list_id,
            "total_snapshot_rows": self.total_snapshot_rows,
            "trusted_match_rows": self.trusted_match_rows,
            "valid_price_rows": self.valid_price_rows,
            "invalid_price_rows": self.invalid_price_rows,
            "unresolved_match_rows": self.unresolved_match_rows,
            "duplicate_equivalent_rows": self.duplicate_equivalent_rows,
            "conflicting_duplicate_rows": self.conflicting_duplicate_rows,
            "rows_to_publish": self.rows_to_publish,
            "rows_written": self.rows_written,
            "previous_items_count": self.previous_items_count,
            "previous_matched_positive_items_count": self.previous_matched_positive_items_count,
            "preserved_previous_snapshot": self.preserved_previous_snapshot,
            "skipped_reason": self.skipped_reason,
            "warnings": list(self.warnings),
            "preview": [
                {
                    "raw_item_id": row.raw_item_id,
                    "canonical_product_id": row.canonical_product_id,
                    "product_id": row.product_id,
                    "status": row.status,
                    "raw_name": row.raw_name,
                    "raw_manufacturer": row.raw_manufacturer,
                    "raw_price": row.raw_price,
                    "price": str(row.price),
                    "stock": row.stock,
                }
                for row in self.preview
            ],
        }


def vidman_competitor_source_key(account_id: object, main_id: object) -> str:
    account = str(account_id or "").strip()
    main = str(main_id or "").strip()
    return f"account:{account}:main:{main}" if account and main else ""


def _as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        dec = value
    else:
        text = str(value).strip()
        if not text:
            return None
        text = text.replace(" ", "").replace(",", ".")
        try:
            dec = Decimal(text)
        except (InvalidOperation, ValueError):
            return None
    if not dec.is_finite() or dec <= 0:
        return None
    return dec


def _finite_decimal_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return str(value)


def ensure_vidman_competitor_price_list_source(
    *,
    db: Session,
    account_id: int,
    main_id: int,
    price_format_code: str,
    is_active: bool = False,
    region: str = "",
    branch_id: str = "",
    branch_code: str = "",
    branch_name: str = "",
    competitor_name: str = "",
    price_coefficient: Decimal | float | str = Decimal("1"),
) -> VidmanCompetitorPriceListSource:
    source = db.execute(
        select(VidmanCompetitorPriceListSource)
        .where(VidmanCompetitorPriceListSource.account_id == account_id)
        .where(VidmanCompetitorPriceListSource.main_id == main_id)
        .where(VidmanCompetitorPriceListSource.price_format_code == price_format_code)
    ).scalar_one_or_none()
    if source is None:
        source = VidmanCompetitorPriceListSource(
            account_id=account_id,
            main_id=main_id,
            price_format_code=price_format_code,
        )
        db.add(source)
        db.flush()
    source.is_active = bool(is_active)
    source.region = str(region or "").strip()
    source.branch_id = str(branch_id or "").strip()
    source.branch_code = str(branch_code or branch_id or "").strip()
    source.branch_name = str(branch_name or "").strip()
    source.competitor_name = str(competitor_name or "").strip()
    source.price_coefficient = _as_decimal(price_coefficient) or Decimal("1")
    source.updated_at = datetime.utcnow()
    return source


def _get_source(
    *,
    db: Session,
    account_id: int,
    main_id: int,
    price_format_code: str,
    require_active: bool,
) -> VidmanCompetitorPriceListSource | None:
    source = db.execute(
        select(VidmanCompetitorPriceListSource)
        .where(VidmanCompetitorPriceListSource.account_id == account_id)
        .where(VidmanCompetitorPriceListSource.main_id == main_id)
        .where(VidmanCompetitorPriceListSource.price_format_code == price_format_code)
    ).scalar_one_or_none()
    if source is None or (require_active and not source.is_active):
        return None
    return source


def _latest_successful_run_id(*, db: Session, account_id: int, main_id: int) -> int | None:
    rows = db.execute(
        select(VidmanImportRun.id, VidmanPriceList.id)
        .join(VidmanRawItem, VidmanRawItem.import_run_id == VidmanImportRun.id)
        .join(VidmanPriceList, VidmanPriceList.id == VidmanRawItem.price_list_id)
        .where(VidmanImportRun.account_id == account_id)
        .where(VidmanImportRun.status == "success")
        .where(VidmanRawItem.account_id == account_id)
        .where(VidmanRawItem.main_id == main_id)
        .group_by(VidmanImportRun.id, VidmanPriceList.id)
        .order_by(VidmanImportRun.id.desc())
    ).all()
    for run_id, price_list_id in rows:
        failed_pages = int(
            db.scalar(
                select(func.count(VidmanImportPage.id))
                .where(VidmanImportPage.import_run_id == run_id)
                .where(VidmanImportPage.price_list_id == price_list_id)
                .where(VidmanImportPage.status != "success")
            )
            or 0
        )
        success_pages = int(
            db.scalar(
                select(func.count(VidmanImportPage.id))
                .where(VidmanImportPage.import_run_id == run_id)
                .where(VidmanImportPage.price_list_id == price_list_id)
                .where(VidmanImportPage.status == "success")
            )
            or 0
        )
        if failed_pages == 0 and success_pages > 0:
            return int(run_id)
    return None


def _validate_snapshot(*, db: Session, account_id: int, main_id: int, import_run_id: int) -> tuple[int | None, str]:
    run = db.get(VidmanImportRun, import_run_id)
    if run is None:
        return None, "import_run_not_found"
    if int(run.account_id) != int(account_id):
        return None, "import_run_account_mismatch"
    if run.status != "success":
        return None, "import_run_not_success"
    price_list_id = db.scalar(
        select(VidmanRawItem.price_list_id)
        .where(VidmanRawItem.import_run_id == import_run_id)
        .where(VidmanRawItem.account_id == account_id)
        .where(VidmanRawItem.main_id == main_id)
        .limit(1)
    )
    if price_list_id is None:
        return None, "snapshot_has_no_rows"
    non_success_pages = int(
        db.scalar(
            select(func.count(VidmanImportPage.id))
            .where(VidmanImportPage.import_run_id == import_run_id)
            .where(VidmanImportPage.price_list_id == price_list_id)
            .where(VidmanImportPage.status != "success")
        )
        or 0
    )
    if non_success_pages:
        return None, "snapshot_has_failed_or_incomplete_pages"
    return int(price_list_id), ""


def _list_metadata(*, db: Session, account_id: int, main_id: int) -> tuple[str, str]:
    account = db.get(VidmanAccount, account_id)
    price_list = db.execute(
        select(VidmanPriceList)
        .where(VidmanPriceList.account_id == account_id)
        .where(VidmanPriceList.main_id == main_id)
    ).scalar_one_or_none()
    account_login = str(account.login if account is not None else account_id)
    plk_name = str(price_list.name if price_list is not None else f"Vidman {main_id}")
    return account_login, plk_name


def build_vidman_competitor_price_list(
    *,
    db: Session,
    account_id: int,
    main_id: int,
    price_format_code: str,
    import_run_id: int | None = None,
    apply: bool = False,
    preview_limit: int = 50,
    require_active: bool = True,
) -> VidmanBuildSummary:
    source_key = vidman_competitor_source_key(account_id, main_id)
    summary = VidmanBuildSummary(
        account_id=account_id,
        main_id=main_id,
        import_run_id=import_run_id,
        price_format_code=price_format_code,
        dry_run=not apply,
        source_active=False,
        source_key=source_key,
    )
    source = _get_source(
        db=db,
        account_id=account_id,
        main_id=main_id,
        price_format_code=price_format_code,
        require_active=require_active,
    )
    if source is None:
        summary.skipped_reason = "inactive_or_unconfigured_vidman_plk_source"
        summary.preserved_previous_snapshot = True
        return summary
    summary.source_active = bool(source.is_active)
    if not source.region or not source.branch_name or not source.competitor_name:
        summary.skipped_reason = "missing_explicit_region_or_competitor_mapping"
        summary.preserved_previous_snapshot = True
        return summary

    if import_run_id is None:
        import_run_id = _latest_successful_run_id(db=db, account_id=account_id, main_id=main_id)
        summary.import_run_id = import_run_id
    if import_run_id is None:
        summary.skipped_reason = "no_successful_snapshot"
        summary.preserved_previous_snapshot = True
        return summary
    price_list_id, snapshot_error = _validate_snapshot(
        db=db,
        account_id=account_id,
        main_id=main_id,
        import_run_id=import_run_id,
    )
    if snapshot_error:
        summary.skipped_reason = snapshot_error
        summary.preserved_previous_snapshot = True
        return summary

    pf = _ensure_price_format(db, price_format_code)
    account_login, plk_name = _list_metadata(db=db, account_id=account_id, main_id=main_id)
    rows = db.execute(
        select(VidmanRawItem, VidmanRawCanonicalLink, VidmanCanonicalProduct, VidmanProductMatch)
        .join(VidmanRawCanonicalLink, VidmanRawCanonicalLink.raw_item_id == VidmanRawItem.id)
        .join(VidmanCanonicalProduct, VidmanCanonicalProduct.id == VidmanRawCanonicalLink.canonical_product_id)
        .join(VidmanProductMatch, VidmanProductMatch.canonical_product_id == VidmanCanonicalProduct.id)
        .where(VidmanRawItem.import_run_id == import_run_id)
        .where(VidmanRawItem.account_id == account_id)
        .where(VidmanRawItem.main_id == main_id)
        .order_by(VidmanRawItem.page_number.asc(), VidmanRawItem.row_number.asc(), VidmanRawItem.id.asc())
    ).all()
    summary.total_snapshot_rows = int(
        db.scalar(
            select(func.count(VidmanRawItem.id))
            .where(VidmanRawItem.import_run_id == import_run_id)
            .where(VidmanRawItem.account_id == account_id)
            .where(VidmanRawItem.main_id == main_id)
        )
        or 0
    )

    by_identity: dict[tuple[int, str, str, str], dict[str, Any]] = {}
    conflicted_keys: set[tuple[int, str, str, str]] = set()
    unresolved_canonicals = set()
    for raw, link, canonical, match in rows:
        if match.status not in TRUSTED_VIDMAN_PRICE_STATUSES or match.product_id is None:
            unresolved_canonicals.add(int(canonical.id))
            continue
        summary.trusted_match_rows += 1
        price = _as_decimal(raw.price)
        if price is None:
            price = _as_decimal(raw.raw_price_text)
        if price is None:
            summary.invalid_price_rows += 1
            continue
        summary.valid_price_rows += 1
        identity = (
            int(match.product_id),
            str(raw.raw_name or "").strip().casefold(),
            str(raw.raw_manufacturer or "").strip().casefold(),
        )
        existing = by_identity.get(identity)
        if existing is None:
            by_identity[identity] = {
                "raw": raw,
                "link": link,
                "canonical": canonical,
                "match": match,
                "price": price,
            }
            continue
        if existing["price"] == price:
            summary.duplicate_equivalent_rows += 1
            continue
        summary.conflicting_duplicate_rows += 1
        conflicted_keys.add(identity)
    summary.unresolved_match_rows = len(unresolved_canonicals)

    publish_rows = [row for key, row in by_identity.items() if key not in conflicted_keys]
    summary.rows_to_publish = len(publish_rows)
    if summary.conflicting_duplicate_rows:
        summary.warnings.append("conflicting_duplicate_rows_excluded")
    if summary.invalid_price_rows:
        summary.warnings.append("invalid_prices_skipped")
    if summary.unresolved_match_rows:
        summary.warnings.append("unresolved_matches_excluded")
    if not publish_rows:
        summary.skipped_reason = "no_rows_to_publish"
        summary.preserved_previous_snapshot = True
        return summary

    existing_list = db.execute(
        select(CompetitorPriceList)
        .where(CompetitorPriceList.source_type == VIDMAN_PRICE_SOURCE_TYPE)
        .where(CompetitorPriceList.source_key == source_key)
        .where(CompetitorPriceList.price_format_id == pf.id)
    ).scalar_one_or_none()
    if existing_list is not None:
        summary.competitor_price_list_id = int(existing_list.id)
        summary.previous_items_count = int(existing_list.items_count or 0)
        summary.previous_matched_positive_items_count = int(existing_list.matched_positive_items_count or 0)

    for row in publish_rows[: max(0, int(preview_limit))]:
        raw = row["raw"]
        match = row["match"]
        link = row["link"]
        summary.preview.append(
            VidmanBuildMappingPreview(
                raw_item_id=int(raw.id),
                canonical_product_id=int(link.canonical_product_id),
                product_id=int(match.product_id),
                status=str(match.status),
                raw_name=str(raw.raw_name or ""),
                raw_manufacturer=str(raw.raw_manufacturer or ""),
                raw_price=_finite_decimal_text(raw.raw_price_text or raw.price),
                price=row["price"],
                stock=str(raw.raw_stock or ""),
            )
        )
    if not apply:
        return summary

    price_list = existing_list
    if price_list is None:
        price_list = CompetitorPriceList(
            price_format_id=pf.id,
            source_type=VIDMAN_PRICE_SOURCE_TYPE,
            source_key=source_key,
            coefficient=Decimal("1"),
        )
        db.add(price_list)
        db.flush()
    summary.competitor_price_list_id = int(price_list.id)
    display = f"{source.branch_name} - {source.competitor_name} - {account_login}"
    price_list.source_key = source_key
    price_list.display_name = display
    price_list.supplier = source.competitor_name
    price_list.region = source.region
    price_list.branch_id = source.branch_id or source.branch_name
    price_list.branch_code = source.branch_code or source.branch_id or source.branch_name
    price_list.branch_name = source.branch_name
    price_list.competitor_name = source.competitor_name
    price_list.account_id = str(account_id)
    price_list.account_login = account_login
    price_list.external_price_list_id = str(main_id)
    price_list.sync_batch_id = f"vidman-run-{import_run_id}"
    price_list.source_updated_at = f"vidman_import_run_id:{import_run_id}"
    price_list.last_checked_at = datetime.utcnow()
    price_list.last_success_at = price_list.last_checked_at
    price_list.last_refresh_status = "updated"
    price_list.last_refresh_message = ""
    price_list.price_date = date.today()
    price_list.price_coefficient = source.price_coefficient
    price_list.updated_at = datetime.utcnow()

    db.execute(
        delete(CompetitorPriceListItem)
        .where(CompetitorPriceListItem.price_list_id == price_list.id)
        .execution_options(synchronize_session=False)
    )
    mappings: list[dict[str, Any]] = []
    for row in publish_rows:
        raw = row["raw"]
        match = row["match"]
        canonical = row["canonical"]
        link = row["link"]
        mappings.append(
            {
                "price_list_id": int(price_list.id),
                "product_id": int(match.product_id),
                "name": str(raw.raw_name or canonical.canonical_name or ""),
                "distributor_goods_name": str(raw.raw_name or canonical.canonical_name or ""),
                "distributor_goods_id": f"vidman:{account_id}:{main_id}:{raw.id}",
                "distributor_price": row["price"],
                "stock": _as_decimal(raw.stock),
                "package_count": _as_decimal(raw.pack_qty) or _as_decimal(raw.min_order),
                "expiry_date": str(raw.raw_expiry_text or raw.expiry_date or ""),
                "match_key": str(canonical.canonical_signature or ""),
                "match_type": f"vidman_{str(match.status).lower()}",
                "match_score": Decimal(str(match.confidence or 100)),
                "matched_sku": f"product:{int(match.product_id)}",
                "raw_name": str(raw.raw_name or ""),
                "raw_manufacturer": str(raw.raw_manufacturer or ""),
                "normalized_name": str(canonical.canonical_name or ""),
                "normalized_manufacturer": str(canonical.canonical_manufacturer or ""),
                "parsed_base_name": str(canonical.base_name or ""),
                "parsed_form": str(canonical.dosage_form or ""),
                "parsed_dosage": canonical.dosage_value,
                "parsed_quantity": canonical.pack_count,
                "parsed_volume": canonical.volume_value,
                "parsed_weight": canonical.weight_value,
                "parsed_concentration": canonical.concentration_value,
                "raw_json": json.dumps(
                    {
                        "source": "vidman",
                        "accountId": account_id,
                        "mainId": main_id,
                        "priceListId": price_list_id,
                        "priceListName": plk_name,
                        "importRunId": import_run_id,
                        "rawItemId": int(raw.id),
                        "canonicalProductId": int(link.canonical_product_id),
                        "matchStatus": match.status,
                        "stockRaw": raw.raw_stock,
                        "priceRaw": raw.raw_price_text,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            }
        )
    db.bulk_insert_mappings(CompetitorPriceListItem, mappings)
    db.flush()
    refresh_price_list_item_counters(db=db, price_list_ids=[int(price_list.id)])

    source.last_successful_import_run_id = int(import_run_id)
    source.competitor_price_list_id = int(price_list.id)
    source.updated_at = datetime.utcnow()
    summary.rows_written = len(mappings)

    _replace_legacy_price_rows_for_list(db=db, price_list=price_list)
    affected_price_format_ids = selected_price_format_ids_for_competitor_price_list(
        db=db,
        competitor_price_list_id=int(price_list.id),
    )
    for price_format_id in affected_price_format_ids:
        sync_selected_competitor_configs(db=db, price_format_id=price_format_id)
        rebuild_competitor_prices_for_selected(db=db, price_format_id=price_format_id)
        enqueue_percentile_preparation(db=db, price_format_id=price_format_id, reason="vidman_price_list_built")
    db.commit()
    canonical_competitor_source_key(price_list)
    return summary
