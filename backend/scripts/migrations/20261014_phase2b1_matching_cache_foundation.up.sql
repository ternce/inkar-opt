-- Phase 2B.1 is additive and disabled by default.  It does not alter legacy
-- matching or pricing tables.  Apply this file in a normal transaction.

CREATE TABLE competitor_source_versions (
    id BIGSERIAL PRIMARY KEY,
    competitor_price_list_id INTEGER NOT NULL REFERENCES competitor_price_lists(id),
    generation BIGINT NOT NULL,
    content_sha256 VARCHAR(64) NOT NULL,
    matching_input_sha256 VARCHAR(64) NOT NULL,
    row_count BIGINT NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    source_metadata_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    matching_started_at TIMESTAMP,
    ready_at TIMESTAMP,
    published_at TIMESTAMP,
    failed_at TIMESTAMP,
    CONSTRAINT uq_comp_source_version_generation UNIQUE (competitor_price_list_id, generation),
    CONSTRAINT uq_comp_source_version_content UNIQUE (competitor_price_list_id, content_sha256),
    CONSTRAINT ck_comp_source_version_status CHECK (
        status IN ('pending','matching','ready','published','failed','superseded')
    )
);

CREATE INDEX ix_competitor_source_versions_competitor_price_list_id
    ON competitor_source_versions (competitor_price_list_id);
CREATE INDEX ix_competitor_source_versions_status
    ON competitor_source_versions (status);
CREATE INDEX ix_comp_source_version_source_status
    ON competitor_source_versions (competitor_price_list_id, status, generation);
CREATE UNIQUE INDEX uq_comp_source_version_published
    ON competitor_source_versions (competitor_price_list_id)
    WHERE status = 'published';

CREATE TABLE competitor_source_version_items (
    id BIGSERIAL PRIMARY KEY,
    source_version_id BIGINT NOT NULL REFERENCES competitor_source_versions(id),
    source_row_key TEXT NOT NULL,
    source_ordinal BIGINT NOT NULL,
    duplicate_ordinal INTEGER NOT NULL DEFAULT 0,
    row_sha256 VARCHAR(64) NOT NULL,
    matching_input_sha256 VARCHAR(64) NOT NULL,
    legacy_source_item_id INTEGER,
    authoritative_product_id INTEGER REFERENCES products(id),
    provisor_id BIGINT,
    provisor_goods_id BIGINT,
    filial_id BIGINT,
    name TEXT NOT NULL DEFAULT '',
    reg_number TEXT NOT NULL DEFAULT '',
    distributor_goods_name TEXT NOT NULL DEFAULT '',
    distributor_goods_id TEXT NOT NULL DEFAULT '',
    distributor_price NUMERIC(18,4),
    stock NUMERIC(18,4),
    package_count NUMERIC(18,4),
    expiry_date TEXT NOT NULL DEFAULT '',
    authoritative_match_key TEXT NOT NULL DEFAULT '',
    authoritative_match_type VARCHAR(64) NOT NULL DEFAULT '',
    authoritative_match_score NUMERIC(18,4),
    authoritative_matched_sku TEXT NOT NULL DEFAULT '',
    raw_name TEXT NOT NULL DEFAULT '',
    raw_manufacturer TEXT NOT NULL DEFAULT '',
    normalized_name TEXT NOT NULL DEFAULT '',
    normalized_manufacturer TEXT NOT NULL DEFAULT '',
    parsed_base_name TEXT NOT NULL DEFAULT '',
    parsed_form TEXT NOT NULL DEFAULT '',
    parsed_forms_json TEXT NOT NULL DEFAULT '',
    parsed_dosage NUMERIC(18,4),
    parsed_dosage_volume NUMERIC(18,4),
    parsed_quantity INTEGER,
    parsed_volume NUMERIC(18,4),
    parsed_weight NUMERIC(18,4),
    parsed_percent_strength NUMERIC(18,4),
    parsed_concentration NUMERIC(18,4),
    parsed_iu_dosage NUMERIC(18,4),
    parsed_strength_signature TEXT NOT NULL DEFAULT '',
    parsed_dimensions_json TEXT NOT NULL DEFAULT '',
    parsed_critical_tokens_json TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '',
    CONSTRAINT uq_comp_source_version_item_ordinal UNIQUE (source_version_id, source_ordinal),
    CONSTRAINT uq_comp_source_version_item_identity UNIQUE (
        source_version_id, source_row_key, duplicate_ordinal
    )
);

CREATE INDEX ix_competitor_source_version_items_source_version_id
    ON competitor_source_version_items (source_version_id);
CREATE INDEX ix_comp_source_version_item_product
    ON competitor_source_version_items (source_version_id, authoritative_product_id);
CREATE INDEX ix_comp_source_version_item_goods
    ON competitor_source_version_items (source_version_id, provisor_goods_id);
CREATE INDEX ix_comp_source_version_item_distributor
    ON competitor_source_version_items (source_version_id, distributor_goods_id);
CREATE INDEX ix_comp_source_version_item_hash
    ON competitor_source_version_items (source_version_id, matching_input_sha256);

CREATE TABLE competitor_match_dependency_revisions (
    id BIGSERIAL PRIMARY KEY,
    scope_type VARCHAR(64) NOT NULL,
    scope_key TEXT NOT NULL DEFAULT 'global',
    revision BIGINT NOT NULL DEFAULT 1,
    fingerprint VARCHAR(64) NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_comp_match_dependency_scope UNIQUE (scope_type, scope_key),
    CONSTRAINT ck_comp_match_dependency_revision CHECK (revision >= 1)
);

CREATE INDEX ix_comp_match_dependency_scope_revision
    ON competitor_match_dependency_revisions (scope_type, scope_key, revision);

