"""Physical-row lineage for immutable competitor source versions."""

from __future__ import annotations

from sqlalchemy import func, insert, literal, select, update
from sqlalchemy.orm import Session

from ...models import (
    CompetitorPriceListItem,
    CompetitorSourcePublication,
    CompetitorSourcePublicationItem,
    CompetitorSourceVersion,
    CompetitorSourceVersionItem,
)
from ...timezone import now_kz_naive
from .fingerprints import FINGERPRINT_FORMAT_VERSION


class SourceLineageError(RuntimeError):
    pass


def publish_physical_source_lineage(
    *, db: Session, source_version: CompetitorSourceVersion
) -> CompetitorSourcePublication:
    """Publish an exact ordinal map for the physical rows currently under lock.

    Source content hashing includes the source ordinal and duplicate ordinal.
    Therefore, after a successful version capture/reuse, joining both ordered
    row sets on source ordinal is an exact, duplicate-preserving identity map.
    The publication is mutable lineage; the source version remains immutable.
    """

    source_id = int(source_version.competitor_price_list_id)
    version_id = int(source_version.id)
    expected = int(source_version.row_count)
    publication = CompetitorSourcePublication(
        competitor_price_list_id=source_id,
        source_version_id=version_id,
        row_count=expected,
        fingerprint_format_version=FINGERPRINT_FORMAT_VERSION,
        status="pending",
        error="",
    )
    db.add(publication)
    db.flush()

    physical = (
        select(
            CompetitorPriceListItem.id.label("physical_source_item_id"),
            (
                func.row_number().over(order_by=CompetitorPriceListItem.id.asc()) - 1
            ).label("source_ordinal"),
        )
        .where(CompetitorPriceListItem.price_list_id == source_id)
        .subquery()
    )
    mapping_rows = (
        select(
            literal(
                int(publication.id),
                type_=CompetitorSourcePublicationItem.source_publication_id.type,
            ),
            CompetitorSourceVersionItem.id,
            physical.c.physical_source_item_id,
        )
        .select_from(CompetitorSourceVersionItem)
        .join(
            physical,
            physical.c.source_ordinal
            == CompetitorSourceVersionItem.source_ordinal,
        )
        .where(CompetitorSourceVersionItem.source_version_id == version_id)
    )
    db.execute(
        insert(CompetitorSourcePublicationItem).from_select(
            (
                "source_publication_id",
                "source_version_item_id",
                "physical_source_item_id",
            ),
            mapping_rows,
        )
    )

    mapped = int(
        db.scalar(
            select(func.count(CompetitorSourcePublicationItem.id)).where(
                CompetitorSourcePublicationItem.source_publication_id
                == int(publication.id)
            )
        )
        or 0
    )
    valid = int(
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
                CompetitorSourceVersionItem.source_version_id == version_id,
            )
        )
        or 0
    )
    if mapped != expected or valid != expected:
        raise SourceLineageError(
            "physical source lineage is incomplete: "
            f"source={source_id} version={version_id} expected={expected} "
            f"mapped={mapped} valid={valid}"
        )

    db.execute(
        update(CompetitorSourcePublication)
        .where(
            CompetitorSourcePublication.competitor_price_list_id == source_id,
            CompetitorSourcePublication.status == "published",
            CompetitorSourcePublication.id != int(publication.id),
        )
        .values(status="superseded")
        .execution_options(synchronize_session=False)
    )
    publication.status = "published"
    publication.published_at = now_kz_naive()
    publication.error = ""
    db.flush()
    return publication
