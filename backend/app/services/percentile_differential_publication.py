from __future__ import annotations

from dataclasses import dataclass
import os

from sqlalchemy import text
from sqlalchemy.orm import Session


def percentile_differential_publication_enabled() -> bool:
    raw = os.getenv("PERCENTILE_DIFFERENTIAL_PUBLICATION_ENABLED", "false")
    return str(raw).strip().casefold() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class DifferentialPublicationStats:
    inserted: int
    updated: int
    deleted: int
    unchanged: int
    replacement_used: bool = False


def prepare_emit_percentile_stage(db: Session, *, table_name: str) -> None:
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError("differential percentile publication requires PostgreSQL")
    db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    db.execute(
        text(
            f"""
            CREATE TEMP TABLE {table_name}
            (LIKE competitor_price_percentiles INCLUDING DEFAULTS)
            ON COMMIT DROP
            """
        )
    )
    db.execute(
        text(
            f"""
            CREATE UNIQUE INDEX ON {table_name} (
                price_format_id,
                product_id,
                source_key,
                branch_name,
                competitor_name,
                percentile_scope,
                percentile
            )
            """
        )
    )


def publish_emit_percentile_stage(
    db: Session,
    *,
    table_name: str,
    scope_sql: str,
    params: dict,
) -> DifferentialPublicationStats:
    """Atomically synchronize one proven percentile scope from a temp table.

    ``scope_sql`` is trusted application SQL referring to target alias ``t``.
    Values remain bound through ``params``. The unique business key is the
    database constraint used by the legacy replacement path.
    """

    changed = """
        ROW(
            t.competitor_price_list_id,
            t.source_type,
            t.value,
            t.source_count,
            t.price_count,
            t.used_price_count,
            t.status
        ) IS DISTINCT FROM ROW(
            s.competitor_price_list_id,
            s.source_type,
            s.value,
            s.source_count,
            s.price_count,
            s.used_price_count,
            s.status
        )
    """
    key_match = """
        t.price_format_id = s.price_format_id
        AND t.product_id = s.product_id
        AND t.source_key IS NOT DISTINCT FROM s.source_key
        AND t.branch_name IS NOT DISTINCT FROM s.branch_name
        AND t.competitor_name IS NOT DISTINCT FROM s.competitor_name
        AND t.percentile_scope = s.percentile_scope
        AND t.percentile = s.percentile
    """
    staged = int(db.scalar(text(f"SELECT count(*) FROM {table_name}")) or 0)
    existing = int(
        db.scalar(
            text(f"SELECT count(*) FROM competitor_price_percentiles t WHERE ({scope_sql})"),
            params,
        )
        or 0
    )
    changed_count = int(db.scalar(text(f"""
        SELECT count(*)
        FROM competitor_price_percentiles t
        JOIN {table_name} s ON {key_match}
        WHERE {changed}
    """), params) or 0)
    missing_count = int(db.scalar(text(f"""
        SELECT count(*) FROM {table_name} s
        WHERE NOT EXISTS (
            SELECT 1 FROM competitor_price_percentiles t WHERE {key_match}
        )
    """), params) or 0)
    obsolete_count = int(db.scalar(text(f"""
        SELECT count(*) FROM competitor_price_percentiles t
        WHERE ({scope_sql})
          AND NOT EXISTS (SELECT 1 FROM {table_name} s WHERE {key_match})
    """), params) or 0)
    raw_threshold = os.getenv("PERCENTILE_DIFFERENTIAL_REPLACEMENT_THRESHOLD", "0.5")
    try:
        replacement_threshold = min(1.0, max(0.0, float(raw_threshold)))
    except ValueError:
        replacement_threshold = 0.5
    mutation_ratio = (changed_count + missing_count + obsolete_count) / max(1, staged, existing)
    if mutation_ratio >= replacement_threshold and (staged or existing):
        deleted = int(db.execute(text(f"""
            DELETE FROM competitor_price_percentiles t WHERE ({scope_sql})
        """), params).rowcount or 0)
        inserted = int(db.execute(text(f"""
            INSERT INTO competitor_price_percentiles (
                price_format_id, product_id, competitor_price_list_id,
                source_type, source_key, branch_name, competitor_name,
                percentile_scope, percentile, value, source_count,
                price_count, used_price_count, status, updated_at
            )
            SELECT price_format_id, product_id, competitor_price_list_id,
                   source_type, source_key, branch_name, competitor_name,
                   percentile_scope, percentile, value, source_count,
                   price_count, used_price_count, status, updated_at
            FROM {table_name}
        """), params).rowcount or 0)
        return DifferentialPublicationStats(inserted, 0, deleted, 0, True)
    updated = int(
        db.execute(
            text(
                f"""
                UPDATE competitor_price_percentiles AS t
                SET competitor_price_list_id = s.competitor_price_list_id,
                    source_type = s.source_type,
                    value = s.value,
                    source_count = s.source_count,
                    price_count = s.price_count,
                    used_price_count = s.used_price_count,
                    status = s.status,
                    updated_at = s.updated_at
                FROM {table_name} AS s
                WHERE {key_match}
                  AND ({changed})
                """
            ),
            params,
        ).rowcount
        or 0
    )
    deleted = int(
        db.execute(
            text(
                f"""
                DELETE FROM competitor_price_percentiles AS t
                WHERE ({scope_sql})
                  AND NOT EXISTS (
                    SELECT 1 FROM {table_name} AS s WHERE {key_match}
                  )
                """
            ),
            params,
        ).rowcount
        or 0
    )
    inserted = int(
        db.execute(
            text(
                f"""
                INSERT INTO competitor_price_percentiles (
                    price_format_id, product_id, competitor_price_list_id,
                    source_type, source_key, branch_name, competitor_name,
                    percentile_scope, percentile, value, source_count,
                    price_count, used_price_count, status, updated_at
                )
                SELECT
                    s.price_format_id, s.product_id, s.competitor_price_list_id,
                    s.source_type, s.source_key, s.branch_name, s.competitor_name,
                    s.percentile_scope, s.percentile, s.value, s.source_count,
                    s.price_count, s.used_price_count, s.status, s.updated_at
                FROM {table_name} AS s
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM competitor_price_percentiles AS t
                    WHERE {key_match}
                )
                """
            ),
            params,
        ).rowcount
        or 0
    )
    return DifferentialPublicationStats(
        inserted=inserted,
        updated=updated,
        deleted=deleted,
        unchanged=max(0, staged - inserted - updated),
    )


