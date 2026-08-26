CREATE TABLE IF NOT EXISTS internal_product_normalized (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL UNIQUE REFERENCES products(id),
    raw_name TEXT NOT NULL DEFAULT '',
    raw_manufacturer TEXT NOT NULL DEFAULT '',
    normalized_name TEXT NOT NULL DEFAULT '',
    base_name TEXT NOT NULL DEFAULT '',
    normalized_manufacturer TEXT NOT NULL DEFAULT '',
    dosage_value NUMERIC(18, 6),
    dosage_unit VARCHAR(32) NOT NULL DEFAULT '',
    strength_components TEXT NOT NULL DEFAULT '',
    concentration_value NUMERIC(18, 6),
    concentration_unit VARCHAR(64) NOT NULL DEFAULT '',
    volume_value NUMERIC(18, 6),
    volume_unit VARCHAR(32) NOT NULL DEFAULT '',
    weight_value NUMERIC(18, 6),
    weight_unit VARCHAR(32) NOT NULL DEFAULT '',
    package_volume TEXT NOT NULL DEFAULT '',
    package_weight TEXT NOT NULL DEFAULT '',
    pack_count INTEGER,
    dosage_form VARCHAR(64) NOT NULL DEFAULT '',
    variant_text TEXT NOT NULL DEFAULT '',
    identity_tokens_json TEXT NOT NULL DEFAULT '[]',
    normalized_signature TEXT NOT NULL DEFAULT '',
    parse_warnings_json TEXT NOT NULL DEFAULT '[]',
    parse_confidence NUMERIC(18, 4) NOT NULL DEFAULT 0,
    source_updated_at TIMESTAMP,
    normalized_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_internal_product_normalized_signature
    ON internal_product_normalized (normalized_signature);

CREATE INDEX IF NOT EXISTS ix_internal_product_normalized_base_name
    ON internal_product_normalized (base_name);

CREATE INDEX IF NOT EXISTS ix_internal_product_normalized_manufacturer
    ON internal_product_normalized (normalized_manufacturer);
