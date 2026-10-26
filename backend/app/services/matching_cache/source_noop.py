from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import (
    CompetitorMatchSnapshot,
    CompetitorMatchSnapshotItem,
    CompetitorPriceList,
    CompetitorPriceListItem,
    CompetitorSourcePublication,
    CompetitorSourcePublicationItem,
    CompetitorSourceVersion,
    CompetitorSourceVersionItem,
)
from .config import matching_cache_source_noop_enabled
from .context import build_source_dependency_vector
from .fingerprints import CanonicalSourceHasher, SOURCE_CONTENT_FIELDS, canonical_source_row


# Fields supplied by the provider/import parser. Matching and normalization
# fields are deliberately reused from the current physical row only after a
# dependency-valid snapshot has proved that those decisions remain valid.
PROVIDER_SOURCE_FIELDS = (
    "provisor_id",
    "provisor_goods_id",
    "filial_id",
    "name",
    "reg_number",
    "distributor_goods_name",
    "distributor_goods_id",
    "distributor_price",
    "stock",
    "package_count",
    "expiry_date",
    "raw_name",
    "raw_manufacturer",
    "raw_json",
)

SEMANTIC_SOURCE_METADATA_FIELDS = (
    "price_format_id",
    "source_type",
    "source_key",
    "display_name",
    "supplier",
    "region",
    "branch_id",
    "branch_code",
    "branch_name",
    "competitor_name",
    "account_id",
    "account_login",
    "external_price_list_id",
    "update_mode",
    "price_date",
    "coefficient",
    "price_coefficient",
    "is_selected",
)


@dataclass(frozen=True)
class SourceNoopDecision:
    reusable: bool
    reason: str
    row_count: int = 0
    content_sha256: str = ""
    source_version_id: int | None = None
    source_publication_id: int | None = None
    match_snapshot_id: int | None = None


def source_semantic_metadata(source: CompetitorPriceList) -> tuple[Any, ...]:
    return tuple(getattr(source, field, None) for field in SEMANTIC_SOURCE_METADATA_FIELDS)


def _mapping(value: Mapping[str, Any] | CompetitorPriceListItem) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {
        "product_id": value.product_id,
        "provisor_id": value.provisor_id,
        "provisor_goods_id": value.provisor_goods_id,
        "filial_id": value.filial_id,
        "name": value.name,
        "reg_number": value.reg_number,
        "distributor_goods_name": value.distributor_goods_name,
        "distributor_goods_id": value.distributor_goods_id,
        "distributor_price": value.distributor_price,
        "stock": value.stock,
        "package_count": value.package_count,
        "expiry_date": value.expiry_date,
        "match_key": value.match_key,
        "match_type": value.match_type,
        "match_score": value.match_score,
        "matched_sku": value.matched_sku,
        "raw_name": value.raw_name,
        "raw_manufacturer": value.raw_manufacturer,
        "normalized_name": value.normalized_name,
        "normalized_manufacturer": value.normalized_manufacturer,
        "parsed_base_name": value.parsed_base_name,
        "parsed_form": value.parsed_form,
        "parsed_forms_json": value.parsed_forms_json,
        "parsed_dosage": value.parsed_dosage,
        "parsed_dosage_volume": value.parsed_dosage_volume,
        "parsed_quantity": value.parsed_quantity,
        "parsed_volume": value.parsed_volume,
        "parsed_weight": value.parsed_weight,
        "parsed_percent_strength": value.parsed_percent_strength,
        "parsed_concentration": value.parsed_concentration,
        "parsed_iu_dosage": value.parsed_iu_dosage,
        "parsed_strength_signature": value.parsed_strength_signature,
        "parsed_dimensions_json": value.parsed_dimensions_json,
        "parsed_critical_tokens_json": value.parsed_critical_tokens_json,
        "raw_json": value.raw_json,
    }


def _reject(reason: str) -> SourceNoopDecision:
    return SourceNoopDecision(False, reason)


