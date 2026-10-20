CREATE TABLE IF NOT EXISTS stock_snapshots (
    id BIGSERIAL PRIMARY KEY,
    branch_id TEXT NOT NULL,
    source_type VARCHAR(32) NOT NULL,
    effective_date DATE,
    status VARCHAR(32) NOT NULL DEFAULT 'staged',
    item_count INTEGER NOT NULL DEFAULT 0,
    mapped_product_count INTEGER NOT NULL DEFAULT 0,
    content_fingerprint VARCHAR(64) NOT NULL DEFAULT '',
    import_job_id INTEGER REFERENCES reference_import_jobs(id),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_stock_snapshots_branch_status
    ON stock_snapshots (branch_id, status, published_at);
CREATE INDEX IF NOT EXISTS ix_stock_snapshots_effective_date ON stock_snapshots (effective_date);
CREATE INDEX IF NOT EXISTS ix_stock_snapshots_fingerprint ON stock_snapshots (content_fingerprint);
CREATE INDEX IF NOT EXISTS ix_stock_snapshots_import_job ON stock_snapshots (import_job_id);

CREATE TABLE IF NOT EXISTS stock_snapshot_items (
    id BIGSERIAL PRIMARY KEY,
    snapshot_id BIGINT NOT NULL REFERENCES stock_snapshots(id),
    ordinal INTEGER NOT NULL,
    material TEXT NOT NULL,
    normalized_sku TEXT NOT NULL,
    product_id INTEGER REFERENCES products(id),
    quantity NUMERIC(18,4),
    raw_json TEXT NOT NULL DEFAULT '{}',
    CONSTRAINT uq_stock_snapshot_item_ordinal UNIQUE (snapshot_id, ordinal)
);

CREATE INDEX IF NOT EXISTS ix_stock_snapshot_items_snapshot ON stock_snapshot_items (snapshot_id);
CREATE INDEX IF NOT EXISTS ix_stock_snapshot_items_sku ON stock_snapshot_items (normalized_sku);
CREATE INDEX IF NOT EXISTS ix_stock_snapshot_items_product ON stock_snapshot_items (product_id);
CREATE INDEX IF NOT EXISTS ix_stock_snapshot_items_snapshot_product
    ON stock_snapshot_items (snapshot_id, product_id);

ALTER TABLE branch_stock ADD COLUMN IF NOT EXISTS snapshot_id BIGINT;
CREATE INDEX IF NOT EXISTS ix_branch_stock_snapshot_id ON branch_stock (snapshot_id);

ALTER TABLE reference_update_statuses ADD COLUMN IF NOT EXISTS active_stock_snapshot_id BIGINT;
ALTER TABLE reference_update_statuses ADD COLUMN IF NOT EXISTS active_source_type VARCHAR(32) NOT NULL DEFAULT '';
ALTER TABLE reference_update_statuses ADD COLUMN IF NOT EXISTS stock_effective_date DATE;
ALTER TABLE reference_update_statuses ADD COLUMN IF NOT EXISTS last_success_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS ix_reference_update_active_stock_snapshot
    ON reference_update_statuses (active_stock_snapshot_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint constraint_row
        JOIN pg_class relation_row ON relation_row.oid = constraint_row.conrelid
        JOIN pg_namespace namespace_row ON namespace_row.oid = relation_row.relnamespace
        WHERE constraint_row.conname = 'fk_branch_stock_snapshot'
          AND relation_row.relname = 'branch_stock'
          AND namespace_row.nspname = current_schema()
    ) THEN
        ALTER TABLE branch_stock ADD CONSTRAINT fk_branch_stock_snapshot
            FOREIGN KEY (snapshot_id) REFERENCES stock_snapshots(id);
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint constraint_row
        JOIN pg_class relation_row ON relation_row.oid = constraint_row.conrelid
        JOIN pg_namespace namespace_row ON namespace_row.oid = relation_row.relnamespace
        WHERE constraint_row.conname = 'fk_reference_update_active_stock_snapshot'
          AND relation_row.relname = 'reference_update_statuses'
          AND namespace_row.nspname = current_schema()
    ) THEN
        ALTER TABLE reference_update_statuses ADD CONSTRAINT fk_reference_update_active_stock_snapshot
            FOREIGN KEY (active_stock_snapshot_id) REFERENCES stock_snapshots(id);
    END IF;
END $$;
