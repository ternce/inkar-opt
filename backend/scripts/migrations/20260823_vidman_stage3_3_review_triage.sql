CREATE TABLE IF NOT EXISTS vidman_product_review_queue (
    id SERIAL PRIMARY KEY,
    canonical_product_id INTEGER NOT NULL UNIQUE REFERENCES vidman_canonical_products(id),
    tier VARCHAR(32) NOT NULL DEFAULT '',
    top_candidate_product_id INTEGER REFERENCES products(id),
    top_candidate_score NUMERIC(18, 4),
    second_candidate_score NUMERIC(18, 4),
    score_gap NUMERIC(18, 4),
    candidate_count INTEGER NOT NULL DEFAULT 0,
    shared_structural_fields INTEGER NOT NULL DEFAULT 0,
    hard_conflicts_json TEXT NOT NULL DEFAULT '[]',
    review_reason TEXT NOT NULL DEFAULT '',
    candidate_rankings_json TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_vidman_review_queue_tier
    ON vidman_product_review_queue (tier);

CREATE INDEX IF NOT EXISTS ix_vidman_review_queue_candidate
    ON vidman_product_review_queue (top_candidate_product_id);

CREATE INDEX IF NOT EXISTS ix_vidman_review_queue_tier_score
    ON vidman_product_review_queue (tier, top_candidate_score);
