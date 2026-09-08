from __future__ import annotations

from datetime import datetime, date
from sqlalchemy import (
    String,
    Integer,
    BigInteger,
    DateTime,
    Date,
    ForeignKey,
    Numeric,
    UniqueConstraint,
    Index,
    Boolean,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .timezone import now_kz_naive


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True, index=True)
    name: Mapped[str] = mapped_column(Text)
    cost: Mapped[float] = mapped_column(Numeric(18, 4), default=0)
    top_rank: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    provisor_goods_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)


class ProductExtra(Base):
    __tablename__ = "product_extras"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    stock: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    manufacturer: Mapped[str] = mapped_column(Text, default="")

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)


class InternalProductNormalized(Base):
    __tablename__ = "internal_product_normalized"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), unique=True, index=True)
    raw_name: Mapped[str] = mapped_column(Text, default="")
    raw_manufacturer: Mapped[str] = mapped_column(Text, default="")
    normalized_name: Mapped[str] = mapped_column(Text, default="")
    base_name: Mapped[str] = mapped_column(Text, default="")
    normalized_manufacturer: Mapped[str] = mapped_column(Text, default="")
    dosage_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    dosage_unit: Mapped[str] = mapped_column(String(32), default="")
    strength_components: Mapped[str] = mapped_column(Text, default="")
    concentration_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    concentration_unit: Mapped[str] = mapped_column(String(64), default="")
    volume_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume_unit: Mapped[str] = mapped_column(String(32), default="")
    weight_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    weight_unit: Mapped[str] = mapped_column(String(32), default="")
    package_volume: Mapped[str] = mapped_column(Text, default="")
    package_weight: Mapped[str] = mapped_column(Text, default="")
    pack_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dosage_form: Mapped[str] = mapped_column(String(64), default="")
    variant_text: Mapped[str] = mapped_column(Text, default="")
    identity_tokens_json: Mapped[str] = mapped_column(Text, default="[]")
    normalized_signature: Mapped[str] = mapped_column(Text, default="")
    parse_warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    parse_confidence: Mapped[float] = mapped_column(Numeric(18, 4), default=0)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    normalized_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        Index("ix_internal_product_normalized_signature", "normalized_signature"),
        Index("ix_internal_product_normalized_base_name", "base_name"),
        Index("ix_internal_product_normalized_manufacturer", "normalized_manufacturer"),
    )


