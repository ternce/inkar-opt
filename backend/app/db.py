from __future__ import annotations

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _default_sqlite_url() -> str:
    # Keep SQLite DB location stable regardless of CWD.
    # Store it as backend/app.db (next to backend/ folder).
    backend_dir = Path(__file__).resolve().parents[1]
    db_path = (backend_dir / "app.db").resolve()
    return f"sqlite:///{db_path.as_posix()}"


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL", _default_sqlite_url())

    # Railway (and some other platforms) commonly provide Postgres URLs as:
    #   postgresql://user:pass@host:port/db
    # SQLAlchemy's default driver for "postgresql://" is psycopg2.
    # This project uses psycopg (v3), so we normalize to the explicit driver.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = "postgresql+psycopg://" + url[len("postgresql://") :]

    return url


engine = create_engine(
    get_database_url(),
    connect_args={"check_same_thread": False} if get_database_url().startswith("sqlite") else {},
)


@event.listens_for(engine, "connect")
def _configure_sqlite_connection(dbapi_connection, connection_record) -> None:
    if not get_database_url().startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    # Импорт моделей важен, чтобы Base увидел таблицы
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_compatible_columns()
    _ensure_compatible_column_types()
    _ensure_nullable_percentile_values()
    _ensure_percentile_source_identity()
    _backfill_percentile_source_identity()
    _ensure_compatible_indexes()
    _backfill_competitor_assignments()
    _backfill_competitor_price_coefficients()


