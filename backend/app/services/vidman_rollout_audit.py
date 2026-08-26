from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection


TRUSTED_STATUSES = frozenset({"AUTO_MATCHED", "MANUALLY_APPROVED"})
COVERAGE_HIGH = Decimal("70")
COVERAGE_MEDIUM = Decimal("50")
COVERAGE_LOW = Decimal("25")


@dataclass(frozen=True)
class SourceMetrics:
    account_id: int
    account_login: str
    account_name: str
    main_id: int
    plk_name: str
    total_raw_rows: int
    import_run_count: int
    latest_run_id: int | None
    latest_run_status: str
    latest_successful_run_id: int | None
    latest_successful_at: str
    latest_successful_rows: int
    snapshot_status: str
    canonical_linked: int = 0
    canonical_unlinked: int = 0
    auto_matched_rows: int = 0
    manually_approved_rows: int = 0
    trusted_mapped_rows: int = 0
    review_skipped: int = 0
    unmatched_skipped: int = 0
    valid_price_rows: int = 0
    invalid_price_rows: int = 0
    unique_internal_products: int = 0
    duplicate_product_groups: int = 0
    safe_deduplicated: int = 0
    price_conflict_groups: int = 0
    excluded_price_conflicts: int = 0
    publishable_rows: int = 0
    raw_to_publishable_percent: Decimal = Decimal("0")
    trusted_to_publishable_percent: Decimal = Decimal("0")
    region: str = ""
    format_code: str = ""
    logical_competitor: str = ""
    source_active: bool = False
    logical_competitor_id: int | None = None
    logical_source_role: str = ""
    logical_source_priority: int | None = None
    logical_source_selected: bool = False
    collision_status: str = "UNMAPPED"
    competitor_price_list_id: int | None = None
    current_stage4_status: str = "NOT_PUBLISHED"
    coverage_band: str = "INSUFFICIENT_COVERAGE"
    readiness: str = "NO_VALID_SNAPSHOT"
    readiness_reason: str = ""
    sample: list[dict[str, Any]] = field(default_factory=list)
    product_prices: dict[int, Decimal] = field(default_factory=dict, repr=False, compare=False)
    trusted_products: set[int] = field(default_factory=set, repr=False, compare=False)
    canonical_ids: set[int] = field(default_factory=set, repr=False, compare=False)


@dataclass(frozen=True)
class DuplicateCandidate:
    source_a: tuple[int, int]
    source_b: tuple[int, int]
    name_match: bool
    region_match: bool
    product_overlap_count: int
    product_overlap_percent_a: Decimal
    product_overlap_percent_b: Decimal
    price_comparable_products: int
    exact_price_percent: Decimal
    median_price_difference_percent: Decimal | None
    p90_price_difference_percent: Decimal | None
    classification: str
    price_consistency: str
    recommended_primary: tuple[int, int] | None
    recommended_fallback: tuple[int, int] | None


@dataclass(frozen=True)
class RolloutAuditReport:
    sources: list[SourceMetrics]
    duplicates: list[DuplicateCandidate]
    existing_collisions: list[dict[str, Any]]
    expected_impact: dict[str, Any]
    published_11870: dict[str, Any]
    before_counts: dict[str, int]
    after_counts: dict[str, int]


def normalize_plk_name(value: object) -> str:
    text_value = str(value or "").casefold().replace("ё", "е")
    text_value = re.sub(r"[^\w\s]+", " ", text_value, flags=re.UNICODE)
    text_value = re.sub(r"\s+", " ", text_value).strip()
    return text_value


def coverage_band(percent: Decimal) -> str:
    if percent >= COVERAGE_HIGH:
        return "HIGH_COVERAGE"
    if percent >= COVERAGE_MEDIUM:
        return "MEDIUM_COVERAGE"
    if percent >= COVERAGE_LOW:
        return "LOW_COVERAGE"
    return "INSUFFICIENT_COVERAGE"


