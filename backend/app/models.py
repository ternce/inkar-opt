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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True, index=True)
    name: Mapped[str] = mapped_column(Text)
    cost: Mapped[float] = mapped_column(Numeric(18, 4), default=0)
    top_rank: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    provisor_goods_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProductExtra(Base):
    __tablename__ = "product_extras"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    stock: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    manufacturer: Mapped[str] = mapped_column(Text, default="")

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PriceFormat(Base):
    __tablename__ = "price_formats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True, index=True)
    name: Mapped[str] = mapped_column(Text)
    branch: Mapped[str] = mapped_column(Text, default="")

    pricing_rule: Mapped[str] = mapped_column(Text, default="")
    pricing_rule_id: Mapped[int | None] = mapped_column(ForeignKey("pricing_rules.id"), nullable=True, index=True)
    rounding_rule_id: Mapped[int | None] = mapped_column(ForeignKey("rounding_rules.id"), nullable=True, index=True)
    pricing_rule_applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pricing_rule_applied_tables_json: Mapped[str] = mapped_column(Text, default="[]")
    competitor_price_mode: Mapped[str] = mapped_column(String(32), default="regular")
    percentile_number: Mapped[int] = mapped_column(Integer, default=10)
    # MVP: "Прогиб" (bend/undercut) — значение В ПРОЦЕНТАХ,
    # на сколько цена должна быть ниже МЦК (минимальной цены конкурента).
    progib: Mapped[float] = mapped_column(Numeric(18, 4), default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    markup_ranges: Mapped[list[MarkupRange]] = relationship(
        back_populates="price_format", cascade="all, delete-orphan"
    )


class PricingContext(Base):
    __tablename__ = "pricing_contexts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    branch_id: Mapped[str] = mapped_column(Text, default="", index=True)
    region: Mapped[str] = mapped_column(Text, default="", index=True)
    sales_channel: Mapped[str] = mapped_column(Text, default="", index=True)
    name: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("branch_id", "region", "sales_channel", "name", name="uq_pricing_context_scope"),
        Index("ix_pricing_context_active_scope", "is_active", "branch_id", "region", "sales_channel"),
    )


class AppUser(Base):
    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[str] = mapped_column(String(32), default="admin", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    branches: Mapped[list["UserBranchAssignment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserBranchAssignment(Base):
    __tablename__ = "user_branch_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id"), index=True)
    branch_id: Mapped[str] = mapped_column(Text, default="", index=True)
    branch_name: Mapped[str] = mapped_column(Text, default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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

    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
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
    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)

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
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    items: Mapped[list["CompetitorPriceListItem"]] = relationship(
        back_populates="price_list", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("price_format_id", "source_type", "source_key", name="uq_competitor_price_list_source"),
        Index("ix_competitor_price_lists_pf_selected", "price_format_id", "is_selected"),
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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


class CompetitorPricePercentile(Base):
    __tablename__ = "competitor_price_percentiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)

    branch_name: Mapped[str] = mapped_column(Text, default="Без филиала", index=True)
    competitor_name: Mapped[str] = mapped_column(Text, default="", index=True)
    percentile: Mapped[int] = mapped_column(Integer, index=True)
    value: Mapped[float] = mapped_column(Numeric(18, 4))
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "price_format_id",
            "product_id",
            "branch_name",
            "competitor_name",
            "percentile",
            name="uq_comp_percentile_scope",
        ),
        Index("ix_comp_percentile_lookup", "price_format_id", "product_id", "percentile"),
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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_jobs_type_format_status", "type", "format_code", "status"),
    )


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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UniversalListPriceFormat(Base):
    __tablename__ = "universal_list_price_formats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    universal_list_id: Mapped[int] = mapped_column(ForeignKey("universal_lists.id"), index=True)
    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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

    __table_args__ = (
        UniqueConstraint("universal_list_id", "product_id", name="uq_list_items_list_product"),
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

    final_price: Mapped[float] = mapped_column(Numeric(18, 4))
    applied_reason: Mapped[str] = mapped_column(Text, default="")
    applied_source_name: Mapped[str] = mapped_column(Text, default="")
    applied_source_type: Mapped[str] = mapped_column(String(64), default="")
    applied_rule_name: Mapped[str] = mapped_column(Text, default="")
    applied_rule_version: Mapped[str] = mapped_column(Text, default="")
    applied_list_ids: Mapped[str] = mapped_column(Text, default="[]")
    used_substitute: Mapped[bool] = mapped_column(Boolean, default=False)
    used_percentile: Mapped[bool] = mapped_column(Boolean, default=False)
    rating_global: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating_local: Mapped[int | None] = mapped_column(Integer, nullable=True)

    zone: Mapped[str] = mapped_column(String(32), default="no-data")  # left|optimal|right|no-data

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("price_list_id", "product_id", name="uq_calculated_prices_pl_product"),
        Index("ix_calculated_prices_pl_zone", "price_list_id", "zone"),
    )


class PricingWorkflowRun(Base):
    __tablename__ = "pricing_workflow_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pricing_context_id: Mapped[int] = mapped_column(ForeignKey("pricing_contexts.id"), index=True)
    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)
    pricing_rule_id: Mapped[int | None] = mapped_column(ForeignKey("pricing_rules.id"), nullable=True, index=True)
    price_list_id: Mapped[int | None] = mapped_column(ForeignKey("price_lists.id"), nullable=True, index=True)
    price_list_number: Mapped[str] = mapped_column(Text, default="", index=True)
    competitor_sources_json: Mapped[str] = mapped_column(Text, default="[]")
    percentile_sources_json: Mapped[str] = mapped_column(Text, default="[]")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
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
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Counterparty(Base):
    __tablename__ = "counterparties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(Text, default="", index=True)
    name: Mapped[str] = mapped_column(Text)
    holding_id: Mapped[str] = mapped_column(Text, default="", index=True)
    branch_id: Mapped[str] = mapped_column(Text, default="", index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="excel")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DeliveryPoint(Base):
    __tablename__ = "delivery_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(Text, default="", index=True)
    counterparty_id: Mapped[str] = mapped_column(Text, default="", index=True)
    name: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text, default="")
    branch_id: Mapped[str] = mapped_column(Text, default="", index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="excel")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CounterpartyPriceFormat(Base):
    __tablename__ = "counterparty_price_formats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    holding_id: Mapped[int | None] = mapped_column(ForeignKey("holdings.id"), nullable=True, index=True)
    counterparty_id: Mapped[int | None] = mapped_column(ForeignKey("counterparties.id"), nullable=True, index=True)
    delivery_point_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_points.id"), nullable=True, index=True)
    price_format_id: Mapped[int] = mapped_column(ForeignKey("price_formats.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="excel")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
