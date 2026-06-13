CREATE TABLE IF NOT EXISTS business_lists (
    id SERIAL PRIMARY KEY,
    list_type VARCHAR(32) NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    original_filename TEXT NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT 'imported',
    summary_json TEXT NOT NULL DEFAULT '{}',
    errors_json TEXT NOT NULL DEFAULT '[]',
    item_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_business_lists_list_type ON business_lists (list_type);
CREATE INDEX IF NOT EXISTS ix_business_lists_created_at ON business_lists (created_at);
CREATE INDEX IF NOT EXISTS ix_business_lists_type_created ON business_lists (list_type, created_at);

CREATE TABLE IF NOT EXISTS business_list_items (
    id SERIAL PRIMARY KEY,
    business_list_id INTEGER NOT NULL REFERENCES business_lists(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    sku TEXT NOT NULL DEFAULT '',
    product_name TEXT NOT NULL DEFAULT '',
    manufacturer TEXT NOT NULL DEFAULT '',
    value_json TEXT NOT NULL DEFAULT '{}',
    value_decimal NUMERIC(18, 6),
    value_bool BOOLEAN,
    source_row INTEGER NOT NULL DEFAULT 0,
    source_identifier TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP,
    CONSTRAINT uq_business_list_items_list_product UNIQUE (business_list_id, product_id)
);

CREATE INDEX IF NOT EXISTS ix_business_list_items_business_list_id ON business_list_items (business_list_id);
CREATE INDEX IF NOT EXISTS ix_business_list_items_product_id ON business_list_items (product_id);
CREATE INDEX IF NOT EXISTS ix_business_list_items_sku ON business_list_items (sku);
CREATE INDEX IF NOT EXISTS ix_business_list_items_list_sku ON business_list_items (business_list_id, sku);
