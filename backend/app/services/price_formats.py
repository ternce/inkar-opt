from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.orm import Session

from ..models import (
    BendRange,
    BranchSapMapping,
    CompetitorPrice,
    CompetitorPriceList,
    CompetitorPricePercentile,
    CompetitorPricePercentileSourceSummary,
    CounterpartyPriceFormat,
    Job,
    MarkupRange,
    NoCompetitorMarkupRange,
    PriceFormat,
    PriceFormatBranchCounter,
    PriceFormatCompetitorAssignment,
    PriceFormatPercentilePreparation,
    PriceList,
    PricingWorkflowRun,
    ProvisorGoodsMap,
    SourceGoodsMatch,
    UniversalList,
    UniversalListPriceFormat,
    VidmanLogicalCompetitor,
)
from ..timezone import now_kz_naive
from .references.types import canonical_branch_id
from .regions import canonical_supported_city_name


PRICE_LIST_TYPES = {"ИПЛ", "ГПЛ"}

DEFAULT_BRANCH_SAP_MAPPINGS: tuple[tuple[str, str], ...] = (
    ("1001", "Алматы"),
    ("1002", "Астана"),
    ("1003", "Атырау"),
    ("1004", "Есик"),
    ("1005", "Караганда"),
    ("1006", "Костанай"),
    ("1007", "Семей"),
    ("1008", "Усть-Каменогорск"),
    ("1009", "Павлодар"),
    ("1010", "Актау"),
    ("1011", "Актобе"),
    ("1012", "Талдыкорган"),
    ("1013", "Шымкент"),
    ("1014", "Уральск"),
)


@dataclass(frozen=True)
class GeneratedPriceFormatCode:
    code: str
    price_list_type: str
    sap_branch_code: str
    sequence_number: int


def canonical_price_format_branch_key(branch: object) -> str:
    supported_name = canonical_supported_city_name(branch)
    value = supported_name or str(branch or "").strip()
    return canonical_branch_id(value)


def seed_default_sap_branch_mappings(db: Session) -> None:
    for sap_branch_code, branch_name in DEFAULT_BRANCH_SAP_MAPPINGS:
        key = canonical_price_format_branch_key(branch_name)
        if not key:
            continue
        row = db.get(BranchSapMapping, key)
        if row is None:
            db.add(
                BranchSapMapping(
                    canonical_branch_key=key,
                    branch_name=branch_name,
                    sap_branch_code=sap_branch_code,
                )
            )
            continue
        if row.branch_name != branch_name or row.sap_branch_code != sap_branch_code:
            row.branch_name = branch_name
            row.sap_branch_code = sap_branch_code
            row.updated_at = now_kz_naive()
    db.flush()


def resolve_sap_branch_mapping(db: Session, branch: object) -> BranchSapMapping:
    seed_default_sap_branch_mappings(db)
    key = canonical_price_format_branch_key(branch)
    row = db.get(BranchSapMapping, key) if key else None
    if row is None:
        raise ValueError("SAP branch mapping is not configured for selected branch")
    return row


def normalize_price_list_type(value: object) -> str:
    price_list_type = str(value or "").strip().upper()
    if price_list_type not in PRICE_LIST_TYPES:
        raise ValueError("priceListType must be one of: ИПЛ, ГПЛ")
    return price_list_type


def _ensure_price_format_branch_counter(db: Session, sap_branch_code: str) -> None:
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        db.execute(
            text(
                """
                INSERT INTO price_format_branch_counters (sap_branch_code, last_sequence, updated_at)
                VALUES (:sap_branch_code, 0, CURRENT_TIMESTAMP)
                ON CONFLICT (sap_branch_code) DO NOTHING
                """
            ),
            {"sap_branch_code": sap_branch_code},
        )
    elif dialect == "sqlite":
        db.execute(
            text(
                """
                INSERT OR IGNORE INTO price_format_branch_counters (sap_branch_code, last_sequence, updated_at)
                VALUES (:sap_branch_code, 0, CURRENT_TIMESTAMP)
                """
            ),
            {"sap_branch_code": sap_branch_code},
        )
    elif db.get(PriceFormatBranchCounter, sap_branch_code) is None:
        db.add(PriceFormatBranchCounter(sap_branch_code=sap_branch_code, last_sequence=0))
        db.flush()


