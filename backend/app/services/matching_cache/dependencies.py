from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...models import CompetitorMatchDependencyRevision
from ...timezone import now_kz_naive


MATCHER_VERSION = "legacy-authoritative-decisions-v3"
NORMALIZATION_VERSION = "legacy-drug-normalization-v1"

PRODUCT_CATALOG = "product_catalog"
MANUAL_CODE_MAPPINGS = "manual_code_mappings"
PROVISOR_REFERENCES = "provisor_references"
PROVISOR_SUBSTITUTES = "provisor_substitutes"
VIDMAN_AUTHORITATIVE = "vidman_authoritative"
MATCHING_ALGORITHM = "matching_algorithm"
NORMALIZATION_ALGORITHM = "normalization_algorithm"

TRACKED_DEPENDENCIES = (
    PRODUCT_CATALOG,
    MANUAL_CODE_MAPPINGS,
    PROVISOR_REFERENCES,
    PROVISOR_SUBSTITUTES,
    VIDMAN_AUTHORITATIVE,
    MATCHING_ALGORITHM,
    NORMALIZATION_ALGORITHM,
)


@dataclass(frozen=True)
class DependencyVector:
    revisions: dict[str, int]
    matcher_version: str
    normalization_version: str
    fingerprint: str
    matching_context: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "revisions": dict(sorted(self.revisions.items())),
            "matcherVersion": self.matcher_version,
            "normalizationVersion": self.normalization_version,
        }
        if self.matching_context is not None:
            payload["matchingContext"] = self.matching_context
        return payload


def with_matching_context(
    vector: DependencyVector, *, context: Mapping[str, Any]
) -> DependencyVector:
    context = dict(context)
    payload = {
        "revisions": dict(sorted(vector.revisions.items())),
        "matcherVersion": vector.matcher_version,
        "normalizationVersion": vector.normalization_version,
        "matchingContext": context,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return DependencyVector(
        revisions=dict(vector.revisions),
        matcher_version=vector.matcher_version,
        normalization_version=vector.normalization_version,
        fingerprint=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        matching_context=context,
    )


def matching_context_key(context: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(context), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def ensure_dependency_revision(
    *, db: Session, scope_type: str, scope_key: str = "global"
) -> CompetitorMatchDependencyRevision:
    normalized_type = str(scope_type or "").strip()
    normalized_key = str(scope_key or "global").strip() or "global"
    if not normalized_type:
        raise ValueError("scope_type is required")
    row = db.execute(
        select(CompetitorMatchDependencyRevision)
        .where(CompetitorMatchDependencyRevision.scope_type == normalized_type)
        .where(CompetitorMatchDependencyRevision.scope_key == normalized_key)
    ).scalar_one_or_none()
    if row is not None:
        return row
    try:
        with db.begin_nested():
            row = CompetitorMatchDependencyRevision(
                scope_type=normalized_type,
                scope_key=normalized_key,
                revision=1,
            )
            db.add(row)
            db.flush()
    except IntegrityError:
        row = db.execute(
            select(CompetitorMatchDependencyRevision)
            .where(CompetitorMatchDependencyRevision.scope_type == normalized_type)
            .where(CompetitorMatchDependencyRevision.scope_key == normalized_key)
        ).scalar_one()
    return row


def bump_dependency_revision(
    *,
    db: Session,
    scope_type: str,
    scope_key: str = "global",
    fingerprint: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> CompetitorMatchDependencyRevision:
    row = ensure_dependency_revision(db=db, scope_type=scope_type, scope_key=scope_key)
    row = db.execute(
        select(CompetitorMatchDependencyRevision)
        .where(CompetitorMatchDependencyRevision.id == row.id)
        .with_for_update()
    ).scalar_one()
    row.revision = int(row.revision) + 1
    row.fingerprint = str(fingerprint or "")[:64]
    row.metadata_json = _json(metadata)
    row.updated_at = now_kz_naive()
    db.flush()
    return row


def read_dependency_revisions(
    *, db: Session, scopes: Mapping[str, str] | None = None
) -> dict[str, int]:
    requested = scopes or {scope: "global" for scope in TRACKED_DEPENDENCIES}
    revisions: dict[str, int] = {}
    for scope_type, scope_key in sorted(requested.items()):
        row = db.execute(
            select(CompetitorMatchDependencyRevision.revision)
            .where(CompetitorMatchDependencyRevision.scope_type == scope_type)
            .where(CompetitorMatchDependencyRevision.scope_key == (scope_key or "global"))
        ).scalar_one_or_none()
        revisions[f"{scope_type}:{scope_key or 'global'}"] = int(row or 0)
    return revisions


def build_dependency_vector(
    *, db: Session, scopes: Mapping[str, str] | None = None
) -> DependencyVector:
    revisions = read_dependency_revisions(db=db, scopes=scopes)
    payload = {
        "revisions": dict(sorted(revisions.items())),
        "matcherVersion": MATCHER_VERSION,
        "normalizationVersion": NORMALIZATION_VERSION,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return DependencyVector(
        revisions=revisions,
        matcher_version=MATCHER_VERSION,
        normalization_version=NORMALIZATION_VERSION,
        fingerprint=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )
