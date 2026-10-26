from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import PriceFormatCompetitorAssignment
from ..competitor_assignments import get_assigned_competitor_price_lists
from ..competitor_source_config import (
    MULTI_PRICE_PERCENTILE_MODE,
    effective_percentile_mode,
)
from .dependencies import (
    DependencyVector,
    build_dependency_vector,
    with_matching_context,
)


class MatchingContextConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class MatchingContext:
    price_format_ids: tuple[int, ...]
    source_ids: tuple[int, ...]

    @property
    def eligible(self) -> bool:
        return bool(self.price_format_ids and self.source_ids)


def resolve_matching_context(
    *, db: Session, competitor_price_list_id: int
) -> MatchingContext:
    source_id = int(competitor_price_list_id)
    price_format_ids = tuple(
        int(value)
        for value in db.scalars(
            select(PriceFormatCompetitorAssignment.price_format_id)
            .where(
                PriceFormatCompetitorAssignment.competitor_price_list_id == source_id,
                PriceFormatCompetitorAssignment.is_active.is_(True),
            )
            .order_by(PriceFormatCompetitorAssignment.price_format_id.asc())
        )
    )
    contexts: dict[tuple[int, ...], list[int]] = {}
    for price_format_id in price_format_ids:
        physical_source_ids = tuple(
            sorted(
                int(item.price_list.id)
                for item in get_assigned_competitor_price_lists(
                    db=db, price_format_id=price_format_id
                )
                if effective_percentile_mode(
                    item.price_list, item.assignment.percentile_mode
                )
                != MULTI_PRICE_PERCENTILE_MODE
            )
        )
        if source_id in physical_source_ids:
            contexts.setdefault(physical_source_ids, []).append(price_format_id)
    if not contexts:
        return MatchingContext((), (source_id,))
    if len(contexts) != 1:
        rendered = ", ".join(
            f"sources={list(source_ids)} formats={format_ids}"
            for source_ids, format_ids in sorted(contexts.items())
        )
        raise MatchingContextConflict(
            f"source {source_id} has incompatible matching cohorts: {rendered}"
        )
    source_ids, format_ids = next(iter(contexts.items()))
    return MatchingContext(tuple(sorted(format_ids)), source_ids)


def build_source_dependency_vector(
    *,
    db: Session,
    competitor_price_list_id: int,
    base_vector: DependencyVector | None = None,
    expected_source_ids: tuple[int, ...] | None = None,
) -> tuple[DependencyVector, MatchingContext]:
    context = resolve_matching_context(
        db=db, competitor_price_list_id=int(competitor_price_list_id)
    )
    if expected_source_ids is not None and context.source_ids != expected_source_ids:
        raise MatchingContextConflict(
            f"source {competitor_price_list_id} matching cohort changed: "
            f"expected={list(expected_source_ids)} actual={list(context.source_ids)}"
        )
    base = base_vector or build_dependency_vector(db=db)
    return (
        with_matching_context(
            base,
            price_format_ids=context.price_format_ids,
            source_ids=context.source_ids,
        ),
        context,
    )