def _pct(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return Decimal("0")
    return (Decimal(numerator) * Decimal("100") / Decimal(denominator)).quantize(Decimal("0.01"))


def _as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    text_value = str(value).strip().replace(" ", "").replace(",", ".")
    if not text_value:
        return None
    try:
        out = Decimal(text_value)
    except (InvalidOperation, ValueError):
        return None
    if not out.is_finite() or out <= 0:
        return None
    return out


def _snapshot_status(latest_status: str, latest_run_id: int | None, latest_success_id: int | None, bad_pages: int, rows: int) -> str:
    if latest_success_id is None:
        return "NO_SUCCESSFUL_SNAPSHOT"
    if bad_pages > 0:
        return "INCOMPLETE_SNAPSHOT"
    if rows <= 0:
        return "SUSPICIOUS_SNAPSHOT"
    if latest_run_id is not None and latest_run_id != latest_success_id and latest_status != "success":
        return "FAILED_LATEST_BUT_PREVIOUS_SUCCESS_AVAILABLE"
    return "READY_SNAPSHOT"


def classify_duplicate_candidate(
    *,
    name_match: bool,
    region_match: bool,
    overlap_a: Decimal,
    overlap_b: Decimal,
    exact_price_percent: Decimal,
    median_price_diff: Decimal | None,
) -> str:
    min_overlap = min(overlap_a, overlap_b)
    median_diff = median_price_diff if median_price_diff is not None else Decimal("999")
    if region_match and min_overlap >= Decimal("90") and exact_price_percent >= Decimal("95"):
        return "DEFINITE_SAME_PHYSICAL_PLK"
    if region_match and min_overlap >= Decimal("80") and median_diff <= Decimal("1"):
        return "VERY_LIKELY_SAME"
    if (name_match and region_match and min_overlap >= Decimal("50")) or (min_overlap >= Decimal("85") and median_diff <= Decimal("5")):
        return "POSSIBLE_DUPLICATE"
    if not region_match and min_overlap < Decimal("50"):
        return "LIKELY_DIFFERENT"
    return "LIKELY_DIFFERENT"


def price_consistency(exact_price_percent: Decimal, median_diff: Decimal | None, p90_diff: Decimal | None) -> str:
    median_value = median_diff if median_diff is not None else Decimal("999")
    p90_value = p90_diff if p90_diff is not None else Decimal("999")
    if exact_price_percent >= Decimal("95") or (median_value <= Decimal("0.1") and p90_value <= Decimal("0.5")):
        return "IDENTICAL_OR_NEAR_IDENTICAL"
    if median_value <= Decimal("2") and p90_value <= Decimal("5"):
        return "SMALL_EXPECTED_VARIATION"
    if median_value <= Decimal("10"):
        return "MATERIAL_VARIATION"
    return "INCONSISTENT_SOURCE"


def _percentile(values: list[Decimal], percentile: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    index = int(math.ceil((float(percentile) / 100.0) * len(ordered))) - 1
    return ordered[max(0, min(index, len(ordered) - 1))]


def _choose_primary(a: SourceMetrics, b: SourceMetrics) -> tuple[tuple[int, int], tuple[int, int]]:
    ranked = sorted(
        [a, b],
        key=lambda item: (
            item.competitor_price_list_id is not None,
            item.source_active,
            item.publishable_rows,
            item.latest_successful_run_id or 0,
        ),
        reverse=True,
    )
    return (ranked[0].account_id, ranked[0].main_id), (ranked[1].account_id, ranked[1].main_id)


def duplicate_candidates(sources: list[SourceMetrics]) -> list[DuplicateCandidate]:
    out: list[DuplicateCandidate] = []
    for index, left in enumerate(sources):
        for right in sources[index + 1 :]:
            if left.account_id == right.account_id and left.main_id == right.main_id:
                continue
            left_products = set(left.product_prices)
            right_products = set(right.product_prices)
            overlap = left_products & right_products
            if not overlap:
                continue
            overlap_a = _pct(len(overlap), len(left_products))
            overlap_b = _pct(len(overlap), len(right_products))
            comparable = 0
            exact = 0
            diffs: list[Decimal] = []
            for product_id in overlap:
                l_price = left.product_prices[product_id]
                r_price = right.product_prices[product_id]
                if l_price <= 0 or r_price <= 0:
                    continue
                comparable += 1
                if l_price == r_price:
                    exact += 1
                base = min(l_price, r_price)
                diffs.append((abs(l_price - r_price) * Decimal("100") / base).quantize(Decimal("0.01")))
            exact_pct = _pct(exact, comparable)
            median_diff = median(diffs) if diffs else None
            p90_diff = _percentile(diffs, Decimal("90"))
            name_match = normalize_plk_name(left.plk_name) == normalize_plk_name(right.plk_name)
            region_match = bool(left.region and right.region and normalize_plk_name(left.region) == normalize_plk_name(right.region))
            classification = classify_duplicate_candidate(
                name_match=name_match,
                region_match=region_match,
                overlap_a=overlap_a,
                overlap_b=overlap_b,
                exact_price_percent=exact_pct,
                median_price_diff=median_diff,
            )
            if classification in {"LIKELY_DIFFERENT", "DEFINITE_DIFFERENT"} and not name_match and min(overlap_a, overlap_b) < Decimal("70"):
                continue
            primary, fallback = _choose_primary(left, right)
            out.append(
                DuplicateCandidate(
                    source_a=(left.account_id, left.main_id),
                    source_b=(right.account_id, right.main_id),
                    name_match=name_match,
                    region_match=region_match,
                    product_overlap_count=len(overlap),
                    product_overlap_percent_a=overlap_a,
                    product_overlap_percent_b=overlap_b,
                    price_comparable_products=comparable,
                    exact_price_percent=exact_pct,
                    median_price_difference_percent=median_diff,
                    p90_price_difference_percent=p90_diff,
                    classification=classification,
                    price_consistency=price_consistency(exact_pct, median_diff, p90_diff),
                    recommended_primary=primary,
                    recommended_fallback=fallback,
                )
            )
    return sorted(out, key=lambda item: (item.classification, -item.product_overlap_count, item.source_a, item.source_b))


def _count_table(conn: Connection, table: str) -> int:
    return int(conn.execute(text(f"select count(*) from {table}")).scalar() or 0)


def _safe_counts(conn: Connection) -> dict[str, int]:
    return {
        "vidman_raw_items": _count_table(conn, "vidman_raw_items"),
        "vidman_canonical_products": _count_table(conn, "vidman_canonical_products"),
        "vidman_product_matches": _count_table(conn, "vidman_product_matches"),
        "competitor_price_lists": _count_table(conn, "competitor_price_lists"),
        "competitor_price_list_items": _count_table(conn, "competitor_price_list_items"),
        "list_719_items": int(
            conn.execute(text("select count(*) from competitor_price_list_items where price_list_id=719")).scalar() or 0
        ),
    }


def _source_rows(conn: Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            text(
                """
                with source_base as (
                    select ri.account_id, ri.main_id,
                           max(coalesce(a.login, '')) as account_login,
                           max(coalesce(a.display_name, '')) as account_name,
                           max(coalesce(pl.name, '')) as plk_name,
                           count(*) as total_raw_rows,
                           count(distinct ri.import_run_id) as import_run_count
                    from vidman_raw_items ri
                    join vidman_accounts a on a.id = ri.account_id
                    join vidman_price_lists pl on pl.id = ri.price_list_id
                    group by ri.account_id, ri.main_id
                ),
                run_rows as (
                    select ri.account_id, ri.main_id, ri.import_run_id,
                           max(r.status) as run_status,
                           max(coalesce(r.finished_at, r.started_at))::text as run_finished_at,
                           count(ri.id) as run_rows,
                           max(ri.price_list_id) as price_list_id
                    from vidman_raw_items ri
                    join vidman_import_runs r on r.id = ri.import_run_id
                    group by ri.account_id, ri.main_id, ri.import_run_id
                ),
                run_pages as (
                    select rr.account_id, rr.main_id, rr.import_run_id,
                           count(p.id) filter (where p.status != 'success') as bad_pages
                    from run_rows rr
                    left join vidman_import_pages p
                      on p.import_run_id = rr.import_run_id
                     and p.price_list_id = rr.price_list_id
                    group by rr.account_id, rr.main_id, rr.import_run_id
                ),
                latest_run as (
                    select distinct on (account_id, main_id)
                           account_id, main_id, import_run_id as latest_run_id, run_status as latest_run_status
                    from run_rows
                    order by account_id, main_id, import_run_id desc
                ),
                latest_success as (
                    select distinct on (rr.account_id, rr.main_id)
                           rr.account_id, rr.main_id, rr.import_run_id as latest_successful_run_id,
                           rr.run_finished_at as latest_successful_at,
                           rr.run_rows as latest_successful_rows,
                           coalesce(rp.bad_pages, 0) as bad_pages
                    from run_rows rr
                    left join run_pages rp
                      on rp.account_id = rr.account_id
                     and rp.main_id = rr.main_id
                     and rp.import_run_id = rr.import_run_id
                    where rr.run_status = 'success'
                    order by rr.account_id, rr.main_id, rr.import_run_id desc
                )
                select b.*, lr.latest_run_id, lr.latest_run_status,
                       ls.latest_successful_run_id, coalesce(ls.latest_successful_at, '') as latest_successful_at,
                       coalesce(ls.latest_successful_rows, 0) as latest_successful_rows,
                       coalesce(ls.bad_pages, 0) as bad_pages
                from source_base b
                left join latest_run lr on lr.account_id=b.account_id and lr.main_id=b.main_id
                left join latest_success ls on ls.account_id=b.account_id and ls.main_id=b.main_id
                order by b.account_id, b.main_id
                """
            )
        ).mappings()
    ]


def _mapping_rows(conn: Connection) -> dict[tuple[int, int], dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            select account_id, main_id, price_format_code, is_active, region, branch_id, branch_code,
                   branch_name, competitor_name, competitor_price_list_id
            from vidman_competitor_price_list_sources
            """
        )
    ).mappings()
    return {(int(row["account_id"]), int(row["main_id"])): dict(row) for row in rows}


def _logical_mapping_rows(conn: Connection) -> dict[tuple[int, int], dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            select s.account_id, s.main_id, s.logical_competitor_id, s.role, s.priority,
                   s.active as source_active, s.approved_manually,
                   lc.name as logical_name, lc.region, lc.price_format_id, pf.code as price_format_code,
                   lc.active as logical_active, lc.collision_status, lc.collision_notes
            from vidman_logical_competitor_sources s
            join vidman_logical_competitors lc on lc.id = s.logical_competitor_id
            left join price_formats pf on pf.id = lc.price_format_id
            """
        )
    ).mappings()
    return {(int(row["account_id"]), int(row["main_id"])): dict(row) for row in rows}


