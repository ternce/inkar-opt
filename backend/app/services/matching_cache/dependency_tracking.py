from __future__ import annotations

from collections.abc import Iterable
import logging

from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session

from ...models import (
    CompetitorCodeMapping,
    InternalProductNormalized,
    Product,
    ProductExtra,
    ProductSubstituteMatch,
    ProvisorGoodsMap,
    SourceGoodsMatch,
    VidmanProductMatch,
)
from ...timezone import now_kz_naive
from .config import matching_cache_dual_write_enabled
from .dependencies import (
    MANUAL_CODE_MAPPINGS,
    PRODUCT_CATALOG,
    PROVISOR_REFERENCES,
    PROVISOR_SUBSTITUTES,
    VIDMAN_AUTHORITATIVE,
)


logger = logging.getLogger(__name__)
_SCOPES_KEY = "matching_cache_dependency_scopes"
_INSTALLED = False

# Kept explicit so coverage tests and rollout audits can fail closed when a new
# matching-relevant model is introduced without an invalidation decision.
DEPENDENCY_MODEL_COVERAGE = {
    "Product": (PRODUCT_CATALOG, PROVISOR_REFERENCES),
    "ProductExtra": (PRODUCT_CATALOG,),
    "InternalProductNormalized": (PRODUCT_CATALOG,),
    "CompetitorCodeMapping": (MANUAL_CODE_MAPPINGS,),
    "ProvisorGoodsMap": (PROVISOR_REFERENCES,),
    "ProductSubstituteMatch": (PROVISOR_SUBSTITUTES,),
    "VidmanProductMatch": (VIDMAN_AUTHORITATIVE,),
    "SourceGoodsMatch": (MANUAL_CODE_MAPPINGS,),
}

REQUIRED_DATABASE_TRIGGERS = frozenset({
    "trg_match_cache_products",
    "trg_match_cache_products_update",
    "trg_match_cache_product_extras",
    "trg_match_cache_product_extras_update",
    "trg_match_cache_internal_normalized",
    "trg_match_cache_internal_normalized_update",
    "trg_match_cache_competitor_code_mappings",
    "trg_match_cache_competitor_code_mappings_update",
    "trg_match_cache_provisor_goods_map",
    "trg_match_cache_provisor_goods_map_update",
    "trg_match_cache_product_substitutes",
    "trg_match_cache_product_substitutes_update",
    "trg_match_cache_vidman_matches",
    "trg_match_cache_vidman_matches_update",
    "trg_match_cache_approved_source_goods",
})


def assert_database_dependency_tracking(db: Session) -> None:
    """Fail closed before worker publication if PostgreSQL triggers are absent."""

    if db.bind is None or db.bind.dialect.name != "postgresql":
        raise RuntimeError("matching-cache workers require PostgreSQL dependency triggers")
    installed = set(db.scalars(text(
        """SELECT t.tgname
           FROM pg_trigger t
           JOIN pg_class c ON c.oid = t.tgrelid
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE NOT t.tgisinternal
             AND n.nspname = current_schema()
             AND t.tgname = ANY(:names)"""
    ), {"names": sorted(REQUIRED_DATABASE_TRIGGERS)}))
    missing = sorted(REQUIRED_DATABASE_TRIGGERS - installed)
    if missing:
        raise RuntimeError(
            "matching-cache dependency triggers are incomplete: " + ", ".join(missing)
        )


def _changed(obj: object, fields: Iterable[str]) -> bool:
    state = inspect(obj)
    is_deleted = state.deleted or (
        state.session is not None and obj in state.session.deleted
    )
    return state.pending or is_deleted or any(
        field in state.attrs and state.attrs[field].history.has_changes() for field in fields
    )