def _max_sequence_from_generated_metadata(db: Session, sap_branch_code: str) -> int:
    return int(
        db.scalar(
            select(func.max(PriceFormat.sequence_number)).where(
                PriceFormat.sap_branch_code == sap_branch_code,
                PriceFormat.sequence_number.is_not(None),
            )
        )
        or 0
    )


def _max_sequence_from_generated_codes(db: Session, sap_branch_code: str) -> int:
    clauses = [
        PriceFormat.code.like(f"{price_list_type}_{sap_branch_code}_%")
        for price_list_type in PRICE_LIST_TYPES
    ]
    pattern = re.compile(
        rf"^(?:{'|'.join(re.escape(price_list_type) for price_list_type in PRICE_LIST_TYPES)})_"
        rf"{re.escape(sap_branch_code)}_([0-9]{{3,}})$"
    )
    max_sequence = 0
    for code in db.scalars(select(PriceFormat.code).where(or_(*clauses))):
        match = pattern.fullmatch(str(code or ""))
        if match:
            max_sequence = max(max_sequence, int(match.group(1)))
    return max_sequence


def _reconcile_price_format_branch_counter(db: Session, sap_branch_code: str) -> None:
    _ensure_price_format_branch_counter(db, sap_branch_code)
    counter = db.execute(
        select(PriceFormatBranchCounter)
        .where(PriceFormatBranchCounter.sap_branch_code == sap_branch_code)
        .with_for_update()
    ).scalar_one()
    existing_max = max(
        int(counter.last_sequence or 0),
        _max_sequence_from_generated_metadata(db, sap_branch_code),
        _max_sequence_from_generated_codes(db, sap_branch_code),
    )
    if int(counter.last_sequence or 0) < existing_max:
        counter.last_sequence = existing_max
        counter.updated_at = now_kz_naive()
        db.flush()


def allocate_price_format_code(db: Session, *, branch: str, price_list_type: object) -> GeneratedPriceFormatCode:
    normalized_type = normalize_price_list_type(price_list_type)
    mapping = resolve_sap_branch_mapping(db, branch)
    sap_branch_code = str(mapping.sap_branch_code)
    _reconcile_price_format_branch_counter(db, sap_branch_code)

    for _ in range(1000):
        if db.bind is not None and db.bind.dialect.name in {"postgresql", "sqlite"}:
            sequence = int(
                db.execute(
                    text(
                        """
                        UPDATE price_format_branch_counters
                        SET last_sequence = last_sequence + 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE sap_branch_code = :sap_branch_code
                        RETURNING last_sequence
                        """
                    ),
                    {"sap_branch_code": sap_branch_code},
                ).scalar_one()
            )
        else:
            counter = db.execute(
                select(PriceFormatBranchCounter)
                .where(PriceFormatBranchCounter.sap_branch_code == sap_branch_code)
                .with_for_update()
            ).scalar_one()
            counter.last_sequence = int(counter.last_sequence or 0) + 1
            counter.updated_at = now_kz_naive()
            sequence = int(counter.last_sequence)
        code = f"{normalized_type}_{sap_branch_code}_{sequence:03d}"
        exists = db.scalar(select(PriceFormat.id).where(PriceFormat.code == code).limit(1))
        if exists is None:
            return GeneratedPriceFormatCode(
                code=code,
                price_list_type=normalized_type,
                sap_branch_code=sap_branch_code,
                sequence_number=sequence,
            )

    raise ValueError("Could not allocate a unique price format code")


PRICE_FORMAT_BUSINESS_DEPENDENCIES: tuple[tuple[str, object], ...] = (
    ("price_lists", PriceList),
    ("pricing_workflow_runs", PricingWorkflowRun),
    ("counterparty_price_formats", CounterpartyPriceFormat),
)

PRICE_FORMAT_OWNED_CONFIGURATION_DEPENDENCIES: tuple[tuple[str, object], ...] = (
    ("markup_ranges", MarkupRange),
    ("bend_ranges", BendRange),
    ("no_competitor_markup_ranges", NoCompetitorMarkupRange),
    ("price_format_competitor_assignments", PriceFormatCompetitorAssignment),
    ("vidman_logical_competitors", VidmanLogicalCompetitor),
    ("provisor_goods_map", ProvisorGoodsMap),
    ("source_goods_matches", SourceGoodsMatch),
    ("universal_lists", UniversalList),
    ("universal_list_price_formats", UniversalListPriceFormat),
)