class PriceFormat(Base):
    __tablename__ = "price_formats"
    __table_args__ = (
        Index(
            "uq_price_formats_sap_branch_sequence",
            "sap_branch_code",
            "sequence_number",
            unique=True,
            postgresql_where=text("sap_branch_code IS NOT NULL AND sequence_number IS NOT NULL"),
            sqlite_where=text("sap_branch_code IS NOT NULL AND sequence_number IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True, index=True)
    name: Mapped[str] = mapped_column(Text)
    branch: Mapped[str] = mapped_column(Text, default="")
    reference_branch_id: Mapped[str | None] = mapped_column(Text, nullable=True, default="", server_default=text("''"))
    sap_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price_list_type: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    sap_branch_code: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    sequence_number: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    pricing_rule: Mapped[str] = mapped_column(Text, default="")
    pricing_rule_id: Mapped[int | None] = mapped_column(ForeignKey("pricing_rules.id"), nullable=True, index=True)
    rounding_rule_id: Mapped[int | None] = mapped_column(ForeignKey("rounding_rules.id"), nullable=True, index=True)
    applied_markup_template_id: Mapped[int | None] = mapped_column(ForeignKey("markup_templates.id"), nullable=True, index=True)
    applied_bend_template_id: Mapped[int | None] = mapped_column(ForeignKey("bend_templates.id"), nullable=True, index=True)
    applied_no_competitor_template_id: Mapped[int | None] = mapped_column(ForeignKey("no_competitor_markup_templates.id"), nullable=True, index=True)
    pricing_rule_applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pricing_rule_applied_tables_json: Mapped[str] = mapped_column(Text, default="[]")
    competitor_price_mode: Mapped[str] = mapped_column(String(32), default="regular")
    percentile_number: Mapped[int] = mapped_column(Integer, default=10)
    # MVP: "Прогиб" (bend/undercut) — значение В ПРОЦЕНТАХ,
    # на сколько цена должна быть ниже МЦК (минимальной цены конкурента).
    progib: Mapped[float] = mapped_column(Numeric(18, 4), default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    markup_ranges: Mapped[list[MarkupRange]] = relationship(
        back_populates="price_format", cascade="all, delete-orphan"
    )


class BranchSapMapping(Base):
    __tablename__ = "branch_sap_mappings"

    canonical_branch_key: Mapped[str] = mapped_column(Text, primary_key=True)
    branch_name: Mapped[str] = mapped_column(Text)
    sap_branch_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)


class PriceFormatBranchCounter(Base):
    __tablename__ = "price_format_branch_counters"

    sap_branch_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    last_sequence: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)


class PricingContext(Base):
    __tablename__ = "pricing_contexts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[str] = mapped_column(Text, default="", index=True)
    region: Mapped[str] = mapped_column(Text, default="", index=True)
    sales_channel: Mapped[str] = mapped_column(Text, default="", index=True)
    name: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint("branch_id", "region", "sales_channel", "name", name="uq_pricing_context_scope"),
        Index("ix_pricing_context_active_scope", "is_active", "branch_id", "region", "sales_channel"),
    )


class AppUser(Base):
    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text, default="", server_default=text("''"), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[str] = mapped_column(String(32), default="admin", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    branches: Mapped[list["UserBranchAssignment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AppSession(Base):
    __tablename__ = "app_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive, server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive, server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    user: Mapped[AppUser] = relationship()

    __table_args__ = (
        Index("ix_app_sessions_session_token_hash", "session_token_hash"),
        Index("ix_app_sessions_active_lookup", "session_token_hash", "expires_at", "revoked_at"),
    )


class UserBranchAssignment(Base):
    __tablename__ = "user_branch_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id"), index=True)
    branch_id: Mapped[str] = mapped_column(Text, default="", index=True)
    branch_name: Mapped[str] = mapped_column(Text, default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    user: Mapped[AppUser] = relationship(back_populates="branches")

    __table_args__ = (
        UniqueConstraint("user_id", "branch_id", name="uq_user_branch_assignment"),
    )


class MarkupRange(Base):
    __tablename__ = "markup_ranges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)

    cost_from: Mapped[float] = mapped_column(Numeric(18, 4))
    cost_to: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    markup_percent: Mapped[float] = mapped_column(Numeric(18, 4))  # 0.10 == 10%

    price_format: Mapped[PriceFormat] = relationship(back_populates="markup_ranges")

    __table_args__ = (
        Index("ix_markup_ranges_pf_from_to", "price_format_id", "cost_from", "cost_to"),
    )


class BendRange(Base):
    __tablename__ = "bend_ranges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)

    # Step function: pick the row with the largest price_from <= competitor price.
    price_from: Mapped[float] = mapped_column(Numeric(18, 4))
    bend_percent: Mapped[float] = mapped_column(Numeric(18, 6))  # value in percent points (e.g. 0.30 == 0.30%)

    __table_args__ = (
        Index("ix_bend_ranges_pf_from", "price_format_id", "price_from"),
    )


class PriceList(Base):
    __tablename__ = "price_lists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[str] = mapped_column(Text, unique=True, index=True)

    price_format_id: Mapped[int | None] = mapped_column(ForeignKey("price_formats.id"), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    activation_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    user: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="Черновик")
    generated_by: Mapped[str] = mapped_column(Text, default="")
    run_sources_json: Mapped[str] = mapped_column(Text, default="{}")
    run_rule_json: Mapped[str] = mapped_column(Text, default="{}")
    run_lists_json: Mapped[str] = mapped_column(Text, default="[]")
    run_reference_versions_json: Mapped[str] = mapped_column(Text, default="{}")
    run_percentile_config_json: Mapped[str] = mapped_column(Text, default="{}")
    run_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")


class CompetitorPrice(Base):
    __tablename__ = "competitors_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True, index=True)

    source_name: Mapped[str] = mapped_column(Text)
    supplier: Mapped[str] = mapped_column(Text, default="")
    price_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    coefficient: Mapped[float] = mapped_column(Numeric(18, 6), default=1.0)
    source_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    match_type: Mapped[str] = mapped_column(String(64), default="")
    source_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source_goods_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    source_distributor_goods_id: Mapped[str] = mapped_column(Text, default="")
    source_manufacturer: Mapped[str] = mapped_column(Text, default="")

    # Если product_id is NULL, то это запись-настройка источника (коэффициент и т.п.)

    __table_args__ = (
        Index(
            "ix_competitors_prices_pf_source_product",
            "price_format_id",
            "source_name",
            "product_id",
        ),
    )


class CompetitorPriceList(Base):
    __tablename__ = "competitor_price_lists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Legacy owner from the old format-centric architecture. Competitor price
    # lists are now treated as a global pool; per-format usage lives in
    # PriceFormatCompetitorAssignment below.
    price_format_id: Mapped[int | None] = mapped_column(ForeignKey("price_formats.id"), nullable=True, index=True)

    source_type: Mapped[str] = mapped_column(String(64), index=True)  # provisor|phcenter|vidman|manual
    source_key: Mapped[str] = mapped_column(Text, index=True)
    display_name: Mapped[str] = mapped_column(Text, default="")
    supplier: Mapped[str] = mapped_column(Text, default="")
    region: Mapped[str] = mapped_column(Text, default="")
    branch_id: Mapped[str] = mapped_column(Text, default="")
    branch_code: Mapped[str] = mapped_column(Text, default="")
    branch_name: Mapped[str] = mapped_column(Text, default="Без филиала")
    competitor_name: Mapped[str] = mapped_column(Text, default="")
    account_id: Mapped[str] = mapped_column(Text, default="")
    account_login: Mapped[str] = mapped_column(Text, default="")
    external_price_list_id: Mapped[str] = mapped_column(Text, default="")
    sync_batch_id: Mapped[str] = mapped_column(String(64), default="")
    source_updated_at: Mapped[str] = mapped_column(Text, default="")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_refresh_status: Mapped[str] = mapped_column(String(64), default="")
    last_refresh_message: Mapped[str] = mapped_column(Text, default="")
    price_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    coefficient: Mapped[float] = mapped_column(Numeric(18, 6), default=1.0)
    price_coefficient: Mapped[float] = mapped_column(Numeric(18, 6), default=1.0)
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False)
    items_count: Mapped[int] = mapped_column(Integer, default=0)
    matched_positive_items_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    items: Mapped[list["CompetitorPriceListItem"]] = relationship(
        back_populates="price_list", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("price_format_id", "source_type", "source_key", name="uq_competitor_price_list_source"),
        Index("ix_competitor_price_lists_pf_selected", "price_format_id", "is_selected"),
        Index(
            "ix_competitor_price_lists_provisor_account_external",
            "source_type",
            "account_id",
            "external_price_list_id",
        ),
    )


class PriceFormatCompetitorAssignment(Base):
    __tablename__ = "price_format_competitor_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)
    competitor_price_list_id: Mapped[int] = mapped_column(ForeignKey("competitor_price_lists.id"), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    coefficient: Mapped[float] = mapped_column(Numeric(18, 6), default=1.0)
    percentile_mode: Mapped[str] = mapped_column(String(32), default="")
    source_mode: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint("price_format_id", "competitor_price_list_id", name="uq_pf_competitor_assignment"),
        Index("ix_pf_comp_assign_pf_active", "price_format_id", "is_active"),
    )


class CompetitorPriceListItem(Base):
    __tablename__ = "competitor_price_list_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    price_list_id: Mapped[int] = mapped_column(ForeignKey("competitor_price_lists.id"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True, index=True)

    provisor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    provisor_goods_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    filial_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    name: Mapped[str] = mapped_column(Text, default="")
    reg_number: Mapped[str] = mapped_column(Text, default="")
    distributor_goods_name: Mapped[str] = mapped_column(Text, default="")
    distributor_goods_id: Mapped[str] = mapped_column(Text, default="")
    distributor_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    stock: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    package_count: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    expiry_date: Mapped[str] = mapped_column(Text, default="")
    match_key: Mapped[str] = mapped_column(Text, default="", index=True)
    match_type: Mapped[str] = mapped_column(String(64), default="unmatched")
    match_score: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    matched_sku: Mapped[str] = mapped_column(Text, default="", index=True)
    raw_name: Mapped[str] = mapped_column(Text, default="")
    raw_manufacturer: Mapped[str] = mapped_column(Text, default="")
    normalized_name: Mapped[str] = mapped_column(Text, default="")
    normalized_manufacturer: Mapped[str] = mapped_column(Text, default="")
    parsed_base_name: Mapped[str] = mapped_column(Text, default="")
    parsed_form: Mapped[str] = mapped_column(Text, default="")
    parsed_forms_json: Mapped[str] = mapped_column(Text, default="")
    parsed_dosage: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    parsed_dosage_volume: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    parsed_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parsed_volume: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    parsed_weight: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    parsed_percent_strength: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    parsed_concentration: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    parsed_iu_dosage: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    parsed_strength_signature: Mapped[str] = mapped_column(Text, default="")
    parsed_dimensions_json: Mapped[str] = mapped_column(Text, default="")
    parsed_critical_tokens_json: Mapped[str] = mapped_column(Text, default="")

    raw_json: Mapped[str] = mapped_column(Text, default="")

    price_list: Mapped[CompetitorPriceList] = relationship(back_populates="items")

    __table_args__ = (
        Index("ix_competitor_price_list_items_pl_product", "price_list_id", "product_id"),
        Index("ix_competitor_price_list_items_pl_match_key", "price_list_id", "match_key"),
        Index("ix_competitor_price_list_items_pl_matched_sku", "price_list_id", "matched_sku"),
        Index("ix_competitor_price_list_items_pl_provisor_goods", "price_list_id", "provisor_goods_id"),
    )


class ManualPriceListImport(Base):
    __tablename__ = "manual_price_list_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    competitor_price_list_id: Mapped[int | None] = mapped_column(
        ForeignKey("competitor_price_lists.id"), nullable=True, index=True
    )
    source_key: Mapped[str] = mapped_column(Text, default="", index=True)
    original_filename: Mapped[str] = mapped_column(Text, default="")
    file_type: Mapped[str] = mapped_column(String(16), default="")
    file_checksum: Mapped[str] = mapped_column(String(64), default="", index=True)
    detected_sheet: Mapped[str] = mapped_column(Text, default="")
    detected_delimiter: Mapped[str] = mapped_column(String(8), default="")
    detected_encoding: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="preview", index=True)
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    empty_rows: Mapped[int] = mapped_column(Integer, default=0)
    valid_rows: Mapped[int] = mapped_column(Integer, default=0)
    invalid_rows: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, default=0)
    conflicting_duplicate_skus: Mapped[int] = mapped_column(Integer, default=0)
    matched_rows: Mapped[int] = mapped_column(Integer, default=0)
    unmatched_rows: Mapped[int] = mapped_column(Integer, default=0)
    persisted_rows: Mapped[int] = mapped_column(Integer, default=0)
    preserved_previous_snapshot: Mapped[bool] = mapped_column(Boolean, default=False)
    requested_by: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_summary: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")

    __table_args__ = (
        Index("ix_manual_pl_import_pl_started", "competitor_price_list_id", "started_at"),
    )