def _ensure_compatible_columns() -> None:
    """Small create_all companion for existing SQLite/Postgres databases."""
    additions = {
        "list_items": [
            ("special_value", "TEXT DEFAULT ''"),
        ],
        "price_formats": [
            ("competitor_price_mode", "VARCHAR(32) DEFAULT 'regular'"),
            ("percentile_number", "INTEGER DEFAULT 10"),
            ("pricing_rule_id", "INTEGER"),
            ("rounding_rule_id", "INTEGER"),
            ("pricing_rule_applied_at", "DATETIME"),
            ("pricing_rule_applied_tables_json", "TEXT DEFAULT '[]'"),
        ],
        "products": [
            ("top_rank", "INTEGER"),
            ("provisor_goods_id", "BIGINT"),
        ],
        "competitor_price_lists": [
            ("branch_id", "TEXT DEFAULT ''"),
            ("branch_code", "TEXT DEFAULT ''"),
            ("branch_name", "TEXT DEFAULT 'Без филиала'"),
            ("competitor_name", "TEXT DEFAULT ''"),
            ("account_id", "TEXT DEFAULT ''"),
            ("account_login", "TEXT DEFAULT ''"),
            ("external_price_list_id", "TEXT DEFAULT ''"),
            ("sync_batch_id", "VARCHAR(64) DEFAULT ''"),
            ("source_updated_at", "TEXT DEFAULT ''"),
            ("last_checked_at", "DATETIME"),
            ("last_success_at", "DATETIME"),
            ("last_refresh_status", "VARCHAR(64) DEFAULT ''"),
            ("last_refresh_message", "TEXT DEFAULT ''"),
            ("price_coefficient", "NUMERIC(18, 6) DEFAULT 1.0"),
        ],
        "competitor_price_list_items": [
            ("provisor_goods_id", "BIGINT"),
            ("expiry_date", "TEXT DEFAULT ''"),
            ("match_key", "TEXT DEFAULT ''"),
            ("match_type", "VARCHAR(64) DEFAULT 'unmatched'"),
            ("match_score", "NUMERIC(18, 4)"),
            ("matched_sku", "TEXT DEFAULT ''"),
            ("raw_name", "TEXT DEFAULT ''"),
            ("raw_manufacturer", "TEXT DEFAULT ''"),
            ("normalized_name", "TEXT DEFAULT ''"),
            ("normalized_manufacturer", "TEXT DEFAULT ''"),
            ("parsed_base_name", "TEXT DEFAULT ''"),
            ("parsed_form", "TEXT DEFAULT ''"),
            ("parsed_forms_json", "TEXT DEFAULT ''"),
            ("parsed_dosage", "NUMERIC(18, 4)"),
            ("parsed_dosage_volume", "NUMERIC(18, 4)"),
            ("parsed_quantity", "INTEGER"),
            ("parsed_volume", "NUMERIC(18, 4)"),
            ("parsed_weight", "NUMERIC(18, 4)"),
            ("parsed_percent_strength", "NUMERIC(18, 4)"),
            ("parsed_concentration", "NUMERIC(18, 4)"),
            ("parsed_iu_dosage", "NUMERIC(18, 4)"),
            ("parsed_strength_signature", "TEXT DEFAULT ''"),
            ("parsed_dimensions_json", "TEXT DEFAULT ''"),
            ("parsed_critical_tokens_json", "TEXT DEFAULT ''"),
        ],
        "competitors_prices": [
            ("match_type", "VARCHAR(64) DEFAULT ''"),
            ("source_item_id", "INTEGER"),
            ("source_goods_id", "BIGINT"),
            ("source_distributor_goods_id", "TEXT DEFAULT ''"),
            ("source_manufacturer", "TEXT DEFAULT ''"),
        ],
        "competitor_price_percentiles": [
            ("competitor_price_list_id", "INTEGER"),
            ("source_type", "VARCHAR(32) DEFAULT ''"),
            ("source_key", "TEXT DEFAULT ''"),
            ("percentile_scope", "VARCHAR(32) DEFAULT 'regional'"),
            ("price_count", "INTEGER DEFAULT 0"),
            ("used_price_count", "INTEGER DEFAULT 0"),
            ("status", "VARCHAR(64) DEFAULT ''"),
        ],
        "jobs": [
            ("type", "VARCHAR(64)"),
            ("status", "VARCHAR(32) DEFAULT 'pending'"),
            ("format_code", "TEXT DEFAULT ''"),
            ("price_format_id", "INTEGER"),
            ("account_id", "INTEGER"),
            ("progress", "INTEGER DEFAULT 0"),
            ("message", "TEXT DEFAULT ''"),
            ("logs", "TEXT DEFAULT '[]'"),
            ("result_json", "TEXT DEFAULT '{}'"),
            ("error", "TEXT DEFAULT ''"),
            ("created_at", "DATETIME"),
            ("started_at", "DATETIME"),
            ("updated_at", "DATETIME"),
            ("finished_at", "DATETIME"),
        ],
        "refresh_jobs": [
            ("source_type", "VARCHAR(64)"),
            ("mode", "VARCHAR(32) DEFAULT 'selected'"),
            ("status", "VARCHAR(32) DEFAULT 'pending'"),
            ("started_at", "DATETIME"),
            ("finished_at", "DATETIME"),
            ("heartbeat_at", "DATETIME"),
            ("requested_by", "TEXT DEFAULT ''"),
            ("total_accounts", "INTEGER DEFAULT 0"),
            ("processed_accounts", "INTEGER DEFAULT 0"),
            ("total_plk", "INTEGER DEFAULT 0"),
            ("processed_plk", "INTEGER DEFAULT 0"),
            ("success_count", "INTEGER DEFAULT 0"),
            ("failed_count", "INTEGER DEFAULT 0"),
            ("skipped_count", "INTEGER DEFAULT 0"),
            ("message", "TEXT DEFAULT ''"),
            ("error_message", "TEXT DEFAULT ''"),
            ("metadata_json", "TEXT DEFAULT '{}'"),
        ],
        "refresh_locks": [
            ("name", "VARCHAR(128)"),
            ("owner_token", "VARCHAR(128) DEFAULT ''"),
            ("lock_type", "VARCHAR(64) DEFAULT ''"),
            ("acquired_at", "DATETIME"),
            ("heartbeat_at", "DATETIME"),
            ("lease_until", "DATETIME"),
            ("metadata_json", "TEXT DEFAULT '{}'"),
        ],
        "product_substitute_matches": [
            ("product_id", "INTEGER"),
            ("source_type", "VARCHAR(64) DEFAULT 'provisor'"),
            ("source_goods_id", "BIGINT"),
            ("source_distributor_goods_id", "TEXT DEFAULT ''"),
            ("source_name", "TEXT DEFAULT ''"),
            ("source_manufacturer", "TEXT DEFAULT ''"),
            ("status", "VARCHAR(32) DEFAULT 'approved'"),
            ("priority", "INTEGER DEFAULT 100"),
            ("created_at", "DATETIME"),
            ("updated_at", "DATETIME"),
            ("comment", "TEXT DEFAULT ''"),
        ],
        "pricing_contexts": [
            ("branch_id", "TEXT DEFAULT ''"),
            ("region", "TEXT DEFAULT ''"),
            ("sales_channel", "TEXT DEFAULT ''"),
            ("name", "TEXT DEFAULT ''"),
            ("is_active", "BOOLEAN DEFAULT TRUE"),
            ("created_at", "DATETIME"),
            ("updated_at", "DATETIME"),
        ],
        "pricing_workflow_runs": [
            ("pricing_context_id", "INTEGER"),
            ("price_format_id", "INTEGER"),
            ("pricing_rule_id", "INTEGER"),
            ("price_list_id", "INTEGER"),
            ("price_list_number", "TEXT DEFAULT ''"),
            ("competitor_sources_json", "TEXT DEFAULT '[]'"),
            ("percentile_sources_json", "TEXT DEFAULT '[]'"),
            ("started_at", "DATETIME"),
            ("finished_at", "DATETIME"),
            ("status", "VARCHAR(32) DEFAULT 'pending'"),
            ("analytics_json", "TEXT DEFAULT '{}'"),
            ("error", "TEXT DEFAULT ''"),
            ("generated_by", "TEXT DEFAULT ''"),
            ("run_sources_json", "TEXT DEFAULT '{}'"),
            ("run_rule_json", "TEXT DEFAULT '{}'"),
            ("run_lists_json", "TEXT DEFAULT '[]'"),
            ("run_reference_versions_json", "TEXT DEFAULT '{}'"),
            ("run_percentile_config_json", "TEXT DEFAULT '{}'"),
            ("run_snapshot_json", "TEXT DEFAULT '{}'"),
        ],
        "price_lists": [
            ("generated_by", "TEXT DEFAULT ''"),
            ("run_sources_json", "TEXT DEFAULT '{}'"),
            ("run_rule_json", "TEXT DEFAULT '{}'"),
            ("run_lists_json", "TEXT DEFAULT '[]'"),
            ("run_reference_versions_json", "TEXT DEFAULT '{}'"),
            ("run_percentile_config_json", "TEXT DEFAULT '{}'"),
            ("run_snapshot_json", "TEXT DEFAULT '{}'"),
        ],
        "reference_update_statuses": [
            ("current_import_status", "VARCHAR(32) DEFAULT ''"),
            ("current_import_started_at", "DATETIME"),
            ("current_import_finished_at", "DATETIME"),
            ("last_successful_import_job_id", "INTEGER"),
            ("active_snapshot_product_count", "INTEGER DEFAULT 0"),
        ],
        "calculated_prices": [
            ("price_from_competitor", "NUMERIC(18, 4)"),
            ("lowest_competitor_price", "NUMERIC(18, 4)"),
            ("chosen_competitor_price", "NUMERIC(18, 4)"),
            ("bend_percent_used", "NUMERIC(18, 6)"),
            ("markup_percent_used", "NUMERIC(18, 6)"),
            ("mdc_markup_percent", "NUMERIC(18, 6)"),
            ("mdc_price", "NUMERIC(18, 4)"),
            ("competitor_candidate_price", "NUMERIC(18, 4)"),
            ("applied_source_name", "TEXT DEFAULT ''"),
            ("applied_source_type", "VARCHAR(64) DEFAULT ''"),
            ("applied_rule_name", "TEXT DEFAULT ''"),
            ("applied_rule_version", "TEXT DEFAULT ''"),
            ("applied_list_ids", "TEXT DEFAULT '[]'"),
            ("applied_rule_type", "VARCHAR(64) DEFAULT ''"),
            ("applied_rule_value", "NUMERIC(18, 6)"),
            ("applied_list_id", "INTEGER"),
            ("used_substitute", "BOOLEAN DEFAULT FALSE"),
            ("used_percentile", "BOOLEAN DEFAULT FALSE"),
            ("rating_global", "INTEGER"),
            ("rating_local", "INTEGER"),
        ],
        "competitor_code_mappings": [
            ("platform", "VARCHAR(32)"),
            ("source_external_key", "TEXT"),
            ("source_match_key", "TEXT DEFAULT ''"),
            ("source_name", "TEXT DEFAULT ''"),
            ("source_manufacturer", "TEXT DEFAULT ''"),
            ("source_dosage_form", "TEXT DEFAULT ''"),
            ("source_normalized_name", "TEXT DEFAULT ''"),
            ("our_product_id", "INTEGER"),
            ("our_sku", "TEXT DEFAULT ''"),
            ("status", "VARCHAR(32) DEFAULT 'unmapped'"),
            ("confidence", "NUMERIC(18, 4)"),
            ("created_at", "DATETIME"),
            ("updated_at", "DATETIME"),
            ("approved_at", "DATETIME"),
            ("created_by", "TEXT DEFAULT ''"),
        ],
        "app_users": [
            ("username", "TEXT"),
            ("display_name", "TEXT DEFAULT ''"),
            ("role", "VARCHAR(32) DEFAULT 'admin'"),
            ("is_active", "BOOLEAN DEFAULT TRUE"),
            ("created_at", "DATETIME"),
            ("updated_at", "DATETIME"),
        ],
        "user_branch_assignments": [
            ("user_id", "INTEGER"),
            ("branch_id", "TEXT DEFAULT ''"),
            ("branch_name", "TEXT DEFAULT ''"),
            ("created_at", "DATETIME"),
        ],
        "price_format_competitor_assignments": [
            ("price_format_id", "INTEGER"),
            ("competitor_price_list_id", "INTEGER"),
            ("is_active", "BOOLEAN DEFAULT TRUE"),
            ("coefficient", "NUMERIC(18, 6) DEFAULT 1.0"),
            ("percentile_mode", "VARCHAR(32) DEFAULT ''"),
            ("source_mode", "VARCHAR(32) DEFAULT ''"),
            ("created_at", "DATETIME"),
            ("updated_at", "DATETIME"),
        ],
    }
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, columns in additions.items():
            if not inspector.has_table(table):
                continue
            existing = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl in columns:
                if name not in existing:
                    if engine.dialect.name == "postgresql":
                        ddl_for_dialect = ddl.replace("DATETIME", "TIMESTAMP")
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl_for_dialect}"))
                    else:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _ensure_compatible_column_types() -> None:
    """Widen external numeric identifiers for PostgreSQL.

    SQLite stores large integers dynamically, but PostgreSQL INTEGER is int32.
    Provisor/Vidman external ids can exceed that range during refresh/import.
    """

    if engine.dialect.name != "postgresql":
        return

    bigint_columns = {
        "products": ("provisor_goods_id",),
        "competitor_price_list_items": ("provisor_id", "provisor_goods_id", "filial_id"),
        "competitors_prices": ("source_goods_id",),
        "provisor_goods_map": ("goods_id",),
        "source_goods_matches": ("goods_id",),
        "product_substitute_matches": ("source_goods_id",),
    }
    text_columns = {
        "products": ("code", "name"),
        "product_extras": ("manufacturer",),
        "price_formats": ("code", "name", "branch", "pricing_rule", "pricing_rule_applied_tables_json"),
        "price_lists": ("number", "user", "status"),
        "competitors_prices": ("source_name", "supplier", "source_distributor_goods_id", "source_manufacturer"),
        "competitor_price_lists": (
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
            "source_updated_at",
        ),
        "competitor_price_list_items": (
            "name",
            "reg_number",
            "distributor_goods_name",
            "distributor_goods_id",
            "expiry_date",
            "match_key",
            "matched_sku",
            "raw_name",
            "raw_manufacturer",
            "normalized_name",
            "normalized_manufacturer",
            "parsed_base_name",
            "parsed_form",
            "parsed_forms_json",
            "parsed_strength_signature",
            "parsed_dimensions_json",
            "parsed_critical_tokens_json",
        ),
        "competitor_price_percentiles": ("branch_name", "competitor_name", "percentile_scope"),
        "price_source_accounts": ("login", "status_message"),
        "jobs": ("format_code", "message"),
        "source_goods_matches": ("distributor_goods_id", "distributor_goods_name", "distributor_producer", "match_method"),
        "product_substitute_matches": (
            "source_distributor_goods_id",
            "source_name",
            "source_manufacturer",
            "comment",
        ),
        "pricing_contexts": ("branch_id", "region", "sales_channel", "name"),
        "pricing_workflow_runs": (
            "price_list_number",
            "competitor_sources_json",
            "percentile_sources_json",
            "analytics_json",
            "error",
            "generated_by",
            "run_sources_json",
            "run_rule_json",
            "run_lists_json",
            "run_reference_versions_json",
            "run_percentile_config_json",
            "run_snapshot_json",
        ),
        "price_lists": (
            "number",
            "user",
            "status",
            "generated_by",
            "run_sources_json",
            "run_rule_json",
            "run_lists_json",
            "run_reference_versions_json",
            "run_percentile_config_json",
            "run_snapshot_json",
        ),
        "reference_update_statuses": (
            "current_import_status",
            "last_successful_import_job_id",
        ),
        "universal_lists": ("code", "name", "status", "type"),
        "calculated_prices": (
            "applied_reason",
            "applied_source_name",
            "applied_rule_name",
            "applied_rule_version",
            "applied_list_ids",
            "applied_rule_type",
        ),
        "competitor_code_mappings": (
            "platform",
            "source_external_key",
            "source_match_key",
            "source_name",
            "source_manufacturer",
            "source_dosage_form",
            "source_normalized_name",
            "our_sku",
            "created_by",
        ),
        "app_users": ("username", "display_name", "role"),
        "user_branch_assignments": ("branch_id", "branch_name"),
    }
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, columns in bigint_columns.items():
            if not inspector.has_table(table):
                continue
            existing = {col["name"]: str(col.get("type", "")).upper() for col in inspector.get_columns(table)}
            for column in columns:
                if column not in existing or "BIGINT" in existing[column]:
                    continue
                conn.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE BIGINT USING "{column}"::bigint'))
        for table, columns in text_columns.items():
            if not inspector.has_table(table):
                continue
            existing = {col["name"]: str(col.get("type", "")).upper() for col in inspector.get_columns(table)}
            for column in columns:
                if column not in existing or existing[column] == "TEXT":
                    continue
                conn.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE TEXT'))