CREATE TABLE competitor_match_snapshots (
    id BIGSERIAL PRIMARY KEY,
    source_version_id BIGINT NOT NULL REFERENCES competitor_source_versions(id),
    dependency_fingerprint VARCHAR(64) NOT NULL,
    dependency_vector_json TEXT NOT NULL DEFAULT '{}',
    product_catalog_revision BIGINT NOT NULL DEFAULT 0,
    manual_code_mapping_revision BIGINT NOT NULL DEFAULT 0,
    provisor_reference_revision BIGINT NOT NULL DEFAULT 0,
    provisor_substitute_revision BIGINT NOT NULL DEFAULT 0,
    vidman_authoritative_revision BIGINT NOT NULL DEFAULT 0,
    matcher_version VARCHAR(64) NOT NULL,
    normalization_version VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    input_row_count BIGINT NOT NULL DEFAULT 0,
    matched_row_count BIGINT NOT NULL DEFAULT 0,
    unmatched_row_count BIGINT NOT NULL DEFAULT 0,
    stats_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ready_at TIMESTAMP,
    published_at TIMESTAMP,
    failed_at TIMESTAMP,
    CONSTRAINT uq_comp_match_snapshot_key UNIQUE (
        source_version_id, dependency_fingerprint, matcher_version, normalization_version
    ),
    CONSTRAINT ck_comp_match_snapshot_status CHECK (
        status IN ('pending','running','ready','published','failed','superseded')
    )
);

CREATE INDEX ix_competitor_match_snapshots_source_version_id
    ON competitor_match_snapshots (source_version_id);
CREATE INDEX ix_competitor_match_snapshots_status
    ON competitor_match_snapshots (status);
CREATE INDEX ix_comp_match_snapshot_source_status
    ON competitor_match_snapshots (source_version_id, status);

CREATE TABLE competitor_match_snapshot_items (
    id BIGSERIAL PRIMARY KEY,
    match_snapshot_id BIGINT NOT NULL REFERENCES competitor_match_snapshots(id),
    source_version_item_id BIGINT NOT NULL REFERENCES competitor_source_version_items(id),
    product_id INTEGER REFERENCES products(id),
    match_key TEXT NOT NULL DEFAULT '',
    match_type VARCHAR(64) NOT NULL DEFAULT 'unmatched',
    match_score NUMERIC(18,4),
    matched_sku TEXT NOT NULL DEFAULT '',
    is_manual BOOLEAN NOT NULL DEFAULT FALSE,
    is_rejected BOOLEAN NOT NULL DEFAULT FALSE,
    is_authoritative BOOLEAN NOT NULL DEFAULT FALSE,
    audit_json TEXT NOT NULL DEFAULT '{}',
    CONSTRAINT uq_comp_match_snapshot_item UNIQUE (match_snapshot_id, source_version_item_id)
);

CREATE INDEX ix_competitor_match_snapshot_items_match_snapshot_id
    ON competitor_match_snapshot_items (match_snapshot_id);
CREATE INDEX ix_comp_match_snapshot_item_product
    ON competitor_match_snapshot_items (match_snapshot_id, product_id);
CREATE INDEX ix_comp_match_snapshot_item_type
    ON competitor_match_snapshot_items (match_snapshot_id, match_type);

CREATE TABLE competitor_match_jobs (
    id BIGSERIAL PRIMARY KEY,
    idempotency_key VARCHAR(64) NOT NULL UNIQUE,
    source_version_id BIGINT NOT NULL REFERENCES competitor_source_versions(id),
    match_snapshot_id BIGINT REFERENCES competitor_match_snapshots(id),
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    claimed_by TEXT NOT NULL DEFAULT '',
    heartbeat_at TIMESTAMP,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_comp_match_job_status CHECK (
        status IN ('pending','running','succeeded','failed','cancelled')
    )
);

CREATE INDEX ix_competitor_match_jobs_source_version_id
    ON competitor_match_jobs (source_version_id);
CREATE INDEX ix_competitor_match_jobs_match_snapshot_id
    ON competitor_match_jobs (match_snapshot_id);
CREATE INDEX ix_competitor_match_jobs_status
    ON competitor_match_jobs (status);
CREATE INDEX ix_comp_match_job_claim
    ON competitor_match_jobs (status, available_at, id);

CREATE TABLE price_format_competitor_materializations (
    id BIGSERIAL PRIMARY KEY,
    price_format_id INTEGER NOT NULL REFERENCES price_formats(id),
    materialization_key VARCHAR(64) NOT NULL,
    assignment_fingerprint VARCHAR(64) NOT NULL,
    source_snapshot_ids_json TEXT NOT NULL DEFAULT '[]',
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    is_current BOOLEAN NOT NULL DEFAULT FALSE,
    row_count BIGINT NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ready_at TIMESTAMP,
    published_at TIMESTAMP,
    failed_at TIMESTAMP,
    CONSTRAINT uq_pf_comp_materialization_key UNIQUE (price_format_id, materialization_key),
    CONSTRAINT ck_pf_comp_materialization_status CHECK (
        status IN ('pending','building','ready','published','failed','superseded')
    )
);

CREATE INDEX ix_price_format_competitor_materializations_price_format_id
    ON price_format_competitor_materializations (price_format_id);
CREATE INDEX ix_price_format_competitor_materializations_status
    ON price_format_competitor_materializations (status);
CREATE INDEX ix_price_format_competitor_materializations_is_current
    ON price_format_competitor_materializations (is_current);
CREATE INDEX ix_pf_comp_materialization_current
    ON price_format_competitor_materializations (price_format_id, is_current);
CREATE UNIQUE INDEX uq_pf_comp_materialization_current
    ON price_format_competitor_materializations (price_format_id)
    WHERE is_current IS TRUE;