def _scopes_for(obj: object) -> set[str]:
    scopes: set[str] = set()
    if isinstance(obj, Product):
        if _changed(obj, ("code", "name", "top_rank", "provisor_goods_id")):
            scopes.add(PRODUCT_CATALOG)
        if _changed(obj, ("provisor_goods_id",)):
            scopes.add(PROVISOR_REFERENCES)
    elif isinstance(obj, ProductExtra):
        if _changed(obj, ("manufacturer",)):
            scopes.add(PRODUCT_CATALOG)
    elif isinstance(obj, InternalProductNormalized):
        if _changed(
            obj,
            (
                "product_id",
                "raw_name",
                "raw_manufacturer",
                "normalized_name",
                "base_name",
                "normalized_manufacturer",
                "dosage_value",
                "dosage_unit",
                "strength_components",
                "concentration_value",
                "concentration_unit",
                "volume_value",
                "volume_unit",
                "weight_value",
                "weight_unit",
                "package_volume",
                "package_weight",
                "pack_count",
                "dosage_form",
                "variant_text",
                "identity_tokens_json",
                "normalized_signature",
                "parse_warnings_json",
                "parse_confidence",
            ),
        ):
            scopes.add(PRODUCT_CATALOG)
    elif isinstance(obj, CompetitorCodeMapping):
        if _changed(
            obj,
            (
                "platform",
                "source_external_key",
                "source_match_key",
                "source_name",
                "source_manufacturer",
                "source_dosage_form",
                "source_normalized_name",
                "our_product_id",
                "our_sku",
                "status",
                "confidence",
            ),
        ):
            scopes.add(MANUAL_CODE_MAPPINGS)
    elif isinstance(obj, ProvisorGoodsMap):
        if _changed(obj, ("price_format_id", "goods_id", "product_id")):
            scopes.add(PROVISOR_REFERENCES)
    elif isinstance(obj, ProductSubstituteMatch):
        if _changed(
            obj,
            (
                "product_id",
                "source_type",
                "source_goods_id",
                "source_distributor_goods_id",
                "source_name",
                "source_manufacturer",
                "status",
                "priority",
            ),
        ):
            scopes.add(PROVISOR_SUBSTITUTES)
    elif isinstance(obj, VidmanProductMatch):
        if _changed(obj, ("canonical_product_id", "product_id", "status", "match_type", "confidence")):
            scopes.add(VIDMAN_AUTHORITATIVE)
    elif isinstance(obj, SourceGoodsMatch):
        # The legacy matcher auto-learns these rows. Tracking those writes would
        # invalidate the very snapshot that produced them forever. Only an
        # explicitly manual/approved row is an authoritative dependency.
        method = str(getattr(obj, "match_method", "") or "").casefold()
        if ("manual" in method or "approved" in method) and _changed(
            obj, ("price_format_id", "source_type", "distributor_goods_id", "goods_id", "product_id")
        ):
            scopes.add(MANUAL_CODE_MAPPINGS)
    return scopes


def _collect_dependency_changes(session: Session, _flush_context, _instances) -> None:
    if not matching_cache_dual_write_enabled():
        return
    scopes = set(session.info.get(_SCOPES_KEY, set()))
    for obj in set(session.new).union(session.dirty).union(session.deleted):
        scopes.update(_scopes_for(obj))
    if scopes:
        session.info[_SCOPES_KEY] = scopes


def _publish_dependency_changes(session: Session, _flush_context) -> None:
    scopes = set(session.info.pop(_SCOPES_KEY, set()))
    if not scopes:
        return
    now = now_kz_naive()
    connection = session.connection()
    if connection.dialect.name == "postgresql":
        trigger_function = connection.scalar(
            text("SELECT to_regprocedure('matching_cache_bump_dependency(text)')")
        )
        if trigger_function is not None:
            # Phase 2B.3 database triggers already invalidated this transaction
            # and also cover Core SQL, raw SQL and out-of-process writers.
            return
    for scope in sorted(scopes):
        connection.execute(
            text(
                """
                INSERT INTO competitor_match_dependency_revisions
                    (scope_type, scope_key, revision, fingerprint, metadata_json, created_at, updated_at)
                VALUES (:scope, 'global', 1, '', '{}', :now, :now)
                ON CONFLICT (scope_type, scope_key) DO UPDATE
                SET revision = competitor_match_dependency_revisions.revision + 1,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {"scope": scope, "now": now},
        )
    logger.info("[MATCH_CACHE_DEPENDENCY_INVALIDATION] scopes=%s", sorted(scopes))


def install_dependency_tracking() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    event.listen(Session, "before_flush", _collect_dependency_changes)
    event.listen(Session, "after_flush_postexec", _publish_dependency_changes)
    _INSTALLED = True
