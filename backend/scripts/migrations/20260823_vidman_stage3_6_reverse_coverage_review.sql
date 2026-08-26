CREATE TABLE IF NOT EXISTS vidman_internal_coverage_rejections (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    canonical_product_id INTEGER NOT NULL REFERENCES vidman_canonical_products(id),
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_vidman_internal_coverage_rejection_product_canonical
        UNIQUE (product_id, canonical_product_id)
);

CREATE INDEX IF NOT EXISTS ix_vidman_internal_coverage_rejection_lookup
    ON vidman_internal_coverage_rejections (product_id, canonical_product_id);

CREATE INDEX IF NOT EXISTS ix_vidman_internal_coverage_rejections_product_id
    ON vidman_internal_coverage_rejections (product_id);

CREATE INDEX IF NOT EXISTS ix_vidman_internal_coverage_rejections_canonical_product_id
    ON vidman_internal_coverage_rejections (canonical_product_id);

CREATE TABLE IF NOT EXISTS vidman_internal_coverage_decisions (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL UNIQUE REFERENCES products(id),
    status VARCHAR(32) NOT NULL DEFAULT '',
    canonical_product_id INTEGER REFERENCES vidman_canonical_products(id),
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_vidman_internal_coverage_decisions_status
    ON vidman_internal_coverage_decisions (status);

CREATE INDEX IF NOT EXISTS ix_vidman_internal_coverage_decisions_canonical_product_id
    ON vidman_internal_coverage_decisions (canonical_product_id);

CREATE TABLE IF NOT EXISTS vidman_internal_coverage_audit (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    canonical_product_id INTEGER REFERENCES vidman_canonical_products(id),
    previous_state VARCHAR(64) NOT NULL DEFAULT '',
    new_state VARCHAR(64) NOT NULL DEFAULT '',
    action VARCHAR(64) NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_vidman_internal_coverage_audit_product_created
    ON vidman_internal_coverage_audit (product_id, created_at);

CREATE INDEX IF NOT EXISTS ix_vidman_internal_coverage_audit_action
    ON vidman_internal_coverage_audit (action);