PRICE_FORMAT_TECHNICAL_DEPENDENCIES: tuple[tuple[str, object], ...] = (
    ("competitors_prices", CompetitorPrice),
    ("competitor_price_percentiles", CompetitorPricePercentile),
    ("competitor_price_percentile_source_summaries", CompetitorPricePercentileSourceSummary),
    ("jobs", Job),
    ("price_format_percentile_preparations", PriceFormatPercentilePreparation),
)

PRICE_FORMAT_COMPETITOR_SOURCE_DEPENDENCIES: tuple[tuple[str, object], ...] = (
    ("competitor_price_lists", CompetitorPriceList),
)

PRICE_FORMAT_DEPENDENCIES: tuple[tuple[str, object], ...] = (
    PRICE_FORMAT_BUSINESS_DEPENDENCIES
    + PRICE_FORMAT_COMPETITOR_SOURCE_DEPENDENCIES
    + PRICE_FORMAT_OWNED_CONFIGURATION_DEPENDENCIES
    + PRICE_FORMAT_TECHNICAL_DEPENDENCIES
)


def price_format_dependency_counts(db: Session, price_format_id: int) -> list[dict]:
    dependencies: list[dict] = []
    for table_name, model in PRICE_FORMAT_DEPENDENCIES:
        count = int(
            db.scalar(
                select(func.count())
                .select_from(model)
                .where(model.price_format_id == price_format_id)
            )
            or 0
        )
        if count:
            dependencies.append({"table": table_name, "count": count})
    return dependencies


def price_format_business_dependency_counts(db: Session, price_format_id: int) -> dict[str, int]:
    dependencies: dict[str, int] = {}
    for table_name, model in PRICE_FORMAT_BUSINESS_DEPENDENCIES:
        count = int(
            db.scalar(
                select(func.count())
                .select_from(model)
                .where(model.price_format_id == price_format_id)
            )
            or 0
        )
        if count:
            dependencies[table_name] = count
    return dependencies


def cleanup_price_format_owned_rows(db: Session, price_format_id: int) -> dict[str, int]:
    """Remove rows whose lifecycle belongs to the price format.

    Competitor price lists are intentionally preserved: they contain imported
    source data and may be reused globally. Only the legacy owner link is
    cleared.
    """

    deleted: dict[str, int] = {}

    delete_models: tuple[tuple[str, object], ...] = (
        ("competitor_price_percentile_source_summaries", CompetitorPricePercentileSourceSummary),
        ("competitor_price_percentiles", CompetitorPricePercentile),
        ("competitors_prices", CompetitorPrice),
        ("jobs", Job),
        ("price_format_percentile_preparations", PriceFormatPercentilePreparation),
        ("markup_ranges", MarkupRange),
        ("bend_ranges", BendRange),
        ("no_competitor_markup_ranges", NoCompetitorMarkupRange),
        ("price_format_competitor_assignments", PriceFormatCompetitorAssignment),
        ("provisor_goods_map", ProvisorGoodsMap),
        ("source_goods_matches", SourceGoodsMatch),
        ("universal_list_price_formats", UniversalListPriceFormat),
    )
    for table_name, model in delete_models:
        result = db.execute(delete(model).where(model.price_format_id == price_format_id))
        if result.rowcount:
            deleted[table_name] = int(result.rowcount)

    result = db.execute(
        update(UniversalList)
        .where(UniversalList.price_format_id == price_format_id)
        .values(price_format_id=None)
    )
    if result.rowcount:
        deleted["universal_lists_unlinked"] = int(result.rowcount)

    result = db.execute(
        update(VidmanLogicalCompetitor)
        .where(VidmanLogicalCompetitor.price_format_id == price_format_id)
        .values(price_format_id=None)
    )
    if result.rowcount:
        deleted["vidman_logical_competitors_unlinked"] = int(result.rowcount)

    result = db.execute(
        update(CompetitorPriceList)
        .where(CompetitorPriceList.price_format_id == price_format_id)
        .values(price_format_id=None, is_selected=False)
    )
    if result.rowcount:
        deleted["competitor_price_lists_unlinked"] = int(result.rowcount)

    return deleted
