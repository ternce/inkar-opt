from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    PriceFormat,
    PriceSourceAccount,
    VidmanCanonicalProduct,
    VidmanImportPage,
    VidmanImportRun,
    VidmanLogicalCompetitor,
    VidmanLogicalCompetitorSource,
    VidmanPriceList,
    VidmanProductMatch,
    VidmanRawCanonicalLink,
    VidmanRawItem,
)
from .price_source_accounts import credentials_from_row
from .vidman_competitor_price_lists import (
    VidmanBuildSummary,
    build_vidman_competitor_price_list,
    ensure_vidman_competitor_price_list_source,
)
from .vidman_logical_competitors import selected_logical_sources
from .vidman_normalization import VidmanNormalizationSummary, process_vidman_stage2
from .vidman_product_matching import VidmanMatchSummary, process_vidman_product_matches
from .vidman_raw_collector import VidmanCollectionSummary, VidmanCollectorConfig, VidmanRawCollector


STATUS_PUBLISHED = "PUBLISHED"
STATUS_DRY_RUN_READY = "DRY_RUN_READY"
STATUS_MAPPING_REQUIRED = "MAPPING_REQUIRED"
STATUS_FALLBACK_NOT_SELECTED = "FALLBACK_NOT_SELECTED"
STATUS_NO_TRUSTED_PRODUCTS = "NO_TRUSTED_PRODUCTS"
STATUS_FAILED_PRESERVED_PREVIOUS = "FAILED_PRESERVED_PREVIOUS"


@dataclass
class VidmanPlkRefreshResult:
    account_id: int
    main_id: int
    name: str
    status: str
    import_run_id: int | None = None
    logical_competitor_id: int | None = None
    logical_competitor_name: str = ""
    role: str = ""
    price_format_code: str = ""
    competitor_price_list_id: int | None = None
    rows_written: int = 0
    rows_to_publish: int = 0
    trusted_match_rows: int = 0
    preserved_previous_snapshot: bool = True
    skipped_reason: str = ""
    error: str = ""
    collection: VidmanCollectionSummary | None = None
    normalization: VidmanNormalizationSummary | None = None
    matching: VidmanMatchSummary | None = None
    build: VidmanBuildSummary | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "main_id": self.main_id,
            "name": self.name,
            "status": self.status,
            "import_run_id": self.import_run_id,
            "logical_competitor_id": self.logical_competitor_id,
            "logical_competitor_name": self.logical_competitor_name,
            "role": self.role,
            "price_format_code": self.price_format_code,
            "competitor_price_list_id": self.competitor_price_list_id,
            "rows_written": self.rows_written,
            "rows_to_publish": self.rows_to_publish,
            "trusted_match_rows": self.trusted_match_rows,
            "preserved_previous_snapshot": self.preserved_previous_snapshot,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
            "collection": self.collection.__dict__ if self.collection is not None else None,
            "normalization": self.normalization.__dict__ if self.normalization is not None else None,
            "matching": self.matching.analytics() if self.matching is not None else None,
            "build": self.build.to_dict() if self.build is not None else None,
        }


@dataclass
class VidmanAccountRefreshResult:
    price_source_account_id: int
    vidman_account_id: int | None
    login: str
    status: str
    dry_run: bool
    discovered_plks: int = 0
    processed_plks: int = 0
    published_plks: int = 0
    preserved_plks: int = 0
    failed_plks: int = 0
    elapsed_seconds: float = 0
    results: list[VidmanPlkRefreshResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "price_source_account_id": self.price_source_account_id,
            "vidman_account_id": self.vidman_account_id,
            "login": self.login,
            "status": self.status,
            "dry_run": self.dry_run,
            "discovered_plks": self.discovered_plks,
            "processed_plks": self.processed_plks,
            "published_plks": self.published_plks,
            "preserved_plks": self.preserved_plks,
            "failed_plks": self.failed_plks,
            "elapsed_seconds": self.elapsed_seconds,
            "errors": list(self.errors),
            "results": [row.to_dict() for row in self.results],
        }


def _collector_config(config: dict[str, Any], *, only_main_id: int | None = None) -> VidmanCollectorConfig:
    return VidmanCollectorConfig(
        delay_seconds=float(config.get("delaySeconds") or config.get("delay_seconds") or 0.25),
        retry_count=int(config.get("retryCount") or config.get("retry_count") or 3),
        timeout=float(config.get("timeout") or 60),
        auth_mode=str(config.get("authMode") or config.get("auth_mode") or "auto"),
        only_main_id=only_main_id,
        max_pages=int(config["maxPages"]) if config.get("maxPages") else None,
    )