def prepare_regular_percentile_stage(db: Session, *, table_name: str) -> None:
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError("differential percentile publication requires PostgreSQL")
    db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    db.execute(
        text(
            f"""
            CREATE TEMP TABLE {table_name}
            (LIKE regular_competitor_price_percentiles INCLUDING DEFAULTS)
            ON COMMIT DROP
            """
        )
    )
    db.execute(
        text(
            f"CREATE UNIQUE INDEX ON {table_name} "
            "(competitor_identity, product_id, percentile)"
        )
    )


def prepare_emit_summary_stage(db: Session, *, table_name: str) -> None:
    db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    db.execute(text(
        f"CREATE TEMP TABLE {table_name} "
        "(LIKE competitor_price_percentile_source_summaries INCLUDING DEFAULTS) ON COMMIT DROP"
    ))
    db.execute(text(f"""
        CREATE UNIQUE INDEX ON {table_name} (
            price_format_id, source_type, source_key,
            coalesce(competitor_price_list_id, -1), branch_name,
            competitor_name, percentile_scope, percentile
        )
    """))


def publish_emit_summary_stage(
    db: Session,
    *,
    table_name: str,
    scope_sql: str,
    params: dict,
) -> DifferentialPublicationStats:
    key = """
        t.price_format_id=s.price_format_id
        AND t.source_type=s.source_type
        AND t.source_key IS NOT DISTINCT FROM s.source_key
        AND t.competitor_price_list_id IS NOT DISTINCT FROM s.competitor_price_list_id
        AND t.branch_name IS NOT DISTINCT FROM s.branch_name
        AND t.competitor_name IS NOT DISTINCT FROM s.competitor_name
        AND t.percentile_scope=s.percentile_scope
        AND t.percentile=s.percentile
    """
    updated = int(db.execute(text(f"""
        UPDATE competitor_price_percentile_source_summaries t
        SET sku_count=s.sku_count, source_count=s.source_count,
            generated_at=s.generated_at, updated_at=s.updated_at
        FROM {table_name} s
        WHERE {key}
          AND ROW(t.sku_count,t.source_count,t.generated_at)
              IS DISTINCT FROM ROW(s.sku_count,s.source_count,s.generated_at)
    """), params).rowcount or 0)
    deleted = int(db.execute(text(f"""
        DELETE FROM competitor_price_percentile_source_summaries t
        WHERE ({scope_sql})
          AND NOT EXISTS (SELECT 1 FROM {table_name} s WHERE {key})
    """), params).rowcount or 0)
    inserted = int(db.execute(text(f"""
        INSERT INTO competitor_price_percentile_source_summaries (
            price_format_id,source_type,source_key,competitor_price_list_id,
            branch_name,competitor_name,percentile_scope,percentile,
            sku_count,source_count,generated_at,updated_at
        )
        SELECT s.price_format_id,s.source_type,s.source_key,s.competitor_price_list_id,
               s.branch_name,s.competitor_name,s.percentile_scope,s.percentile,
               s.sku_count,s.source_count,s.generated_at,s.updated_at
        FROM {table_name} s
        WHERE NOT EXISTS (
            SELECT 1 FROM competitor_price_percentile_source_summaries t WHERE {key}
        )
    """), params).rowcount or 0)
    staged = int(db.scalar(text(f"SELECT count(*) FROM {table_name}")) or 0)
    return DifferentialPublicationStats(inserted, updated, deleted, max(0, staged - inserted - updated))


