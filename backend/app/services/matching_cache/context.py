from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from ...models import (
    CompetitorPriceList,
    CompetitorSourceVersion,
    CompetitorSourceVersionItem,
    PriceFormatCompetitorAssignment,
)
from ..competitor_assignments import get_assigned_competitor_price_lists
from ..competitor_source_config import MULTI_PRICE_PERCENTILE_MODE, effective_percentile_mode
from .dependencies import (
    DependencyVector,
    build_dependency_vector,
    matching_context_key,
    with_matching_context,
)


class MatchingContextConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class MatchingReferenceSource:
    source_id: int
    source_version_id: int | None
    content_sha256: str

    def as_dict(self) -> dict:
        return {
            "sourceId": self.source_id,
            "sourceVersionId": self.source_version_id,
            "contentSha256": self.content_sha256,
        }


@dataclass(frozen=True)
class MatchingContext:
    price_format_ids: tuple[int, ...]
    source_ids: tuple[int, ...]
    provisor_reference_sources: tuple[MatchingReferenceSource, ...] = ()

    @property
    def eligible(self) -> bool:
        return bool(self.price_format_ids and self.source_ids)

    def identity_dict(self) -> dict:
        payload = {
            "priceFormatIds": list(self.price_format_ids),
            "sourceIds": list(self.source_ids),
        }
        if self.provisor_reference_sources:
            payload["provisorReferenceSources"] = [
                item.as_dict() for item in self.provisor_reference_sources
            ]
        return payload

    @property
    def context_key(self) -> str:
        return matching_context_key(self.identity_dict())


def _physical_source_ids(*, db: Session, price_format_id: int) -> tuple[int, ...]:
    return tuple(sorted(
        int(item.price_list.id)
        for item in get_assigned_competitor_price_lists(
            db=db, price_format_id=int(price_format_id)
        )
        if effective_percentile_mode(item.price_list, item.assignment.percentile_mode)
        != MULTI_PRICE_PERCENTILE_MODE
    ))


def _reference_sources(
    *, db: Session, source_ids: tuple[int, ...]
) -> tuple[MatchingReferenceSource, ...]:
    if not source_ids:
        return ()
    # Local import avoids the competitor_matching -> materialization -> context
    # module cycle while keeping this identity tied to the legacy constant.
    from ..competitor_matching import PROVISOR_REFERENCE_FILIAL_IDS

    reference_ids = tuple(int(value) for value in PROVISOR_REFERENCE_FILIAL_IDS)
    rows = db.execute(
        select(
            CompetitorPriceList.id,
            CompetitorSourceVersion.id,
            CompetitorSourceVersion.content_sha256,
        )
        .join(
            CompetitorSourceVersion,
            (CompetitorSourceVersion.competitor_price_list_id == CompetitorPriceList.id)
            & (CompetitorSourceVersion.status == "published"),
            isouter=True,
        )
        .where(
            CompetitorPriceList.id.in_(source_ids),
            CompetitorPriceList.source_type == "provisor",
            exists().where(
                CompetitorSourceVersionItem.source_version_id
                == CompetitorSourceVersion.id,
                CompetitorSourceVersionItem.filial_id.in_(reference_ids),
                CompetitorSourceVersionItem.provisor_goods_id.is_not(None),
            ),
        )
        .order_by(CompetitorPriceList.id.asc())
    ).all()
    return tuple(
        MatchingReferenceSource(
            source_id=int(source_id),
            source_version_id=int(version_id) if version_id is not None else None,
            content_sha256=str(content_sha256 or ""),
        )
        for source_id, version_id, content_sha256 in rows
    )


def resolve_matching_contexts(
    *, db: Session, competitor_price_list_id: int
) -> tuple[MatchingContext, ...]:
    """Return every independent legacy matching context for one physical source."""

    source_id = int(competitor_price_list_id)
    price_format_ids = tuple(int(value) for value in db.scalars(
        select(PriceFormatCompetitorAssignment.price_format_id)
        .where(
            PriceFormatCompetitorAssignment.competitor_price_list_id == source_id,
            PriceFormatCompetitorAssignment.is_active.is_(True),
        )
        .order_by(PriceFormatCompetitorAssignment.price_format_id.asc())
    ))
    grouped: dict[
        tuple[tuple[int, ...], tuple[MatchingReferenceSource, ...]], list[int]
    ] = {}
    for price_format_id in price_format_ids:
        source_ids = _physical_source_ids(db=db, price_format_id=price_format_id)
        if source_id not in source_ids:
            continue
        reference_sources = _reference_sources(db=db, source_ids=source_ids)
        grouped.setdefault((source_ids, reference_sources), []).append(price_format_id)
    return tuple(
        MatchingContext(
            price_format_ids=tuple(sorted(format_ids)),
            source_ids=source_ids,
            provisor_reference_sources=reference_sources,
        )
        for (source_ids, reference_sources), format_ids in sorted(
            grouped.items(), key=lambda item: (item[1], item[0][0])
        )
    )


def resolve_matching_context(
    *, db: Session, competitor_price_list_id: int,
    price_format_id: int | None = None, context_key: str | None = None,
) -> MatchingContext:
    source_id = int(competitor_price_list_id)
    contexts = resolve_matching_contexts(db=db, competitor_price_list_id=source_id)
    if not contexts:
        singleton = MatchingContext((), (source_id,))
        if context_key and singleton.context_key != context_key:
            raise MatchingContextConflict(
                f"source {source_id} singleton matching context changed"
            )
        return singleton
    if price_format_id is not None:
        contexts = tuple(item for item in contexts if int(price_format_id) in item.price_format_ids)
    if context_key:
        contexts = tuple(item for item in contexts if item.context_key == context_key)
    if len(contexts) == 1:
        return contexts[0]
    qualifier = (
        f"price_format_id={price_format_id}" if price_format_id is not None
        else f"context_key={context_key}" if context_key else "no explicit context"
    )
    rendered = ", ".join(
        f"key={item.context_key} formats={list(item.price_format_ids)} sources={list(item.source_ids)}"
        for item in contexts
    )
    raise MatchingContextConflict(
        f"source {source_id} requires one matching context ({qualifier}); candidates=[{rendered}]"
    )


def build_source_dependency_vector(
    *, db: Session, competitor_price_list_id: int,
    base_vector: DependencyVector | None = None,
    expected_source_ids: tuple[int, ...] | None = None,
    price_format_id: int | None = None,
    context_key: str | None = None,
) -> tuple[DependencyVector, MatchingContext]:
    context = resolve_matching_context(
        db=db,
        competitor_price_list_id=int(competitor_price_list_id),
        price_format_id=price_format_id,
        context_key=context_key,
    )
    if expected_source_ids is not None and context.source_ids != expected_source_ids:
        raise MatchingContextConflict(
            f"source {competitor_price_list_id} matching cohort changed: "
            f"expected={list(expected_source_ids)} actual={list(context.source_ids)}"
        )
    base = base_vector or build_dependency_vector(db=db)
    return with_matching_context(base, context=context.identity_dict()), context