def _latest_successful_complete_run_id(*, db: Session, account_id: int, main_id: int) -> int | None:
    rows = db.execute(
        select(VidmanImportRun.id, VidmanRawItem.price_list_id)
        .join(VidmanRawItem, VidmanRawItem.import_run_id == VidmanImportRun.id)
        .where(VidmanImportRun.account_id == account_id)
        .where(VidmanImportRun.status == "success")
        .where(VidmanRawItem.account_id == account_id)
        .where(VidmanRawItem.main_id == main_id)
        .group_by(VidmanImportRun.id, VidmanRawItem.price_list_id)
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


def _logical_sources(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        select(VidmanLogicalCompetitorSource, VidmanLogicalCompetitor, PriceFormat)
        .join(VidmanLogicalCompetitor, VidmanLogicalCompetitor.id == VidmanLogicalCompetitorSource.logical_competitor_id)
        .outerjoin(PriceFormat, PriceFormat.id == VidmanLogicalCompetitor.price_format_id)
        .where(VidmanLogicalCompetitorSource.active.is_(True))
        .where(VidmanLogicalCompetitor.active.is_(True))
        .order_by(VidmanLogicalCompetitorSource.priority.asc(), VidmanLogicalCompetitorSource.id.asc())
    ).all()
    return [
        {
            "id": int(source.id),
            "logical_competitor_id": int(logical.id),
            "logical_name": logical.name,
            "region": logical.region,
            "price_format_id": logical.price_format_id,
            "price_format_code": pf.code if pf is not None else "",
            "account_id": int(source.account_id),
            "main_id": int(source.main_id),
            "role": source.role,
            "priority": int(source.priority or 0),
            "active": bool(source.active),
        }
        for source, logical, pf in rows
    ]


def _current_logical_context(
    *,
    sources: list[dict[str, Any]],
    selected: dict[int, dict[str, Any]],
    account_id: int,
    main_id: int,
) -> tuple[dict[str, Any] | None, bool]:
    current = next(
        (
            row
            for row in sources
            if int(row["account_id"]) == int(account_id) and int(row["main_id"]) == int(main_id)
        ),
        None,
    )
    if current is None:
        return None, False
    selected_row = selected.get(int(current["logical_competitor_id"]))
    is_selected = bool(
        selected_row is not None
        and int(selected_row["account_id"]) == int(account_id)
        and int(selected_row["main_id"]) == int(main_id)
    )
    return current, is_selected


def _canonical_ids_for_run(*, db: Session, import_run_id: int, account_id: int, main_id: int) -> list[int]:
    rows = db.execute(
        select(VidmanRawCanonicalLink.canonical_product_id)
        .join(VidmanRawItem, VidmanRawItem.id == VidmanRawCanonicalLink.raw_item_id)
        .where(VidmanRawItem.import_run_id == import_run_id)
        .where(VidmanRawItem.account_id == account_id)
        .where(VidmanRawItem.main_id == main_id)
        .group_by(VidmanRawCanonicalLink.canonical_product_id)
        .order_by(VidmanRawCanonicalLink.canonical_product_id.asc())
    ).all()
    return [int(row[0]) for row in rows]


def _unmatched_canonical_ids(db: Session, canonical_ids: list[int]) -> list[int]:
    if not canonical_ids:
        return []
    matched = {
        int(row[0])
        for row in db.execute(
            select(VidmanProductMatch.canonical_product_id).where(
                VidmanProductMatch.canonical_product_id.in_(canonical_ids)
            )
        ).all()
    }
    existing = {
        int(row[0])
        for row in db.execute(select(VidmanCanonicalProduct.id).where(VidmanCanonicalProduct.id.in_(canonical_ids))).all()
    }
    return sorted(existing - matched)


async def refresh_vidman_account_stage43(
    *,
    db: Session,
    price_source_account_id: int,
    price_format_code: str = "",
    apply: bool = True,
    max_plks: int | None = None,
    only_main_ids: list[int] | None = None,
) -> VidmanAccountRefreshResult:
    started = time.monotonic()
    source_account = db.get(PriceSourceAccount, price_source_account_id)
    if source_account is None:
        raise ValueError("price source account not found")
    if source_account.source_type != "vidman":
        raise ValueError("price source account is not Vidman")
    if not source_account.is_active:
        raise ValueError("price source account is inactive")

    credentials = credentials_from_row(source_account)
    result = VidmanAccountRefreshResult(
        price_source_account_id=int(source_account.id),
        vidman_account_id=None,
        login=source_account.login,
        status="running",
        dry_run=not apply,
    )
    account_label = str(credentials.config.get("name") or credentials.config.get("title") or source_account.login)
    discovery_collector = VidmanRawCollector(
        db=db,
        login=credentials.login,
        password=credentials.password,
        account_name=account_label,
        config=_collector_config(credentials.config),
    )
    account, discovered = await discovery_collector.discover_price_lists()
    result.vidman_account_id = int(account.id)
    only_main_id_set = {int(item) for item in (only_main_ids or [])}
    if only_main_id_set:
        discovered = [pl for pl in discovered if int(pl.main_id) in only_main_id_set]
    if max_plks is not None:
        discovered = discovered[: max(0, int(max_plks))]
    result.discovered_plks = len(discovered)

    for price_list in discovered:
        plk_result = await _refresh_single_plk(
            db=db,
            credentials=credentials,
            account_id=int(account.id),
            account_label=account_label,
            price_list=price_list,
            apply=apply,
        )
        result.results.append(plk_result)

    result.processed_plks = len(result.results)
    result.published_plks = sum(1 for row in result.results if row.status == STATUS_PUBLISHED)
    result.failed_plks = sum(1 for row in result.results if row.status == STATUS_FAILED_PRESERVED_PREVIOUS)
    result.preserved_plks = sum(1 for row in result.results if row.preserved_previous_snapshot)
    result.status = "success" if not result.errors else "partial"
    result.elapsed_seconds = time.monotonic() - started

    source_account.price_lists_count = result.discovered_plks
    source_account.status = "connected" if result.status in {"success", "partial"} else source_account.status
    db.commit()
    return result


async def _refresh_single_plk(
    *,
    db: Session,
    credentials,
    account_id: int,
    account_label: str,
    price_list: VidmanPriceList,
    apply: bool,
) -> VidmanPlkRefreshResult:
    main_id = int(price_list.main_id)
    result = VidmanPlkRefreshResult(account_id=account_id, main_id=main_id, name=price_list.name, status="running")
    try:
        collector = VidmanRawCollector(
            db=db,
            login=credentials.login,
            password=credentials.password,
            account_name=account_label,
            config=_collector_config(credentials.config, only_main_id=main_id),
        )
        collection = await collector.collect()
        result.collection = collection
        result.import_run_id = int(collection.import_run_id)
        if collection.status != "success":
            result.status = STATUS_FAILED_PRESERVED_PREVIOUS
            result.skipped_reason = collection.status
            return result

        normalization = process_vidman_stage2(db, account_id=account_id, main_id=main_id, only_unprocessed=True)
        result.normalization = normalization

        match_summary = VidmanMatchSummary()
        for canonical_id in _unmatched_canonical_ids(
            db,
            _canonical_ids_for_run(db=db, import_run_id=int(collection.import_run_id), account_id=account_id, main_id=main_id),
        ):
            one = process_vidman_product_matches(db, canonical_id=canonical_id, only_unmatched=True, apply=True)
            match_summary.processed += one.processed
            match_summary.written_matches += one.written_matches
            match_summary.written_candidates += one.written_candidates
            match_summary.stable_id_match += one.stable_id_match
            match_summary.exact_structural_match += one.exact_structural_match
            match_summary.exact_name_structural_match += one.exact_name_structural_match
            match_summary.review_required += one.review_required
            match_summary.unmatched += one.unmatched
        result.matching = match_summary

        logical_sources = _logical_sources(db)
        snapshot_ready = {
            (int(row["account_id"]), int(row["main_id"])): _latest_successful_complete_run_id(
                db=db,
                account_id=int(row["account_id"]),
                main_id=int(row["main_id"]),
            )
            is not None
            for row in logical_sources
        }
        selected = selected_logical_sources(sources=logical_sources, snapshot_ready=snapshot_ready)
        logical_context, is_selected = _current_logical_context(
            sources=logical_sources,
            selected=selected,
            account_id=account_id,
            main_id=main_id,
        )
        if logical_context is None:
            result.status = STATUS_MAPPING_REQUIRED
            result.skipped_reason = "missing_logical_competitor_mapping"
            return result
        result.logical_competitor_id = int(logical_context["logical_competitor_id"])
        result.logical_competitor_name = str(logical_context["logical_name"] or "")
        result.role = str(logical_context["role"] or "")
        result.price_format_code = str(logical_context["price_format_code"] or "")
        if not is_selected:
            result.status = STATUS_FALLBACK_NOT_SELECTED
            result.skipped_reason = "logical_source_not_selected"
            return result
        if not result.price_format_code:
            result.status = STATUS_MAPPING_REQUIRED
            result.skipped_reason = "logical_competitor_missing_price_format"
            return result

        ensure_vidman_competitor_price_list_source(
            db=db,
            account_id=account_id,
            main_id=main_id,
            price_format_code=result.price_format_code,
            is_active=True,
            region=str(logical_context["region"] or ""),
            branch_id=str(logical_context["region"] or ""),
            branch_code=str(logical_context["region"] or ""),
            branch_name=str(logical_context["region"] or ""),
            competitor_name=result.logical_competitor_name,
        )
        build = build_vidman_competitor_price_list(
            db=db,
            account_id=account_id,
            main_id=main_id,
            price_format_code=result.price_format_code,
            import_run_id=int(collection.import_run_id),
            apply=apply,
            require_active=True,
        )
        result.build = build
        result.competitor_price_list_id = build.competitor_price_list_id
        result.rows_written = build.rows_written
        result.rows_to_publish = build.rows_to_publish
        result.trusted_match_rows = build.trusted_match_rows
        result.preserved_previous_snapshot = build.preserved_previous_snapshot
        result.skipped_reason = build.skipped_reason
        if build.skipped_reason:
            result.status = STATUS_NO_TRUSTED_PRODUCTS
        else:
            result.status = STATUS_PUBLISHED if apply else STATUS_DRY_RUN_READY
            result.preserved_previous_snapshot = False
        return result
    except Exception as exc:
        db.rollback()
        result.status = STATUS_FAILED_PRESERVED_PREVIOUS
        result.error = str(exc)
        result.skipped_reason = "exception"
        return result
