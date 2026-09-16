from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, true

from ....models import CompetitorPricePercentile
from ...competitor_source_config import emit_display_aliases_from_source_key


PERCENTILE_SOURCE_EMIT = "emit"
PERCENTILE_SOURCE_COMPETITOR = "competitor"
PERCENTILE_SOURCE_DEFAULT = PERCENTILE_SOURCE_EMIT
PERCENTILE_SOURCE_CHOICES = {PERCENTILE_SOURCE_EMIT, PERCENTILE_SOURCE_COMPETITOR}
_EMIT_SOURCE_ID_RE = re.compile(
    r"^(?P<price_format_id>\d+):(?P<scope>[^:]+):(?P<source_key>emit:\d+):"
    r"(?P<region>.*):(?P<competitor>.*):p(?P<percentile>\d+)$"
)


def normalize_percentile_source(value: object) -> str:
    source = str(value or PERCENTILE_SOURCE_DEFAULT).strip().casefold()
    return source if source in PERCENTILE_SOURCE_CHOICES else PERCENTILE_SOURCE_DEFAULT


def is_emit_source_key(value: object) -> bool:
    return str(value or "").strip().startswith("emit:")


def percentile_source_id(
    *,
    percentile_source: str,
    price_format_id: object,
    scope: object,
    source_key: object,
    region: object,
    competitor: object,
    percentile: object,
) -> str:
    base = (
        f"{price_format_id}:{scope}:{source_key or ''}:{region or ''}:"
        f"{competitor or ''}:p{int(percentile)}"
    )
    if normalize_percentile_source(percentile_source) == PERCENTILE_SOURCE_EMIT:
        return base
    return f"{PERCENTILE_SOURCE_COMPETITOR}:{base}"


def emit_percentile_source_id_aliases(source_id: object) -> set[str]:
    text = str(source_id or "").strip()
    match = _EMIT_SOURCE_ID_RE.match(text)
    if not match:
        return {text} if text else set()
    source_key = match.group("source_key")
    aliases = emit_display_aliases_from_source_key(source_key)
    if not aliases:
        return {text}
    out = {text}
    for alias in aliases:
        out.add(
            percentile_source_id(
                percentile_source=PERCENTILE_SOURCE_EMIT,
                price_format_id=match.group("price_format_id"),
                scope=match.group("scope"),
                source_key=source_key,
                region=alias,
                competitor=alias,
                percentile=match.group("percentile"),
            )
        )
    return out


def emit_percentile_source_name_aliases(source_name: object) -> set[str]:
    text = str(source_name or "").strip()
    if not text.startswith("percentile:"):
        return {text} if text else set()
    return {f"percentile:{source_id}" for source_id in emit_percentile_source_id_aliases(text.removeprefix("percentile:"))}


@dataclass(frozen=True)
class PercentileSourceProvider:
    key: str
    label: str
    regional: bool
    requires_competitor: bool

    def row_filter(self):
        source_key = func.coalesce(CompetitorPricePercentile.source_key, "")
        if self.key == PERCENTILE_SOURCE_EMIT:
            return true()
        return ~source_key.like("emit:%")

    def source_id(
        self,
        *,
        price_format_id: object,
        scope: object,
        source_key: object,
        region: object,
        competitor: object,
        percentile: object,
    ) -> str:
        return percentile_source_id(
            percentile_source=self.key,
            price_format_id=price_format_id,
            scope=scope,
            source_key=source_key,
            region=region,
            competitor=competitor,
            percentile=percentile,
        )


_PROVIDERS = {
    PERCENTILE_SOURCE_EMIT: PercentileSourceProvider(
        key=PERCENTILE_SOURCE_EMIT,
        label="Emit",
        regional=True,
        requires_competitor=False,
    ),
    PERCENTILE_SOURCE_COMPETITOR: PercentileSourceProvider(
        key=PERCENTILE_SOURCE_COMPETITOR,
        label="Other Competitors",
        regional=False,
        requires_competitor=True,
    ),
}


def get_percentile_provider(value: object) -> PercentileSourceProvider:
    return _PROVIDERS[normalize_percentile_source(value)]