class ManualPriceListImportError(Base):
    __tablename__ = "manual_price_list_import_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_id: Mapped[int] = mapped_column(ForeignKey("manual_price_list_imports.id"), index=True)
    row_number: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    field: Mapped[str] = mapped_column(Text, default="")
    raw_value: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str] = mapped_column(String(64), default="", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)


class VidmanAccount(Base):
    __tablename__ = "vidman_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str] = mapped_column(Text, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)


class VidmanPriceList(Base):
    __tablename__ = "vidman_price_lists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("vidman_accounts.id"), index=True)
    main_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(Text, default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    detected_pages: Mapped[int] = mapped_column(Integer, default=0)
    last_collected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint("account_id", "main_id", name="uq_vidman_price_lists_account_main"),
        Index("ix_vidman_price_lists_account_main", "account_id", "main_id"),
    )


class VidmanCompetitorPriceListSource(Base):
    __tablename__ = "vidman_competitor_price_list_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("vidman_accounts.id"), index=True)
    main_id: Mapped[int] = mapped_column(BigInteger, index=True)
    price_format_code: Mapped[str] = mapped_column(Text, default="", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    region: Mapped[str] = mapped_column(Text, default="")
    branch_id: Mapped[str] = mapped_column(Text, default="")
    branch_code: Mapped[str] = mapped_column(Text, default="")
    branch_name: Mapped[str] = mapped_column(Text, default="")
    competitor_name: Mapped[str] = mapped_column(Text, default="")
    price_coefficient: Mapped[float] = mapped_column(Numeric(18, 6), default=1.0)
    last_successful_import_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("vidman_import_runs.id"), nullable=True, index=True
    )
    competitor_price_list_id: Mapped[int | None] = mapped_column(
        ForeignKey("competitor_price_lists.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint("account_id", "main_id", "price_format_code", name="uq_vidman_competitor_plk_source"),
        Index("ix_vidman_competitor_plk_source_active", "is_active", "price_format_code"),
    )


class VidmanLogicalCompetitor(Base):
    __tablename__ = "vidman_logical_competitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, index=True)
    region: Mapped[str] = mapped_column(Text, default="", index=True)
    price_format_id: Mapped[int | None] = mapped_column(ForeignKey("price_formats.id"), nullable=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    collision_status: Mapped[str] = mapped_column(String(64), default="UNRESOLVED", index=True)
    collision_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint("name", "region", "price_format_id", name="uq_vidman_logical_competitor_scope"),
    )


class VidmanLogicalCompetitorSource(Base):
    __tablename__ = "vidman_logical_competitor_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    logical_competitor_id: Mapped[int] = mapped_column(ForeignKey("vidman_logical_competitors.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("vidman_accounts.id"), index=True)
    main_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(32), default="FALLBACK", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    approved_manually: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint("account_id", "main_id", name="uq_vidman_logical_competitor_source_source"),
        Index("ix_vidman_logical_source_competitor_role", "logical_competitor_id", "role", "active"),
    )


class VidmanImportRun(Base):
    __tablename__ = "vidman_import_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("vidman_accounts.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_plks: Mapped[int] = mapped_column(Integer, default=0)
    completed_plks: Mapped[int] = mapped_column(Integer, default=0)
    failed_plks: Mapped[int] = mapped_column(Integer, default=0)
    total_pages: Mapped[int] = mapped_column(Integer, default=0)
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")


