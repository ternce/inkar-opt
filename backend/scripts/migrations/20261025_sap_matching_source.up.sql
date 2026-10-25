-- Independent, technical SAP material source for manual Provisor matching.
-- This schema has no foreign key or trigger to stock_snapshots/branch_stock.
CREATE TABLE IF NOT EXISTS sap_matching_source_batches (
    id BIGSERIAL PRIMARY KEY,
    source_version VARCHAR(128) NOT NULL,
    source_type VARCHAR(32) NOT NULL,
    plant VARCHAR(16) NOT NULL DEFAULT '',
    lgort VARCHAR(16) NOT NULL DEFAULT '1000',
    effective_date DATE,
    payload_sha256 VARCHAR(64) NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    record_count INTEGER NOT NULL DEFAULT 0,
    imported_by TEXT NOT NULL DEFAULT '',
    imported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_sap_matching_batch_version_scope UNIQUE (source_version, plant, lgort)
);

CREATE INDEX IF NOT EXISTS ix_sap_matching_batch_active_scope
    ON sap_matching_source_batches (active, plant, lgort);

CREATE TABLE IF NOT EXISTS sap_matching_source_items (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES sap_matching_source_batches(id) ON DELETE CASCADE,
    material TEXT NOT NULL,
    normalized_sku TEXT NOT NULL,
    product_id INTEGER REFERENCES products(id),
    product_name TEXT NOT NULL DEFAULT '',
    manufacturer TEXT NOT NULL DEFAULT '',
    identity_status VARCHAR(32) NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    imported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_sap_matching_item_batch_sku UNIQUE (batch_id, normalized_sku)
);

CREATE INDEX IF NOT EXISTS ix_sap_matching_source_items_batch
    ON sap_matching_source_items (batch_id);
CREATE INDEX IF NOT EXISTS ix_sap_matching_source_items_sku
    ON sap_matching_source_items (normalized_sku);
CREATE INDEX IF NOT EXISTS ix_sap_matching_item_product_status
    ON sap_matching_source_items (product_id, identity_status);
