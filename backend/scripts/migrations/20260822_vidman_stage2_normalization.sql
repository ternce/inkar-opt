CREATE TABLE IF NOT EXISTS vidman_normalized_items (
    id SERIAL PRIMARY KEY,
    raw_item_id INTEGER NOT NULL UNIQUE REFERENCES vidman_raw_items(id),
    normalized_name TEXT NOT NULL DEFAULT '',
    normalized_manufacturer TEXT NOT NULL DEFAULT '',
    base_name TEXT NOT NULL DEFAULT '',
    dosage_value NUMERIC(18, 6),
    dosage_unit VARCHAR(32) NOT NULL DEFAULT '',
    concentration_value NUMERIC(18, 6),
    concentration_unit VARCHAR(32) NOT NULL DEFAULT '',
    volume_value NUMERIC(18, 6),
    volume_unit VARCHAR(32) NOT NULL DEFAULT '',
    weight_value NUMERIC(18, 6),
    weight_unit VARCHAR(32) NOT NULL DEFAULT '',
    pack_count INTEGER,
    dosage_form VARCHAR(64) NOT NULL DEFAULT '',
    variant_text TEXT NOT NULL DEFAULT '',
    normalized_signature TEXT NOT NULL DEFAULT '',
    parse_confidence NUMERIC(18, 4) NOT NULL DEFAULT 0,
    parse_warnings_json TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_vidman_normalized_items_raw_item_id
    ON vidman_normalized_items (raw_item_id);

CREATE INDEX IF NOT EXISTS ix_vidman_normalized_signature
    ON vidman_normalized_items (normalized_signature);

CREATE INDEX IF NOT EXISTS ix_vidman_normalized_base_name
    ON vidman_normalized_items (base_name);

CREATE INDEX IF NOT EXISTS ix_vidman_normalized_manufacturer
    ON vidman_normalized_items (normalized_manufacturer);

CREATE TABLE IF NOT EXISTS vidman_canonical_products (
    id SERIAL PRIMARY KEY,
    canonical_name TEXT NOT NULL DEFAULT '',
    canonical_manufacturer TEXT NOT NULL DEFAULT '',
    base_name TEXT NOT NULL DEFAULT '',
    dosage_value NUMERIC(18, 6),
    dosage_unit VARCHAR(32) NOT NULL DEFAULT '',
    concentration_value NUMERIC(18, 6),
    concentration_unit VARCHAR(32) NOT NULL DEFAULT '',
    volume_value NUMERIC(18, 6),
    volume_unit VARCHAR(32) NOT NULL DEFAULT '',
    weight_value NUMERIC(18, 6),
    weight_unit VARCHAR(32) NOT NULL DEFAULT '',
    pack_count INTEGER,
    dosage_form VARCHAR(64) NOT NULL DEFAULT '',
    canonical_signature TEXT NOT NULL UNIQUE,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    raw_variants_count INTEGER NOT NULL DEFAULT 0,
    accounts_count INTEGER NOT NULL DEFAULT 0,
    plks_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_vidman_canonical_signature
    ON vidman_canonical_products (canonical_signature);

CREATE INDEX IF NOT EXISTS ix_vidman_canonical_base_name
    ON vidman_canonical_products (base_name);

CREATE INDEX IF NOT EXISTS ix_vidman_canonical_manufacturer
    ON vidman_canonical_products (canonical_manufacturer);

CREATE TABLE IF NOT EXISTS vidman_raw_canonical_links (
    id SERIAL PRIMARY KEY,
    raw_item_id INTEGER NOT NULL UNIQUE REFERENCES vidman_raw_items(id),
    normalized_item_id INTEGER NOT NULL REFERENCES vidman_normalized_items(id),
    canonical_product_id INTEGER NOT NULL REFERENCES vidman_canonical_products(id),
    match_type VARCHAR(64) NOT NULL DEFAULT '',
    confidence NUMERIC(18, 4) NOT NULL DEFAULT 0,
    is_auto_linked BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_vidman_raw_canonical_links_raw_item_id
    ON vidman_raw_canonical_links (raw_item_id);

CREATE INDEX IF NOT EXISTS ix_vidman_raw_canonical_links_normalized
    ON vidman_raw_canonical_links (normalized_item_id);

CREATE INDEX IF NOT EXISTS ix_vidman_raw_canonical_links_canonical
    ON vidman_raw_canonical_links (canonical_product_id);