class VidmanImportPage(Base):
    __tablename__ = "vidman_import_pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_run_id: Mapped[int] = mapped_column(ForeignKey("vidman_import_runs.id"), index=True)
    price_list_id: Mapped[int] = mapped_column(ForeignKey("vidman_price_lists.id"), index=True)
    main_id: Mapped[int] = mapped_column(BigInteger, index=True)
    page_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    rows_count: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint("import_run_id", "price_list_id", "page_number", name="uq_vidman_import_pages_run_list_page"),
        Index("ix_vidman_import_pages_resume", "import_run_id", "status", "main_id", "page_number"),
    )


class VidmanRawItem(Base):
    __tablename__ = "vidman_raw_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_run_id: Mapped[int] = mapped_column(ForeignKey("vidman_import_runs.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("vidman_accounts.id"), index=True)
    price_list_id: Mapped[int] = mapped_column(ForeignKey("vidman_price_lists.id"), index=True)
    main_id: Mapped[int] = mapped_column(BigInteger, index=True)
    page_number: Mapped[int] = mapped_column(Integer, index=True)
    row_number: Mapped[int] = mapped_column(Integer)
    raw_name: Mapped[str] = mapped_column(Text, default="")
    raw_manufacturer: Mapped[str] = mapped_column(Text, default="")
    raw_expiry_text: Mapped[str] = mapped_column(Text, default="")
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    raw_price_text: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    raw_pack_qty: Mapped[str] = mapped_column(Text, default="")
    pack_qty: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    raw_min_order: Mapped[str] = mapped_column(Text, default="")
    min_order: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    raw_stock: Mapped[str] = mapped_column(Text, default="")
    stock: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    raw_html: Mapped[str] = mapped_column(Text, default="")
    row_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        Index("ix_vidman_raw_items_run_list_page", "import_run_id", "price_list_id", "page_number"),
    )


class VidmanNormalizedItem(Base):
    __tablename__ = "vidman_normalized_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("vidman_raw_items.id"), unique=True, index=True)
    normalized_name: Mapped[str] = mapped_column(Text, default="")
    normalized_manufacturer: Mapped[str] = mapped_column(Text, default="")
    base_name: Mapped[str] = mapped_column(Text, default="", index=True)
    dosage_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    dosage_unit: Mapped[str] = mapped_column(String(32), default="")
    concentration_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    concentration_unit: Mapped[str] = mapped_column(String(32), default="")
    volume_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume_unit: Mapped[str] = mapped_column(String(32), default="")
    weight_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    weight_unit: Mapped[str] = mapped_column(String(32), default="")
    pack_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dosage_form: Mapped[str] = mapped_column(String(64), default="")
    variant_text: Mapped[str] = mapped_column(Text, default="")
    normalized_signature: Mapped[str] = mapped_column(Text, default="", index=True)
    parse_confidence: Mapped[float] = mapped_column(Numeric(18, 4), default=0)
    parse_warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        Index("ix_vidman_normalized_signature", "normalized_signature"),
        Index("ix_vidman_normalized_base_name", "base_name"),
        Index("ix_vidman_normalized_manufacturer", "normalized_manufacturer"),
    )


class VidmanCanonicalProduct(Base):
    __tablename__ = "vidman_canonical_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_name: Mapped[str] = mapped_column(Text, default="")
    canonical_manufacturer: Mapped[str] = mapped_column(Text, default="")
    base_name: Mapped[str] = mapped_column(Text, default="", index=True)
    dosage_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    dosage_unit: Mapped[str] = mapped_column(String(32), default="")
    concentration_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    concentration_unit: Mapped[str] = mapped_column(String(32), default="")
    volume_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume_unit: Mapped[str] = mapped_column(String(32), default="")
    weight_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    weight_unit: Mapped[str] = mapped_column(String(32), default="")
    pack_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dosage_form: Mapped[str] = mapped_column(String(64), default="")
    canonical_signature: Mapped[str] = mapped_column(Text, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    raw_variants_count: Mapped[int] = mapped_column(Integer, default=0)
    accounts_count: Mapped[int] = mapped_column(Integer, default=0)
    plks_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        Index("ix_vidman_canonical_signature", "canonical_signature"),
        Index("ix_vidman_canonical_base_name", "base_name"),
        Index("ix_vidman_canonical_manufacturer", "canonical_manufacturer"),
    )


class VidmanRawCanonicalLink(Base):
    __tablename__ = "vidman_raw_canonical_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("vidman_raw_items.id"), unique=True, index=True)
    normalized_item_id: Mapped[int] = mapped_column(ForeignKey("vidman_normalized_items.id"), index=True)
    canonical_product_id: Mapped[int] = mapped_column(ForeignKey("vidman_canonical_products.id"), index=True)
    match_type: Mapped[str] = mapped_column(String(64), default="")
    confidence: Mapped[float] = mapped_column(Numeric(18, 4), default=0)
    is_auto_linked: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        Index("ix_vidman_raw_canonical_links_canonical", "canonical_product_id"),
    )


class VidmanProductMatch(Base):
    __tablename__ = "vidman_product_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_product_id: Mapped[int] = mapped_column(
        ForeignKey("vidman_canonical_products.id"), unique=True, index=True
    )
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="UNMATCHED", index=True)
    match_type: Mapped[str] = mapped_column(String(64), default="")
    confidence: Mapped[float] = mapped_column(Numeric(18, 4), default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    matched_by: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class VidmanProductMatchCandidate(Base):
    __tablename__ = "vidman_product_match_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_product_id: Mapped[int] = mapped_column(ForeignKey("vidman_canonical_products.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Numeric(18, 4), default=0)
    match_type: Mapped[str] = mapped_column(String(64), default="")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint(
            "canonical_product_id",
            "product_id",
            name="uq_vidman_product_match_candidates_canonical_product",
        ),
        Index("ix_vidman_product_match_candidates_lookup", "canonical_product_id", "rank"),
    )


class VidmanProductReviewQueue(Base):
    __tablename__ = "vidman_product_review_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_product_id: Mapped[int] = mapped_column(
        ForeignKey("vidman_canonical_products.id"), unique=True, index=True
    )
    tier: Mapped[str] = mapped_column(String(32), default="", index=True)
    top_candidate_product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True, index=True)
    top_candidate_score: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    second_candidate_score: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    score_gap: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    shared_structural_fields: Mapped[int] = mapped_column(Integer, default=0)
    hard_conflicts_json: Mapped[str] = mapped_column(Text, default="[]")
    review_reason: Mapped[str] = mapped_column(Text, default="")
    candidate_rankings_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        Index("ix_vidman_review_queue_tier_score", "tier", "top_candidate_score"),
        Index("ix_vidman_review_queue_candidate", "top_candidate_product_id"),
    )


