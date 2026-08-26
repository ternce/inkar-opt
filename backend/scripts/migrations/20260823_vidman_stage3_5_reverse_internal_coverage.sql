CREATE TABLE IF NOT EXISTS vidman_internal_coverage_queue (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL UNIQUE REFERENCES products(id),
    tier VARCHAR(32) NOT NULL DEFAULT '',
    top_candidate_canonical_id INTEGER REFERENCES vidman_canonical_products(id),
    top_candidate_score NUMERIC(18, 4),
    second_candidate_score NUMERIC(18, 4),
    score_gap NUMERIC(18, 4),
    candidate_count INTEGER NOT NULL DEFAULT 0,
    shared_structural_fields INTEGER NOT NULL DEFAULT 0,
    hard_conflicts_json TEXT NOT NULL DEFAULT '[]',
    coverage_reason TEXT NOT NULL DEFAULT '',
    candidate_rankings_json TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_vidman_internal_coverage_tier
    ON vidman_internal_coverage_queue (tier);

CREATE INDEX IF NOT EXISTS ix_vidman_internal_coverage_tier_score
    ON vidman_internal_coverage_queue (tier, top_candidate_score);

CREATE INDEX IF NOT EXISTS ix_vidman_internal_coverage_candidate
    ON vidman_internal_coverage_queue (top_candidate_canonical_id);