def validate_unchanged_source_import(
    *,
    db: Session,
    source: CompetitorPriceList,
    incoming_rows: Iterable[Mapping[str, Any] | CompetitorPriceListItem],
    semantic_metadata_before: tuple[Any, ...],
    authoritative_input: bool = False,
    require_snapshot: bool = True,
) -> SourceNoopDecision:
    """Fail-closed validation for skipping physical source replacement.

    The caller must already hold the source transaction/advisory lock. Incoming
    provider fields are overlaid on the current post-match row in physical order.
    The resulting *complete* canonical content must equal the published immutable
    version. This preserves duplicate multiplicity, source order, and all current
    matching decisions without trusting a partial provider fingerprint.
    """

    if not matching_cache_source_noop_enabled():
        return _reject("disabled")
    if db.get_bind().dialect.name != "postgresql":
        return _reject("unsupported_database")
    if source.id is None:
        return _reject("source_not_persisted")
    if source_semantic_metadata(source) != semantic_metadata_before:
        return _reject("semantic_source_metadata_changed")

    source_id = int(source.id)
    version = db.scalar(
        select(CompetitorSourceVersion)
        .where(
            CompetitorSourceVersion.competitor_price_list_id == source_id,
            CompetitorSourceVersion.status == "published",
        )
        .with_for_update()
    )
    if version is None:
        return _reject("published_source_version_absent")
    publication = db.scalar(
        select(CompetitorSourcePublication)
        .where(
            CompetitorSourcePublication.competitor_price_list_id == source_id,
            CompetitorSourcePublication.source_version_id == int(version.id),
            CompetitorSourcePublication.status == "published",
        )
        .with_for_update()
    )
    if publication is None:
        return _reject("published_source_lineage_absent")
    expected = int(version.row_count)
    if int(publication.row_count) != expected:
        return _reject("publication_row_count_mismatch")

    source_columns = tuple(
        getattr(CompetitorPriceListItem, field)
        for field in (
            "id",
            "product_id",
            *PROVIDER_SOURCE_FIELDS,
            "match_key",
            "match_type",
            "match_score",
            "matched_sku",
            "normalized_name",
            "normalized_manufacturer",
            "parsed_base_name",
            "parsed_form",
            "parsed_forms_json",
            "parsed_dosage",
            "parsed_dosage_volume",
            "parsed_quantity",
            "parsed_volume",
            "parsed_weight",
            "parsed_percent_strength",
            "parsed_concentration",
            "parsed_iu_dosage",
            "parsed_strength_signature",
            "parsed_dimensions_json",
            "parsed_critical_tokens_json",
        )
    )

    def existing_rows():
        last_id = 0
        while True:
            batch = db.execute(
                select(*source_columns)
                .where(
                    CompetitorPriceListItem.price_list_id == source_id,
                    CompetitorPriceListItem.id > last_id,
                )
                .order_by(CompetitorPriceListItem.id.asc())
                .limit(2000)
            ).mappings().all()
            if not batch:
                return
            for current in batch:
                yield current
            last_id = int(batch[-1]["id"])
    sentinel = object()
    hasher = CanonicalSourceHasher()
    occurrences: dict[str, int] = {}
    row_count = 0
    for ordinal, pair in enumerate(zip_longest(existing_rows(), incoming_rows, fillvalue=sentinel)):
        current, incoming = pair
        if current is sentinel or incoming is sentinel:
            return _reject("source_row_count_changed")
        current_mapping = _mapping(current)
        incoming_mapping = _mapping(incoming)
        fields = SOURCE_CONTENT_FIELDS if authoritative_input else PROVIDER_SOURCE_FIELDS
        for field in fields:
            alias = {
                "authoritative_product_id": "product_id",
                "authoritative_match_key": "match_key",
                "authoritative_match_type": "match_type",
                "authoritative_match_score": "match_score",
                "authoritative_matched_sku": "matched_sku",
            }.get(field, field)
            current_mapping[alias] = incoming_mapping.get(alias)
        canonical = canonical_source_row(
            current_mapping,
            input_ordinal=ordinal,
            occurrences=occurrences,
        )
        hasher.add(canonical)
        row_count += 1
    content_sha256, _ = hasher.finish()
    if row_count != expected or content_sha256 != str(version.content_sha256):
        return _reject("canonical_source_content_changed")

    lineage_count = int(
        db.scalar(
            select(func.count(CompetitorSourcePublicationItem.id))
            .select_from(CompetitorSourcePublicationItem)
            .join(
                CompetitorPriceListItem,
                CompetitorPriceListItem.id
                == CompetitorSourcePublicationItem.physical_source_item_id,
            )
            .join(
                CompetitorSourceVersionItem,
                CompetitorSourceVersionItem.id
                == CompetitorSourcePublicationItem.source_version_item_id,
            )
            .where(
                CompetitorSourcePublicationItem.source_publication_id
                == int(publication.id),
                CompetitorPriceListItem.price_list_id == source_id,
                CompetitorSourceVersionItem.source_version_id == int(version.id),
            )
        )
        or 0
    )
    if lineage_count != expected:
        return _reject("physical_lineage_incomplete")

    snapshot_id: int | None = None
    if require_snapshot:
        vector, _context = build_source_dependency_vector(
            db=db, competitor_price_list_id=source_id
        )
        snapshot = db.scalar(
            select(CompetitorMatchSnapshot)
            .where(
                CompetitorMatchSnapshot.source_version_id == int(version.id),
                CompetitorMatchSnapshot.dependency_fingerprint == vector.fingerprint,
                CompetitorMatchSnapshot.matcher_version == vector.matcher_version,
                CompetitorMatchSnapshot.normalization_version == vector.normalization_version,
                CompetitorMatchSnapshot.status.in_(("ready", "published")),
            )
            .order_by(CompetitorMatchSnapshot.id.desc())
        )
        if snapshot is None:
            return _reject("dependency_valid_snapshot_absent")
        snapshot_count = int(
            db.scalar(
                select(func.count(CompetitorMatchSnapshotItem.id)).where(
                    CompetitorMatchSnapshotItem.match_snapshot_id == int(snapshot.id)
                )
            )
            or 0
        )
        if snapshot_count != expected:
            return _reject("snapshot_row_count_mismatch")
        snapshot_id = int(snapshot.id)

    return SourceNoopDecision(
        True,
        "verified_identical_source",
        row_count=row_count,
        content_sha256=content_sha256,
        source_version_id=int(version.id),
        source_publication_id=int(publication.id),
        match_snapshot_id=snapshot_id,
    )