class VidmanRejectedCandidate(Base):
    __tablename__ = "vidman_rejected_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_product_id: Mapped[int] = mapped_column(ForeignKey("vidman_canonical_products.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint(
            "canonical_product_id",
            "product_id",
            name="uq_vidman_rejected_candidates_canonical_product",
        ),
        Index("ix_vidman_rejected_candidates_lookup", "canonical_product_id", "product_id"),
    )


class VidmanProductMatchAudit(Base):
    __tablename__ = "vidman_product_match_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_product_id: Mapped[int] = mapped_column(ForeignKey("vidman_canonical_products.id"), index=True)
    previous_status: Mapped[str] = mapped_column(String(32), default="")
    new_status: Mapped[str] = mapped_column(String(32), default="")
    previous_product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    new_product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64), default="", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        Index("ix_vidman_product_match_audit_canonical_created", "canonical_product_id", "created_at"),
    )


class VidmanInternalCoverageQueue(Base):
    __tablename__ = "vidman_internal_coverage_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), unique=True, index=True)
    tier: Mapped[str] = mapped_column(String(32), default="", index=True)
    top_candidate_canonical_id: Mapped[int | None] = mapped_column(
        ForeignKey("vidman_canonical_products.id"), nullable=True, index=True
    )
    top_candidate_score: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    second_candidate_score: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    score_gap: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    shared_structural_fields: Mapped[int] = mapped_column(Integer, default=0)
    hard_conflicts_json: Mapped[str] = mapped_column(Text, default="[]")
    coverage_reason: Mapped[str] = mapped_column(Text, default="")
    candidate_rankings_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        Index("ix_vidman_internal_coverage_tier_score", "tier", "top_candidate_score"),
        Index("ix_vidman_internal_coverage_candidate", "top_candidate_canonical_id"),
    )


class VidmanInternalCoverageRejection(Base):
    __tablename__ = "vidman_internal_coverage_rejections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    canonical_product_id: Mapped[int] = mapped_column(ForeignKey("vidman_canonical_products.id"), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "canonical_product_id",
            name="uq_vidman_internal_coverage_rejection_product_canonical",
        ),
        Index("ix_vidman_internal_coverage_rejection_lookup", "product_id", "canonical_product_id"),
    )


class VidmanInternalCoverageDecision(Base):
    __tablename__ = "vidman_internal_coverage_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="", index=True)
    canonical_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("vidman_canonical_products.id"), nullable=True, index=True
    )
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)


class VidmanInternalCoverageAudit(Base):
    __tablename__ = "vidman_internal_coverage_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    canonical_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("vidman_canonical_products.id"), nullable=True, index=True
    )
    previous_state: Mapped[str] = mapped_column(String(64), default="")
    new_state: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64), default="", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        Index("ix_vidman_internal_coverage_audit_product_created", "product_id", "created_at"),
    )


class CompetitorPricePercentile(Base):
    __tablename__ = "competitor_price_percentiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    competitor_price_list_id: Mapped[int | None] = mapped_column(ForeignKey("competitor_price_lists.id"), nullable=True, index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="", index=True)
    source_key: Mapped[str] = mapped_column(Text, default="", index=True)

    branch_name: Mapped[str] = mapped_column(Text, default="Без филиала", index=True)
    competitor_name: Mapped[str] = mapped_column(Text, default="", index=True)
    percentile_scope: Mapped[str] = mapped_column(String(32), default="regional", index=True)
    percentile: Mapped[int] = mapped_column(Integer, index=True)
    value: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    price_count: Mapped[int] = mapped_column(Integer, default=0)
    used_price_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint(
            "price_format_id",
            "product_id",
            "source_key",
            "branch_name",
            "competitor_name",
            "percentile_scope",
            "percentile",
            name="uq_comp_percentile_scope",
        ),
        Index("ix_comp_percentile_lookup", "price_format_id", "product_id", "source_key", "percentile_scope", "percentile"),
        Index("ix_comp_percentile_bulk_generation", "price_format_id", "percentile_scope", "percentile", "product_id"),
        Index(
            "ix_comp_percentile_group_lookup",
            "price_format_id",
            "source_key",
            "branch_name",
            "competitor_name",
            "percentile_scope",
            "percentile",
            "product_id",
        ),
    )


class CompetitorPricePercentileSourceSummary(Base):
    __tablename__ = "competitor_price_percentile_source_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="", index=True)
    source_key: Mapped[str] = mapped_column(Text, default="", index=True)
    competitor_price_list_id: Mapped[int | None] = mapped_column(ForeignKey("competitor_price_lists.id"), nullable=True, index=True)
    branch_name: Mapped[str] = mapped_column(Text, default="", index=True)
    competitor_name: Mapped[str] = mapped_column(Text, default="", index=True)
    percentile_scope: Mapped[str] = mapped_column(String(32), default="regional", index=True)
    percentile: Mapped[int] = mapped_column(Integer, index=True)
    sku_count: Mapped[int] = mapped_column(Integer, default=0)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint(
            "price_format_id",
            "source_type",
            "source_key",
            "competitor_price_list_id",
            "branch_name",
            "competitor_name",
            "percentile_scope",
            "percentile",
            name="uq_comp_pct_source_summary",
        ),
        Index(
            "ix_comp_pct_source_summary_lookup",
            "price_format_id",
            "source_key",
            "branch_name",
            "competitor_name",
            "percentile_scope",
            "percentile",
        ),
    )


class RegularCompetitorPricePercentile(Base):
    __tablename__ = "regular_competitor_price_percentiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    competitor_identity: Mapped[str] = mapped_column(Text, index=True)
    competitor_name: Mapped[str] = mapped_column(Text, default="", index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    percentile: Mapped[int] = mapped_column(Integer, index=True)
    value: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    min_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    max_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    algorithm_version: Mapped[str] = mapped_column(String(32), default="percentile_inc_v1")
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint(
            "competitor_identity",
            "product_id",
            "percentile",
            name="uq_regular_comp_percentile_identity_product",
        ),
        Index(
            "ix_regular_comp_percentile_lookup",
            "competitor_identity",
            "product_id",
            "percentile",
        ),
    )