def _stage4_lists(conn: Connection) -> dict[tuple[int, int], dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            select id, account_id, external_price_list_id, source_key, price_format_id, items_count,
                   matched_positive_items_count, region, branch_name, competitor_name
            from competitor_price_lists
            where source_type='vidman'
            """
        )
    ).mappings()
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        account = str(row["account_id"] or "")
        main = str(row["external_price_list_id"] or "")
        if account.isdigit() and main.isdigit():
            out[(int(account), int(main))] = dict(row)
    return out


def _dry_run_metrics(conn: Connection, account_id: int, main_id: int, run_id: int, sample_limit: int) -> dict[str, Any]:
    rows = conn.execute(
        text(
            """
            select ri.id as raw_item_id, ri.raw_name, ri.raw_manufacturer, ri.raw_price_text, ri.price, ri.raw_stock,
                   l.canonical_product_id, m.status, m.product_id, p.name as product_name
            from vidman_raw_items ri
            left join vidman_raw_canonical_links l on l.raw_item_id = ri.id
            left join vidman_product_matches m on m.canonical_product_id = l.canonical_product_id
            left join products p on p.id = m.product_id
            where ri.account_id=:account_id and ri.main_id=:main_id and ri.import_run_id=:run_id
            order by ri.page_number, ri.row_number, ri.id
            """
        ),
        {"account_id": account_id, "main_id": main_id, "run_id": run_id},
    ).mappings()
    canonical_linked = 0
    canonical_unlinked = 0
    auto_rows = 0
    manual_rows = 0
    review_skipped = 0
    unmatched_skipped = 0
    invalid_prices = 0
    identity_rows: dict[tuple[int, str, str], list[tuple[Decimal, dict[str, Any]]]] = {}
    canonical_ids: set[int] = set()
    for row in rows:
        row_dict = dict(row)
        canonical_id = row_dict.get("canonical_product_id")
        if canonical_id is None:
            canonical_unlinked += 1
            continue
        canonical_linked += 1
        canonical_ids.add(int(canonical_id))
        status = str(row_dict.get("status") or "")
        if status == "AUTO_MATCHED":
            auto_rows += 1
        elif status == "MANUALLY_APPROVED":
            manual_rows += 1
        elif status == "REVIEW_REQUIRED":
            review_skipped += 1
            continue
        else:
            unmatched_skipped += 1
            continue
        product_id = row_dict.get("product_id")
        if product_id is None:
            unmatched_skipped += 1
            continue
        price = _as_decimal(row_dict.get("price")) or _as_decimal(row_dict.get("raw_price_text"))
        if price is None:
            invalid_prices += 1
            continue
        key = (
            int(product_id),
            normalize_plk_name(row_dict.get("raw_name")),
            normalize_plk_name(row_dict.get("raw_manufacturer")),
        )
        identity_rows.setdefault(key, []).append((price, row_dict))
    safe_deduped = 0
    conflict_groups = 0
    excluded_conflicts = 0
    publishable: list[tuple[Decimal, dict[str, Any]]] = []
    product_prices: dict[int, Decimal] = {}
    for values in identity_rows.values():
        unique_prices = {price for price, _row in values}
        if len(unique_prices) == 1:
            safe_deduped += max(0, len(values) - 1)
            price, row = values[0]
            publishable.append((price, row))
            product_prices.setdefault(int(row["product_id"]), price)
            continue
        conflict_groups += 1
        excluded_conflicts += len(values)
    sample: list[dict[str, Any]] = []
    for price, row in publishable[:sample_limit]:
        sample.append(
            {
                "product_id": int(row["product_id"]),
                "product_name": str(row.get("product_name") or ""),
                "raw_name": str(row.get("raw_name") or ""),
                "status": str(row.get("status") or ""),
                "price": str(price),
                "source": f"{account_id}/{main_id}",
                "validation": "CONFIRMED",
            }
        )
    trusted_rows = auto_rows + manual_rows
    return {
        "canonical_linked": canonical_linked,
        "canonical_unlinked": canonical_unlinked,
        "auto_matched_rows": auto_rows,
        "manually_approved_rows": manual_rows,
        "trusted_mapped_rows": trusted_rows,
        "review_skipped": review_skipped,
        "unmatched_skipped": unmatched_skipped,
        "valid_price_rows": sum(len(values) for values in identity_rows.values()),
        "invalid_price_rows": invalid_prices,
        "unique_internal_products": len(product_prices),
        "duplicate_product_groups": sum(1 for values in identity_rows.values() if len(values) > 1),
        "safe_deduplicated": safe_deduped,
        "price_conflict_groups": conflict_groups,
        "excluded_price_conflicts": excluded_conflicts,
        "publishable_rows": len(publishable),
        "product_prices": product_prices,
        "trusted_products": set(product_prices),
        "canonical_ids": canonical_ids,
        "sample": sample,
    }


def _readiness(source: SourceMetrics, duplicate_hold: bool, collision_hold: bool) -> tuple[str, str]:
    if source.snapshot_status not in {"READY_SNAPSHOT", "FAILED_LATEST_BUT_PREVIOUS_SUCCESS_AVAILABLE"}:
        return "NO_VALID_SNAPSHOT", source.snapshot_status
    if source.logical_competitor_id is None:
        return "READY_AFTER_LOGICAL_MAPPING", "missing logical competitor mapping"
    if not source.region:
        return "READY_AFTER_REGION_MAPPING", "missing explicit region"
    if not source.format_code:
        return "READY_AFTER_FORMAT_MAPPING", "missing explicit format"
    if not source.logical_competitor:
        return "READY_AFTER_LOGICAL_MAPPING", "missing logical competitor"
    if not source.logical_source_selected:
        return "DUPLICATE_SOURCE_DO_NOT_APPLY", "not selected primary/fallback for logical competitor"
    if duplicate_hold:
        return "DUPLICATE_SOURCE_DO_NOT_APPLY", "duplicate physical PLK candidate"
    if collision_hold:
        return "READY_AFTER_LOGICAL_MAPPING", "unresolved existing competitor collision"
    if source.publishable_rows <= 0:
        return "NEEDS_DATA_FIX", "no publishable rows"
    return "READY_FOR_APPLY", "explicit mapping and no duplicate hold"


def _existing_collisions(conn: Connection, sources: list[SourceMetrics]) -> list[dict[str, Any]]:
    existing = [
        dict(row)
        for row in conn.execute(
            text(
                """
                select id, source_type, source_key, display_name, supplier, branch_name, competitor_name, items_count
                from competitor_price_lists
                where source_type != 'vidman'
                """
            )
        ).mappings()
    ]
    out: list[dict[str, Any]] = []
    for source in sources:
        name = normalize_plk_name(source.logical_competitor or source.plk_name)
        if not name:
            continue
        for row in existing:
            haystack = normalize_plk_name(" ".join(str(row.get(key) or "") for key in ("display_name", "supplier", "competitor_name")))
            if not haystack:
                continue
            if name == haystack or name in haystack or haystack in name:
                out.append(
                    {
                        "source": (source.account_id, source.main_id),
                        "status": "POSSIBLE_COLLISION",
                        "existing_price_list_id": row["id"],
                        "existing_source_type": row["source_type"],
                        "existing_source_key": row["source_key"],
                        "existing_name": row["display_name"] or row["competitor_name"] or row["supplier"],
                    }
                )
    return out


def _published_11870(conn: Connection) -> dict[str, Any]:
    row = conn.execute(
        text(
            """
            select l.id, l.source_type, l.source_key, l.region, l.branch_name, l.competitor_name,
                   l.account_id, l.external_price_list_id, l.items_count, l.matched_positive_items_count,
                   pf.code as format_code,
                   count(i.id) filter (where i.distributor_price is not null and i.distributor_price > 0) as positive_items
            from competitor_price_lists l
            join price_formats pf on pf.id = l.price_format_id
            left join competitor_price_list_items i on i.price_list_id = l.id
            where l.id=719
            group by l.id, pf.code
            """
        )
    ).mappings().first()
    return dict(row) if row else {}


def build_rollout_audit(conn: Connection, *, sample_limit: int = 20) -> RolloutAuditReport:
    before = _safe_counts(conn)
    mappings = _mapping_rows(conn)
    logical_mappings = _logical_mapping_rows(conn)
    published = _stage4_lists(conn)
    sources: list[SourceMetrics] = []
    for row in _source_rows(conn):
        latest_success_id = row.get("latest_successful_run_id")
        status = _snapshot_status(
            str(row.get("latest_run_status") or ""),
            int(row["latest_run_id"]) if row.get("latest_run_id") is not None else None,
            int(latest_success_id) if latest_success_id is not None else None,
            int(row.get("bad_pages") or 0),
            int(row.get("latest_successful_rows") or 0),
        )
        metrics: dict[str, Any] = {}
        if latest_success_id is not None and status in {"READY_SNAPSHOT", "FAILED_LATEST_BUT_PREVIOUS_SUCCESS_AVAILABLE"}:
            metrics = _dry_run_metrics(conn, int(row["account_id"]), int(row["main_id"]), int(latest_success_id), sample_limit)
        source_key = (int(row["account_id"]), int(row["main_id"]))
        mapping = mappings.get(source_key, {})
        logical_mapping = logical_mappings.get(source_key, {})
        published_row = published.get((int(row["account_id"]), int(row["main_id"])), {})
        publishable = int(metrics.get("publishable_rows") or 0)
        raw_rows = int(row.get("latest_successful_rows") or 0)
        trusted = int(metrics.get("trusted_mapped_rows") or 0)
        raw_pct = _pct(publishable, raw_rows)
        source = SourceMetrics(
            account_id=int(row["account_id"]),
            account_login=str(row.get("account_login") or ""),
            account_name=str(row.get("account_name") or ""),
            main_id=int(row["main_id"]),
            plk_name=str(row.get("plk_name") or ""),
            total_raw_rows=int(row.get("total_raw_rows") or 0),
            import_run_count=int(row.get("import_run_count") or 0),
            latest_run_id=int(row["latest_run_id"]) if row.get("latest_run_id") is not None else None,
            latest_run_status=str(row.get("latest_run_status") or ""),
            latest_successful_run_id=int(latest_success_id) if latest_success_id is not None else None,
            latest_successful_at=str(row.get("latest_successful_at") or ""),
            latest_successful_rows=raw_rows,
            snapshot_status=status,
            canonical_linked=int(metrics.get("canonical_linked") or 0),
            canonical_unlinked=int(metrics.get("canonical_unlinked") or 0),
            auto_matched_rows=int(metrics.get("auto_matched_rows") or 0),
            manually_approved_rows=int(metrics.get("manually_approved_rows") or 0),
            trusted_mapped_rows=trusted,
            review_skipped=int(metrics.get("review_skipped") or 0),
            unmatched_skipped=int(metrics.get("unmatched_skipped") or 0),
            valid_price_rows=int(metrics.get("valid_price_rows") or 0),
            invalid_price_rows=int(metrics.get("invalid_price_rows") or 0),
            unique_internal_products=int(metrics.get("unique_internal_products") or 0),
            duplicate_product_groups=int(metrics.get("duplicate_product_groups") or 0),
            safe_deduplicated=int(metrics.get("safe_deduplicated") or 0),
            price_conflict_groups=int(metrics.get("price_conflict_groups") or 0),
            excluded_price_conflicts=int(metrics.get("excluded_price_conflicts") or 0),
            publishable_rows=publishable,
            raw_to_publishable_percent=raw_pct,
            trusted_to_publishable_percent=_pct(publishable, trusted),
            region=str(logical_mapping.get("region") or mapping.get("region") or published_row.get("region") or ""),
            format_code=str(logical_mapping.get("price_format_code") or mapping.get("price_format_code") or ""),
            logical_competitor=str(logical_mapping.get("logical_name") or mapping.get("competitor_name") or published_row.get("competitor_name") or ""),
            source_active=bool(logical_mapping.get("source_active") if logical_mapping else mapping.get("is_active") or False),
            logical_competitor_id=int(logical_mapping["logical_competitor_id"]) if logical_mapping.get("logical_competitor_id") is not None else None,
            logical_source_role=str(logical_mapping.get("role") or ""),
            logical_source_priority=int(logical_mapping["priority"]) if logical_mapping.get("priority") is not None else None,
            logical_source_selected=False,
            collision_status=str(logical_mapping.get("collision_status") or "UNMAPPED"),
            competitor_price_list_id=int(published_row["id"]) if published_row.get("id") is not None else None,
            current_stage4_status="PUBLISHED" if published_row else "NOT_PUBLISHED",
            coverage_band=coverage_band(raw_pct),
            sample=list(metrics.get("sample") or []),
            product_prices=dict(metrics.get("product_prices") or {}),
            trusted_products=set(metrics.get("trusted_products") or set()),
            canonical_ids=set(metrics.get("canonical_ids") or set()),
        )
        sources.append(source)
    snapshot_ready = {
        (source.account_id, source.main_id): source.snapshot_status in {"READY_SNAPSHOT", "FAILED_LATEST_BUT_PREVIOUS_SUCCESS_AVAILABLE"}
        for source in sources
    }
    logical_groups: dict[int, list[SourceMetrics]] = {}
    for source in sources:
        if source.logical_competitor_id is not None and source.source_active:
            logical_groups.setdefault(source.logical_competitor_id, []).append(source)
    selected_sources: set[tuple[int, int]] = set()
    for _logical_id, grouped_sources in logical_groups.items():
        primaries = sorted(
            [source for source in grouped_sources if source.logical_source_role == "PRIMARY"],
            key=lambda source: source.logical_source_priority or 0,
        )
        fallbacks = sorted(
            [source for source in grouped_sources if source.logical_source_role == "FALLBACK"],
            key=lambda source: source.logical_source_priority or 100,
        )
        primary = next((source for source in primaries if snapshot_ready.get((source.account_id, source.main_id), False)), None)
        if primary is not None:
            selected_sources.add((primary.account_id, primary.main_id))
            continue
        fallback = next((source for source in fallbacks if snapshot_ready.get((source.account_id, source.main_id), False)), None)
        if fallback is not None:
            selected_sources.add((fallback.account_id, fallback.main_id))
    if selected_sources:
        sources = [
            SourceMetrics(
                **{
                    **source.__dict__,
                    "logical_source_selected": (source.account_id, source.main_id) in selected_sources,
                }
            )
            for source in sources
        ]
    duplicates = duplicate_candidates(sources)
    duplicate_holds = {
        source
        for dup in duplicates
        if dup.classification in {"DEFINITE_SAME_PHYSICAL_PLK", "VERY_LIKELY_SAME", "POSSIBLE_DUPLICATE"}
        for source in [dup.source_a, dup.source_b]
        if source != dup.recommended_primary
        and (
            next((item.logical_competitor_id for item in sources if (item.account_id, item.main_id) == source), None)
            != next((item.logical_competitor_id for item in sources if (item.account_id, item.main_id) == dup.recommended_primary), None)
        )
    }
    collisions = _existing_collisions(conn, sources)
    collision_holds = {
        (source.account_id, source.main_id)
        for source in sources
        if source.collision_status in {"UNRESOLVED", "POSSIBLE_COLLISION"}
    }
    updated_sources: list[SourceMetrics] = []
    for source in sources:
        readiness, reason = _readiness(
            source,
            (source.account_id, source.main_id) in duplicate_holds,
            (source.account_id, source.main_id) in collision_holds,
        )
        updated_sources.append(SourceMetrics(**{**source.__dict__, "readiness": readiness, "readiness_reason": reason}))
    ready = [source for source in updated_sources if source.readiness == "READY_FOR_APPLY" and source.current_stage4_status != "PUBLISHED"]
    product_counts: dict[int, int] = {}
    for source in ready:
        for product_id in source.product_prices:
            product_counts[product_id] = product_counts.get(product_id, 0) + 1
    expected = {
        "CURRENT_VIDMAN_PUBLISHED_LISTS": int(
            conn.execute(text("select count(*) from competitor_price_lists where source_type='vidman' and items_count > 0")).scalar()
            or 0
        ),
        "CURRENT_VIDMAN_PRICE_ITEMS": int(
            conn.execute(
                text(
                    "select count(i.id) from competitor_price_list_items i join competitor_price_lists l on l.id=i.price_list_id where l.source_type='vidman'"
                )
            ).scalar()
            or 0
        ),
        "PLANNED_NEW_LISTS": len(ready),
        "PLANNED_NEW_PRICE_ITEMS": sum(source.publishable_rows for source in ready),
        "UNIQUE_PRODUCTS_GAINING_VIDMAN_PRICE": len(product_counts),
        "products_gaining_1_vidman_competitor": sum(1 for count in product_counts.values() if count == 1),
        "products_gaining_2_vidman_competitors": sum(1 for count in product_counts.values() if count == 2),
        "products_gaining_3plus_vidman_competitors": sum(1 for count in product_counts.values() if count >= 3),
    }
    after = _safe_counts(conn)
    return RolloutAuditReport(
        sources=updated_sources,
        duplicates=duplicates,
        existing_collisions=collisions,
        expected_impact=expected,
        published_11870=_published_11870(conn),
        before_counts=before,
        after_counts=after,
    )


def report_to_dict(report: RolloutAuditReport) -> dict[str, Any]:
    return {
        "sources": [{k: v for k, v in source.__dict__.items() if k not in {"product_prices", "trusted_products", "canonical_ids"}} for source in report.sources],
        "duplicates": [dup.__dict__ for dup in report.duplicates],
        "existing_collisions": report.existing_collisions,
        "expected_impact": report.expected_impact,
        "published_11870": report.published_11870,
        "before_counts": report.before_counts,
        "after_counts": report.after_counts,
    }
