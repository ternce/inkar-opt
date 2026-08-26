CREATE TABLE IF NOT EXISTS vidman_product_matches (
    id SERIAL PRIMARY KEY,
    canonical_product_id INTEGER NOT NULL UNIQUE REFERENCES vidman_canonical_products(id),
    product_id INTEGER REFERENCES products(id),
    status VARCHAR(32) NOT NULL DEFAULT 'UNMATCHED',
    match_type VARCHAR(64) NOT NULL DEFAULT '',
    confidence NUMERIC(18, 4) NOT NULL DEFAULT 0,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    matched_by TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_vidman_product_matches_status
    ON vidman_product_matches (status);

CREATE INDEX IF NOT EXISTS ix_vidman_product_matches_product
    ON vidman_product_matches (product_id);

CREATE TABLE IF NOT EXISTS vidman_product_match_candidates (
    id SERIAL PRIMARY KEY,
    canonical_product_id INTEGER NOT NULL REFERENCES vidman_canonical_products(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    rank INTEGER NOT NULL DEFAULT 0,
    score NUMERIC(18, 4) NOT NULL DEFAULT 0,
    match_type VARCHAR(64) NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_vidman_product_match_candidates_canonical_product
        UNIQUE (canonical_product_id, product_id)
);

CREATE INDEX IF NOT EXISTS ix_vidman_product_match_candidates_lookup
    ON vidman_product_match_candidates (canonical_product_id, rank);