class RegularCompetitorPricePercentileSourceSummary(Base):
    __tablename__ = "regular_competitor_price_percentile_source_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    competitor_identity: Mapped[str] = mapped_column(Text, index=True)
    competitor_name: Mapped[str] = mapped_column(Text, default="", index=True)
    percentile: Mapped[int] = mapped_column(Integer, index=True)
    sku_count: Mapped[int] = mapped_column(Integer, default=0)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint(
            "competitor_identity",
            "percentile",
            name="uq_regular_comp_pct_source_summary",
        ),
        Index(
            "ix_regular_comp_pct_source_summary_lookup",
            "competitor_identity",
            "percentile",
        ),
    )


class PriceSourceAccount(Base):
    __tablename__ = "price_source_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_type: Mapped[str] = mapped_column(String(64), index=True)  # provisor|vidman
    login: Mapped[str] = mapped_column(Text, index=True)
    encrypted_password: Mapped[str] = mapped_column(Text, default="")
    config_json: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[str] = mapped_column(String(64), default="not_checked")
    status_message: Mapped[str] = mapped_column(Text, default="")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    price_lists_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint("source_type", "login", name="uq_price_source_accounts_source_login"),
        Index("ix_price_source_accounts_source_active", "source_type", "is_active"),
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    format_code: Mapped[str] = mapped_column(Text, default="", index=True)
    price_format_id: Mapped[int | None] = mapped_column(ForeignKey("price_formats.id"), nullable=True, index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("price_source_accounts.id"), nullable=True, index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    logs: Mapped[str] = mapped_column(Text, default="[]")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_jobs_type_format_status", "type", "format_code", "status"),
    )


class PriceFormatPercentilePreparation(Base):
    __tablename__ = "price_format_percentile_preparations"

    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="not_configured", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    source_refresh_id: Mapped[str] = mapped_column(Text, default="")
    source_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    configuration_fingerprint: Mapped[str] = mapped_column(Text, default="")
    job_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    rows_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        Index("ix_pf_percentile_prep_status", "status", "updated_at"),
    )


class RefreshJob(Base):
    __tablename__ = "refresh_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    mode: Mapped[str] = mapped_column(String(32), default="selected", index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    requested_by: Mapped[str] = mapped_column(Text, default="")
    total_accounts: Mapped[int] = mapped_column(Integer, default=0)
    processed_accounts: Mapped[int] = mapped_column(Integer, default=0)
    total_plk: Mapped[int] = mapped_column(Integer, default=0)
    processed_plk: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")

    __table_args__ = (
        Index("ix_refresh_jobs_source_status", "source_type", "status", "started_at"),
    )


class RefreshLock(Base):
    __tablename__ = "refresh_locks"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_token: Mapped[str] = mapped_column(String(128), default="", index=True)
    lock_type: Mapped[str] = mapped_column(String(64), default="", index=True)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    lease_until: Mapped[datetime] = mapped_column(DateTime, index=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")


class NoCompetitorMarkupRange(Base):
    __tablename__ = "no_competitor_markup_ranges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)

    cost_from: Mapped[float] = mapped_column(Numeric(18, 4))
    cost_to: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    markup_percent: Mapped[float] = mapped_column(Numeric(18, 4))

    __table_args__ = (
        Index("ix_no_comp_markup_ranges_pf_from_to", "price_format_id", "cost_from", "cost_to"),
    )


class ProvisorGoodsMap(Base):
    __tablename__ = "provisor_goods_map"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)
    # Stable Provisor goodsId (shared across filials/distributors)
    goods_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # Our product (Excel SKU) that corresponds to this goodsId
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint("price_format_id", "goods_id", name="uq_provisor_goods_map_pf_goods"),
        Index("ix_provisor_goods_map_pf_goods_product", "price_format_id", "goods_id", "product_id"),
    )


class ProductSubstituteMatch(Base):
    __tablename__ = "product_substitute_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(64), default="provisor", index=True)
    source_goods_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    source_distributor_goods_id: Mapped[str] = mapped_column(Text, default="", index=True)
    source_name: Mapped[str] = mapped_column(Text, default="")
    source_manufacturer: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="approved", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint("product_id", "source_type", "source_goods_id", name="uq_product_substitute_source_goods"),
        Index("ix_product_substitute_lookup", "product_id", "source_type", "status", "priority"),
    )


class SourceGoodsMatch(Base):
    __tablename__ = "source_goods_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    distributor_goods_id: Mapped[str] = mapped_column(Text, index=True)
    goods_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    distributor_goods_name: Mapped[str] = mapped_column(Text, default="")
    distributor_producer: Mapped[str] = mapped_column(Text, default="")
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    similarity_score: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    match_method: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint(
            "price_format_id",
            "source_type",
            "distributor_goods_id",
            name="uq_source_goods_match_pf_source_sku",
        ),
        Index("ix_source_goods_match_lookup", "price_format_id", "source_type", "distributor_goods_id", "product_id"),
    )


class CompetitorCodeMapping(Base):
    __tablename__ = "competitor_code_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    source_external_key: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    source_match_key: Mapped[str] = mapped_column(Text, default="", index=True)
    source_name: Mapped[str] = mapped_column(Text, default="")
    source_manufacturer: Mapped[str] = mapped_column(Text, default="")
    source_dosage_form: Mapped[str] = mapped_column(Text, default="")
    source_normalized_name: Mapped[str] = mapped_column(Text, default="", index=True)
    our_product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True, index=True)
    our_sku: Mapped[str] = mapped_column(Text, default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default="unmapped", index=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        Index("ix_competitor_code_mappings_platform_status", "platform", "status"),
        Index("ix_competitor_code_mappings_platform_key", "platform", "source_match_key"),
    )


class UniversalList(Base):
    __tablename__ = "universal_lists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    code: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(Text, default="Не активный")
    type: Mapped[str] = mapped_column(Text)

    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Список может быть привязан к ЦФ (если NULL — глобальный)
    price_format_id: Mapped[int | None] = mapped_column(ForeignKey("price_formats.id"), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)


class UniversalListPriceFormat(Base):
    __tablename__ = "universal_list_price_formats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    universal_list_id: Mapped[int] = mapped_column(ForeignKey("universal_lists.id"), index=True)
    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint("universal_list_id", "price_format_id", name="uq_universal_list_price_format"),
        Index("ix_universal_list_price_format_pf", "price_format_id", "universal_list_id"),
    )