def _ensure_nullable_percentile_values() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("competitor_price_percentiles"):
        return

    columns = inspector.get_columns("competitor_price_percentiles")
    value_column = next((col for col in columns if col["name"] == "value"), None)
    if value_column is None or not value_column.get("nullable") is False:
        return

    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text('ALTER TABLE competitor_price_percentiles ALTER COLUMN value DROP NOT NULL'))
            return

        if engine.dialect.name != "sqlite":
            return

        conn.execute(text("ALTER TABLE competitor_price_percentiles RENAME TO competitor_price_percentiles_old"))
        conn.execute(
            text(
                """
                CREATE TABLE competitor_price_percentiles (
                    id INTEGER NOT NULL PRIMARY KEY,
                    price_format_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    branch_name TEXT NOT NULL,
                    competitor_name TEXT NOT NULL,
                    percentile_scope VARCHAR(32) NOT NULL DEFAULT 'regional',
                    percentile INTEGER NOT NULL,
                    value NUMERIC(18, 4),
                    source_count INTEGER NOT NULL,
                    updated_at DATETIME NOT NULL,
                    FOREIGN KEY(price_format_id) REFERENCES price_formats (id),
                    FOREIGN KEY(product_id) REFERENCES products (id),
                    CONSTRAINT uq_comp_percentile_scope UNIQUE (
                        price_format_id,
                        product_id,
                        branch_name,
                        competitor_name,
                        percentile_scope,
                        percentile
                    )
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO competitor_price_percentiles (
                    id,
                    price_format_id,
                    product_id,
                    branch_name,
                    competitor_name,
                    percentile_scope,
                    percentile,
                    value,
                    source_count,
                    updated_at
                )
                SELECT
                    id,
                    price_format_id,
                    product_id,
                    branch_name,
                    competitor_name,
                    COALESCE(percentile_scope, 'regional'),
                    percentile,
                    value,
                    source_count,
                    updated_at
                FROM competitor_price_percentiles_old
                """
            )
        )
        conn.execute(text("DROP TABLE competitor_price_percentiles_old"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitor_price_percentiles_price_format_id ON competitor_price_percentiles (price_format_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitor_price_percentiles_product_id ON competitor_price_percentiles (product_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitor_price_percentiles_branch_name ON competitor_price_percentiles (branch_name)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitor_price_percentiles_competitor_name ON competitor_price_percentiles (competitor_name)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitor_price_percentiles_percentile_scope ON competitor_price_percentiles (percentile_scope)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitor_price_percentiles_percentile ON competitor_price_percentiles (percentile)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_comp_percentile_lookup ON competitor_price_percentiles (price_format_id, product_id, percentile_scope, percentile)"))


def _ensure_percentile_source_identity() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("competitor_price_percentiles"):
        return

    columns = {col["name"] for col in inspector.get_columns("competitor_price_percentiles")}
    required = {"competitor_price_list_id", "source_type", "source_key"}
    missing = required - columns
    if missing:
        return

    unique_constraints = inspector.get_unique_constraints("competitor_price_percentiles")
    current = next((constraint for constraint in unique_constraints if constraint.get("name") == "uq_comp_percentile_scope"), None)
    current_columns = tuple((current or {}).get("column_names") or ())
    desired_columns = (
        "price_format_id",
        "product_id",
        "source_key",
        "branch_name",
        "competitor_name",
        "percentile_scope",
        "percentile",
    )
    if current_columns == desired_columns:
        return

    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("ALTER TABLE competitor_price_percentiles DROP CONSTRAINT IF EXISTS uq_comp_percentile_scope"))
            conn.execute(
                text(
                    """
                    ALTER TABLE competitor_price_percentiles
                    ADD CONSTRAINT uq_comp_percentile_scope UNIQUE (
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
            return

        if engine.dialect.name != "sqlite":
            return

        conn.execute(text("ALTER TABLE competitor_price_percentiles RENAME TO competitor_price_percentiles_old"))
        conn.execute(
            text(
                """
                CREATE TABLE competitor_price_percentiles (
                    id INTEGER NOT NULL PRIMARY KEY,
                    price_format_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    competitor_price_list_id INTEGER,
                    source_type VARCHAR(32) NOT NULL DEFAULT '',
                    source_key TEXT NOT NULL DEFAULT '',
                    branch_name TEXT NOT NULL,
                    competitor_name TEXT NOT NULL,
                    percentile_scope VARCHAR(32) NOT NULL DEFAULT 'regional',
                    percentile INTEGER NOT NULL,
                    value NUMERIC(18, 4),
                    source_count INTEGER NOT NULL,
                    price_count INTEGER NOT NULL DEFAULT 0,
                    used_price_count INTEGER NOT NULL DEFAULT 0,
                    status VARCHAR(64) NOT NULL DEFAULT '',
                    updated_at DATETIME NOT NULL,
                    FOREIGN KEY(price_format_id) REFERENCES price_formats (id),
                    FOREIGN KEY(product_id) REFERENCES products (id),
                    FOREIGN KEY(competitor_price_list_id) REFERENCES competitor_price_lists (id),
                    CONSTRAINT uq_comp_percentile_scope UNIQUE (
                        price_format_id,
                        product_id,
                        source_key,
                        branch_name,
                        competitor_name,
                        percentile_scope,
                        percentile
                    )
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT OR IGNORE INTO competitor_price_percentiles (
                    id,
                    price_format_id,
                    product_id,
                    competitor_price_list_id,
                    source_type,
                    source_key,
                    branch_name,
                    competitor_name,
                    percentile_scope,
                    percentile,
                    value,
                    source_count,
                    price_count,
                    used_price_count,
                    status,
                    updated_at
                )
                SELECT
                    id,
                    price_format_id,
                    product_id,
                    competitor_price_list_id,
                    COALESCE(source_type, ''),
                    COALESCE(source_key, ''),
                    branch_name,
                    competitor_name,
                    COALESCE(percentile_scope, 'regional'),
                    percentile,
                    value,
                    source_count,
                    COALESCE(price_count, 0),
                    COALESCE(used_price_count, COALESCE(price_count, source_count, 0)),
                    COALESCE(status, ''),
                    updated_at
                FROM competitor_price_percentiles_old
                """
            )
        )
        conn.execute(text("DROP TABLE competitor_price_percentiles_old"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitor_price_percentiles_price_format_id ON competitor_price_percentiles (price_format_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitor_price_percentiles_product_id ON competitor_price_percentiles (product_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitor_price_percentiles_competitor_price_list_id ON competitor_price_percentiles (competitor_price_list_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitor_price_percentiles_source_type ON competitor_price_percentiles (source_type)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitor_price_percentiles_source_key ON competitor_price_percentiles (source_key)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitor_price_percentiles_branch_name ON competitor_price_percentiles (branch_name)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitor_price_percentiles_competitor_name ON competitor_price_percentiles (competitor_name)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitor_price_percentiles_percentile_scope ON competitor_price_percentiles (percentile_scope)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitor_price_percentiles_percentile ON competitor_price_percentiles (percentile)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_comp_percentile_lookup ON competitor_price_percentiles (price_format_id, product_id, source_key, percentile_scope, percentile)"))


def _backfill_percentile_source_identity() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("competitor_price_percentiles") or not inspector.has_table("competitor_price_lists"):
        return
    columns = {col["name"] for col in inspector.get_columns("competitor_price_percentiles")}
    required = {"competitor_price_list_id", "source_type", "source_key", "price_format_id", "branch_name", "competitor_name", "percentile_scope"}
    if required - columns:
        return

    with engine.begin() as conn:
        legacy_before = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM competitor_price_percentiles
                    WHERE COALESCE(source_key, '') = ''
                    """
                )
            ).scalar()
            or 0
        )
        regional_result = conn.execute(
            text(
                """
                UPDATE competitor_price_percentiles AS p
                SET
                    competitor_price_list_id = (
                        SELECT MIN(c.id)
                        FROM competitor_price_lists AS c
                        WHERE c.price_format_id = p.price_format_id
                          AND c.branch_name = p.branch_name
                          AND c.competitor_name = p.competitor_name
                          AND COALESCE(c.source_key, '') <> ''
                    ),
                    source_key = (
                        SELECT MIN(c.source_key)
                        FROM competitor_price_lists AS c
                        WHERE c.price_format_id = p.price_format_id
                          AND c.branch_name = p.branch_name
                          AND c.competitor_name = p.competitor_name
                          AND COALESCE(c.source_key, '') <> ''
                    ),
                    source_type = (
                        SELECT CASE
                            WHEN MIN(c.source_key) LIKE 'emit:%' THEN 'emit'
                            ELSE COALESCE(MIN(c.source_type), '')
                        END
                        FROM competitor_price_lists AS c
                        WHERE c.price_format_id = p.price_format_id
                          AND c.branch_name = p.branch_name
                          AND c.competitor_name = p.competitor_name
                          AND COALESCE(c.source_key, '') <> ''
                    )
                WHERE COALESCE(p.source_key, '') = ''
                  AND COALESCE(p.percentile_scope, 'regional') = 'regional'
                  AND (
                    SELECT COUNT(*)
                    FROM competitor_price_lists AS c
                    WHERE c.price_format_id = p.price_format_id
                      AND c.branch_name = p.branch_name
                      AND c.competitor_name = p.competitor_name
                      AND COALESCE(c.source_key, '') <> ''
                  ) = 1
                """
            )
        )
        kazakhstan_result = conn.execute(
            text(
                """
                UPDATE competitor_price_percentiles
                SET
                    source_type = 'emit',
                    source_key = 'emit:kazakhstan:' || COALESCE(competitor_name, '')
                WHERE COALESCE(source_key, '') = ''
                  AND COALESCE(percentile_scope, '') = 'kazakhstan'
                """
            )
        )
        legacy_after = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM competitor_price_percentiles
                    WHERE COALESCE(source_key, '') = ''
                    """
                )
            ).scalar()
            or 0
        )
    if legacy_before or int(regional_result.rowcount or 0) or int(kazakhstan_result.rowcount or 0):
        logger.info(
            "[EMIT_PERCENTILE_INVENTORY] stage=schema_backfill legacy_before=%s regional_backfilled=%s "
            "kazakhstan_backfilled=%s legacy_remaining=%s ambiguous_or_unmatched=%s",
            legacy_before,
            int(regional_result.rowcount or 0),
            int(kazakhstan_result.rowcount or 0),
            legacy_after,
            legacy_after,
        )


def _ensure_compatible_indexes() -> None:
    indexes = [
        (
            "ix_competitor_price_list_items_pl_product",
            "competitor_price_list_items",
            ("price_list_id", "product_id"),
        ),
        (
            "ix_competitor_price_list_items_pl_match_key",
            "competitor_price_list_items",
            ("price_list_id", "match_key"),
        ),
        (
            "ix_competitor_price_list_items_pl_matched_sku",
            "competitor_price_list_items",
            ("price_list_id", "matched_sku"),
        ),
        (
            "ix_competitor_price_list_items_pl_provisor_goods",
            "competitor_price_list_items",
            ("price_list_id", "provisor_goods_id"),
        ),
        (
            "ix_comp_percentile_bulk_generation",
            "competitor_price_percentiles",
            ("price_format_id", "percentile_scope", "percentile", "product_id"),
        ),
        (
            "ix_comp_percentile_source_lookup",
            "competitor_price_percentiles",
            ("price_format_id", "source_key", "percentile_scope", "percentile", "product_id"),
        ),
        (
            "ix_comp_percentile_group_lookup",
            "competitor_price_percentiles",
            ("price_format_id", "source_key", "branch_name", "competitor_name", "percentile_scope", "percentile", "product_id"),
        ),
        (
            "ix_product_substitute_lookup",
            "product_substitute_matches",
            ("product_id", "source_type", "status", "priority"),
        ),
        (
            "ix_pricing_context_active_scope",
            "pricing_contexts",
            ("is_active", "branch_id", "region", "sales_channel"),
        ),
        (
            "ix_pricing_workflow_runs_context_status",
            "pricing_workflow_runs",
            ("pricing_context_id", "status"),
        ),
        (
            "ix_pricing_workflow_runs_price_list",
            "pricing_workflow_runs",
            ("price_list_number",),
        ),
        (
            "ix_competitor_code_mappings_platform_status",
            "competitor_code_mappings",
            ("platform", "status"),
        ),
        (
            "ix_competitor_code_mappings_platform_key",
            "competitor_code_mappings",
            ("platform", "source_match_key"),
        ),
        (
            "ix_app_users_username",
            "app_users",
            ("username",),
        ),
        (
            "ix_user_branch_assignments_branch",
            "user_branch_assignments",
            ("branch_id", "branch_name"),
        ),
        (
            "ix_pf_comp_assign_pf_active",
            "price_format_competitor_assignments",
            ("price_format_id", "is_active"),
        ),
        (
            "ix_pf_comp_assign_list",
            "price_format_competitor_assignments",
            ("competitor_price_list_id",),
        ),
        (
            "ux_pf_comp_assign_pair",
            "price_format_competitor_assignments",
            ("price_format_id", "competitor_price_list_id"),
        ),
        (
            "ix_refresh_jobs_source_status",
            "refresh_jobs",
            ("source_type", "status", "started_at"),
        ),
        (
            "ix_refresh_locks_owner_token",
            "refresh_locks",
            ("owner_token",),
        ),
        (
            "ix_refresh_locks_type",
            "refresh_locks",
            ("lock_type",),
        ),
        (
            "ix_refresh_locks_lease_until",
            "refresh_locks",
            ("lease_until",),
        ),
    ]
    inspector = inspect(engine)
    with engine.begin() as conn:
        for name, table, columns in indexes:
            if not inspector.has_table(table):
                continue
            existing = {idx["name"] for idx in inspector.get_indexes(table)}
            if name in existing:
                continue
            columns_sql = ", ".join(columns)
            unique = "UNIQUE " if name.startswith("ux_") else ""
            conn.execute(text(f"CREATE {unique}INDEX IF NOT EXISTS {name} ON {table} ({columns_sql})"))


def _backfill_competitor_assignments() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("price_format_competitor_assignments") or not inspector.has_table("competitor_price_lists"):
        return
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(
                text(
                    """
                    INSERT INTO price_format_competitor_assignments
                        (price_format_id, competitor_price_list_id, is_active, coefficient, percentile_mode, source_mode, created_at, updated_at)
                    SELECT
                        c.price_format_id,
                        c.id,
                        COALESCE(c.is_selected, FALSE),
                        COALESCE(c.coefficient, 1.0),
                        '',
                        '',
                        COALESCE(c.created_at, CURRENT_TIMESTAMP),
                        COALESCE(c.updated_at, CURRENT_TIMESTAMP)
                    FROM competitor_price_lists c
                    WHERE c.price_format_id IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1
                        FROM price_format_competitor_assignments a
                        WHERE a.price_format_id = c.price_format_id
                          AND a.competitor_price_list_id = c.id
                      )
                    """
                )
            )
        else:
            conn.execute(
                text(
                    """
                    INSERT INTO price_format_competitor_assignments
                        (price_format_id, competitor_price_list_id, is_active, coefficient, percentile_mode, source_mode, created_at, updated_at)
                    SELECT
                        c.price_format_id,
                        c.id,
                        COALESCE(c.is_selected, 0),
                        COALESCE(c.coefficient, 1.0),
                        '',
                        '',
                        COALESCE(c.created_at, CURRENT_TIMESTAMP),
                        COALESCE(c.updated_at, CURRENT_TIMESTAMP)
                    FROM competitor_price_lists c
                    WHERE c.price_format_id IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1
                        FROM price_format_competitor_assignments a
                        WHERE a.price_format_id = c.price_format_id
                          AND a.competitor_price_list_id = c.id
                      )
                    """
                )
            )


def _backfill_competitor_price_coefficients() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("competitor_price_lists"):
        return
    columns = {col["name"] for col in inspector.get_columns("competitor_price_lists")}
    if "price_coefficient" not in columns:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE competitor_price_lists
                SET price_coefficient = COALESCE(NULLIF(coefficient, 0), 1.0)
                WHERE (price_coefficient IS NULL OR price_coefficient = 1.0)
                  AND coefficient IS NOT NULL
                  AND coefficient > 0
                  AND coefficient != 1.0
                """
            )
        )
        if inspector.has_table("price_format_competitor_assignments"):
            if engine.dialect.name == "postgresql":
                conn.execute(
                    text(
                        """
                        UPDATE competitor_price_lists c
                        SET price_coefficient = a.coefficient
                        FROM price_format_competitor_assignments a
                        WHERE a.competitor_price_list_id = c.id
                          AND a.is_active = TRUE
                          AND a.coefficient IS NOT NULL
                          AND a.coefficient > 0
                          AND a.coefficient != 1.0
                          AND (c.price_coefficient IS NULL OR c.price_coefficient = 1.0)
                        """
                    )
                )
            else:
                conn.execute(
                    text(
                        """
                        UPDATE competitor_price_lists
                        SET price_coefficient = (
                            SELECT a.coefficient
                            FROM price_format_competitor_assignments a
                            WHERE a.competitor_price_list_id = competitor_price_lists.id
                              AND a.is_active = 1
                              AND a.coefficient IS NOT NULL
                              AND a.coefficient > 0
                              AND a.coefficient != 1.0
                            ORDER BY a.updated_at DESC, a.id DESC
                            LIMIT 1
                        )
                        WHERE (price_coefficient IS NULL OR price_coefficient = 1.0)
                          AND EXISTS (
                            SELECT 1
                            FROM price_format_competitor_assignments a
                            WHERE a.competitor_price_list_id = competitor_price_lists.id
                              AND a.is_active = 1
                              AND a.coefficient IS NOT NULL
                              AND a.coefficient > 0
                              AND a.coefficient != 1.0
                          )
                        """
                    )
                )