def publish_regular_percentile_stage(
    db: Session,
    *,
    table_name: str,
    all_identities: bool,
) -> DifferentialPublicationStats:
    key_match = """
        t.competitor_identity = s.competitor_identity
        AND t.product_id = s.product_id
        AND t.percentile = s.percentile
    """
    changed = """
        ROW(t.competitor_name, t.value, t.sample_count, t.source_count,
            t.min_price, t.max_price, t.algorithm_version)
        IS DISTINCT FROM
        ROW(s.competitor_name, s.value, s.sample_count, s.source_count,
            s.min_price, s.max_price, s.algorithm_version)
    """
    updated = int(
        db.execute(
            text(
                f"""
                UPDATE regular_competitor_price_percentiles AS t
                SET competitor_name = s.competitor_name,
                    value = s.value,
                    sample_count = s.sample_count,
                    source_count = s.source_count,
                    min_price = s.min_price,
                    max_price = s.max_price,
                    algorithm_version = s.algorithm_version,
                    calculated_at = s.calculated_at
                FROM {table_name} AS s
                WHERE {key_match} AND ({changed})
                """
            )
        ).rowcount
        or 0
    )
    identity_scope = (
        "TRUE"
        if all_identities
        else "t.competitor_identity IN (SELECT identity FROM tmp_regular_percentile_identities)"
    )
    deleted = int(
        db.execute(
            text(
                f"""
                DELETE FROM regular_competitor_price_percentiles AS t
                WHERE {identity_scope}
                  AND NOT EXISTS (
                    SELECT 1 FROM {table_name} AS s WHERE {key_match}
                  )
                """
            )
        ).rowcount
        or 0
    )
    inserted = int(
        db.execute(
            text(
                f"""
                INSERT INTO regular_competitor_price_percentiles (
                    competitor_identity, competitor_name, product_id,
                    percentile, value, sample_count, source_count,
                    min_price, max_price, algorithm_version, calculated_at
                )
                SELECT s.competitor_identity, s.competitor_name, s.product_id,
                       s.percentile, s.value, s.sample_count, s.source_count,
                       s.min_price, s.max_price, s.algorithm_version, s.calculated_at
                FROM {table_name} AS s
                WHERE NOT EXISTS (
                    SELECT 1 FROM regular_competitor_price_percentiles AS t
                    WHERE {key_match}
                )
                """
            )
        ).rowcount
        or 0
    )
    staged = int(db.scalar(text(f"SELECT count(*) FROM {table_name}")) or 0)
    return DifferentialPublicationStats(inserted, updated, deleted, max(0, staged - inserted - updated))