class ListItem(Base):
    __tablename__ = "list_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    universal_list_id: Mapped[int] = mapped_column(ForeignKey("universal_lists.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)

    # Значение параметра зависит от типа списка:
    # - фикс цена: fixed price
    # - макс наценка: max markup percent (0.20 == 20%)
    # - мин цена: min price
    # - гос цена: government price
    # - фикс наценка: fixed markup percent (0.50 == 50%)
    value: Mapped[float] = mapped_column(Numeric(18, 6))
    # Some list types support an explicit non-numeric domain value.  Keep it
    # separate from the numeric value so it can never be confused with NULL or
    # zero (currently used by Critical Markup for the literal "-").
    special_value: Mapped[str] = mapped_column(Text, default="", server_default="")

    __table_args__ = (
        UniqueConstraint("universal_list_id", "product_id", name="uq_list_items_list_product"),
    )


class BusinessList(Base):
    __tablename__ = "business_lists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    list_type: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(Text, default="")
    original_filename: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="imported", index=True)
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    errors_json: Mapped[str] = mapped_column(Text, default="[]")
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    items: Mapped[list["BusinessListItem"]] = relationship(
        back_populates="business_list", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_business_lists_type_created", "list_type", "created_at"),
    )


class BusinessListItem(Base):
    __tablename__ = "business_list_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_list_id: Mapped[int] = mapped_column(ForeignKey("business_lists.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    sku: Mapped[str] = mapped_column(Text, default="", index=True)
    product_name: Mapped[str] = mapped_column(Text, default="")
    manufacturer: Mapped[str] = mapped_column(Text, default="")
    value_json: Mapped[str] = mapped_column(Text, default="{}")
    value_decimal: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    value_bool: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    source_row: Mapped[int] = mapped_column(Integer, default=0)
    source_identifier: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    business_list: Mapped[BusinessList] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("business_list_id", "product_id", name="uq_business_list_items_list_product"),
        Index("ix_business_list_items_list_sku", "business_list_id", "sku"),
    )


class CalculatedPrice(Base):
    __tablename__ = "calculated_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    price_list_id: Mapped[int] = mapped_column(ForeignKey("price_lists.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)

    cost: Mapped[float] = mapped_column(Numeric(18, 4))
    base_price: Mapped[float] = mapped_column(Numeric(18, 4))

    competitor_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    price_from_competitor: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    lowest_competitor_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    chosen_competitor_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    bend_percent_used: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    markup_percent_used: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    mdc_markup_percent: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    mdc_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    competitor_candidate_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)

    final_price: Mapped[float] = mapped_column(Numeric(18, 4))
    memorandum_max_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    price_before_memorandum: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    memorandum_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    memorandum_below_mdc: Mapped[bool] = mapped_column(Boolean, default=False)
    memorandum_list_id: Mapped[int | None] = mapped_column(ForeignKey("universal_lists.id"), nullable=True, index=True)
    memorandum_list_name: Mapped[str] = mapped_column(Text, default="")
    memorandum_diagnostic_code: Mapped[str] = mapped_column(String(64), default="")
    applied_reason: Mapped[str] = mapped_column(Text, default="")
    applied_source_name: Mapped[str] = mapped_column(Text, default="")
    applied_source_type: Mapped[str] = mapped_column(String(64), default="")
    applied_rule_name: Mapped[str] = mapped_column(Text, default="")
    applied_rule_version: Mapped[str] = mapped_column(Text, default="")
    applied_list_ids: Mapped[str] = mapped_column(Text, default="[]")
    applied_rule_type: Mapped[str] = mapped_column(String(64), default="")
    applied_rule_value: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    applied_list_id: Mapped[int | None] = mapped_column(ForeignKey("universal_lists.id"), nullable=True, index=True)
    used_substitute: Mapped[bool] = mapped_column(Boolean, default=False)
    used_percentile: Mapped[bool] = mapped_column(Boolean, default=False)
    rating_global: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating_local: Mapped[int | None] = mapped_column(Integer, nullable=True)

    zone: Mapped[str] = mapped_column(String(32), default="")  # left|optimal|right; empty when Ц1 is absent

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint("price_list_id", "product_id", name="uq_calculated_prices_pl_product"),
        Index("ix_calculated_prices_pl_zone", "price_list_id", "zone"),
    )


class PricingWorkflowRun(Base):
    __tablename__ = "pricing_workflow_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pricing_context_id: Mapped[int] = mapped_column(ForeignKey("pricing_contexts.id"), index=True)
    price_format_id: Mapped[int | None] = mapped_column(ForeignKey("price_formats.id"), nullable=True, index=True)
    pricing_rule_id: Mapped[int | None] = mapped_column(ForeignKey("pricing_rules.id"), nullable=True, index=True)
    price_list_id: Mapped[int | None] = mapped_column(ForeignKey("price_lists.id"), nullable=True, index=True)
    price_list_number: Mapped[str] = mapped_column(Text, default="", index=True)
    competitor_sources_json: Mapped[str] = mapped_column(Text, default="[]")
    percentile_sources_json: Mapped[str] = mapped_column(Text, default="[]")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    analytics_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    generated_by: Mapped[str] = mapped_column(Text, default="")
    run_sources_json: Mapped[str] = mapped_column(Text, default="{}")
    run_rule_json: Mapped[str] = mapped_column(Text, default="{}")
    run_lists_json: Mapped[str] = mapped_column(Text, default="[]")
    run_reference_versions_json: Mapped[str] = mapped_column(Text, default="{}")
    run_percentile_config_json: Mapped[str] = mapped_column(Text, default="{}")
    run_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")

    __table_args__ = (
        Index("ix_pricing_workflow_runs_context_status", "pricing_context_id", "status"),
        Index("ix_pricing_workflow_runs_price_list", "price_list_number"),
    )


class ReferenceImportJob(Base):
    __tablename__ = "reference_import_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data_type: Mapped[str] = mapped_column(String(64), index=True)
    branch_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    filename: Mapped[str] = mapped_column(Text, default="")
    source_type: Mapped[str] = mapped_column(String(32), default="excel")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_success: Mapped[int] = mapped_column(Integer, default=0)
    rows_failed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    log_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user_name: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        Index("ix_reference_import_jobs_type_status", "data_type", "status"),
    )


