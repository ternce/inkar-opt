CREATE TABLE IF NOT EXISTS vidman_rejected_candidates (
    id SERIAL PRIMARY KEY,
    canonical_product_id INTEGER NOT NULL REFERENCES vidman_canonical_products(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_vidman_rejected_candidates_canonical_product
        UNIQUE (canonical_product_id, product_id)
);

CREATE INDEX IF NOT EXISTS ix_vidman_rejected_candidates_lookup
    ON vidman_rejected_candidates (canonical_product_id, product_id);

CREATE INDEX IF NOT EXISTS ix_vidman_rejected_candidates_canonical_product_id
    ON vidman_rejected_candidates (canonical_product_id);

CREATE INDEX IF NOT EXISTS ix_vidman_rejected_candidates_product_id
    ON vidman_rejected_candidates (product_id);

CREATE TABLE IF NOT EXISTS vidman_product_match_audit (
    id SERIAL PRIMARY KEY,
    canonical_product_id INTEGER NOT NULL REFERENCES vidman_canonical_products(id),
    previous_status VARCHAR(32) NOT NULL DEFAULT '',
    new_status VARCHAR(32) NOT NULL DEFAULT '',
    previous_product_id INTEGER REFERENCES products(id),
    new_product_id INTEGER REFERENCES products(id),
    action VARCHAR(64) NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_vidman_product_match_audit_canonical_created
    ON vidman_product_match_audit (canonical_product_id, created_at);

CREATE INDEX IF NOT EXISTS ix_vidman_product_match_audit_action
    ON vidman_product_match_audit (action);