class ReferenceUpdateStatus(Base):
    __tablename__ = "reference_update_statuses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[str] = mapped_column(Text, default="", index=True)
    branch_name: Mapped[str] = mapped_column(Text, default="")
    data_type: Mapped[str] = mapped_column(String(64), index=True)
    last_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rows_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="missing")
    error: Mapped[str] = mapped_column(Text, default="")
    current_import_status: Mapped[str] = mapped_column(String(32), default="")
    current_import_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_import_finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_successful_import_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_snapshot_product_count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("branch_id", "data_type", name="uq_reference_update_branch_type"),
        Index("ix_reference_update_status", "data_type", "status"),
    )


class BranchStock(Base):
    __tablename__ = "branch_stock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[str] = mapped_column(Text, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    sku: Mapped[str] = mapped_column(Text, index=True)
    stock: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), default="excel")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint("branch_id", "product_id", name="uq_branch_stock_branch_product"),
    )


class BranchCost(Base):
    __tablename__ = "branch_costs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[str] = mapped_column(Text, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    sku: Mapped[str] = mapped_column(Text, index=True)
    cost: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), default="excel")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint("branch_id", "product_id", name="uq_branch_cost_branch_product"),
    )


class ProductRating(Base):
    __tablename__ = "product_ratings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[str] = mapped_column(Text, default="", index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    sku: Mapped[str] = mapped_column(Text, index=True)
    rating_type: Mapped[str] = mapped_column(String(32), index=True)  # global|local
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), default="excel")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint("branch_id", "product_id", "rating_type", name="uq_product_rating_scope"),
    )


class Holding(Base):
    __tablename__ = "holdings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(Text, default="", index=True)
    name: Mapped[str] = mapped_column(Text)
    branch_id: Mapped[str] = mapped_column(Text, default="", index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="excel")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)


class Counterparty(Base):
    __tablename__ = "counterparties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(Text, default="", index=True)
    name: Mapped[str] = mapped_column(Text)
    holding_id: Mapped[str] = mapped_column(Text, default="", index=True)
    branch_id: Mapped[str] = mapped_column(Text, default="", index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="excel")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)


class DeliveryPoint(Base):
    __tablename__ = "delivery_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(Text, default="", index=True)
    counterparty_id: Mapped[str] = mapped_column(Text, default="", index=True)
    name: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text, default="")
    branch_id: Mapped[str] = mapped_column(Text, default="", index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="excel")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)


class CounterpartyPriceFormat(Base):
    __tablename__ = "counterparty_price_formats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    holding_id: Mapped[int | None] = mapped_column(ForeignKey("holdings.id"), nullable=True, index=True)
    counterparty_id: Mapped[int | None] = mapped_column(ForeignKey("counterparties.id"), nullable=True, index=True)
    delivery_point_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_points.id"), nullable=True, index=True)
    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="excel")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    __table_args__ = (
        UniqueConstraint(
            "counterparty_id",
            "delivery_point_id",
            "price_format_id",
            name="uq_counterparty_delivery_format",
        ),
        Index("ix_counterparty_price_format_pf_status", "price_format_id", "status"),
    )


class MarkupTemplate(Base):
    __tablename__ = "markup_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True, index=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    rows: Mapped[list["MarkupTemplateRow"]] = relationship(cascade="all, delete-orphan")


class MarkupTemplateRow(Base):
    __tablename__ = "markup_template_rows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("markup_templates.id"), index=True)
    cost_from: Mapped[float] = mapped_column(Numeric(18, 4))
    cost_to: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    markup_percent: Mapped[float] = mapped_column(Numeric(18, 4))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class BendTemplate(Base):
    __tablename__ = "bend_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True, index=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    rows: Mapped[list["BendTemplateRow"]] = relationship(cascade="all, delete-orphan")


class BendTemplateRow(Base):
    __tablename__ = "bend_template_rows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("bend_templates.id"), index=True)
    cost_from: Mapped[float] = mapped_column(Numeric(18, 4))
    cost_to: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    bend_percent: Mapped[float] = mapped_column(Numeric(18, 6))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class NoCompetitorMarkupTemplate(Base):
    __tablename__ = "no_competitor_markup_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True, index=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    rows: Mapped[list["NoCompetitorMarkupTemplateRow"]] = relationship(cascade="all, delete-orphan")


class NoCompetitorMarkupTemplateRow(Base):
    __tablename__ = "no_competitor_markup_template_rows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("no_competitor_markup_templates.id"), index=True)
    cost_from: Mapped[float] = mapped_column(Numeric(18, 4))
    cost_to: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    markup_percent: Mapped[float] = mapped_column(Numeric(18, 4))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class RoundingRule(Base):
    __tablename__ = "rounding_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True, index=True)
    name: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(32), default="math")
    precision: Mapped[int] = mapped_column(Integer, default=2)
    step: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)


class PricingRule(Base):
    __tablename__ = "pricing_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True, index=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    region_scope: Mapped[str] = mapped_column(Text, default="")
    branch_scope: Mapped[str] = mapped_column(Text, default="")
    markup_template_id: Mapped[int | None] = mapped_column(ForeignKey("markup_templates.id"), nullable=True, index=True)
    bend_template_id: Mapped[int | None] = mapped_column(ForeignKey("bend_templates.id"), nullable=True, index=True)
    no_competitor_template_id: Mapped[int | None] = mapped_column(ForeignKey("no_competitor_markup_templates.id"), nullable=True, index=True)
    rounding_rule_id: Mapped[int | None] = mapped_column(ForeignKey("rounding_rules.id"), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive)

    competitor_gap_tiers: Mapped[list["PricingRuleCompetitorGapTier"]] = relationship(cascade="all, delete-orphan")


class PricingRuleCompetitorGapTier(Base):
    __tablename__ = "pricing_rule_competitor_gap_tiers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pricing_rule_id: Mapped[int] = mapped_column(ForeignKey("pricing_rules.id", ondelete="CASCADE"), index=True)
    min_price: Mapped[float] = mapped_column(Numeric(18, 4))
    max_price: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    max_gap_percent: Mapped[float] = mapped_column(Numeric(18, 4))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive, server_default=text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_kz_naive, server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        UniqueConstraint("pricing_rule_id", "sort_order", name="uq_pricing_rule_competitor_gap_rule_order"),
        UniqueConstraint("pricing_rule_id", "min_price", name="uq_pricing_rule_competitor_gap_rule_min_price"),
        Index("ix_pricing_rule_competitor_gap_rule_order", "pricing_rule_id", "sort_order"),
    )
