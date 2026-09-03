CREATE TABLE IF NOT EXISTS pricing_rule_competitor_gap_tiers (
    id SERIAL PRIMARY KEY,
    pricing_rule_id INTEGER NOT NULL REFERENCES pricing_rules(id) ON DELETE CASCADE,
    min_price NUMERIC(18, 4) NOT NULL,
    max_price NUMERIC(18, 4),
    max_gap_percent NUMERIC(18, 4) NOT NULL,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_pricing_rule_competitor_gap_rule_order UNIQUE (pricing_rule_id, sort_order),
    CONSTRAINT uq_pricing_rule_competitor_gap_rule_min_price UNIQUE (pricing_rule_id, min_price)
);

CREATE INDEX IF NOT EXISTS ix_pricing_rule_competitor_gap_tiers_pricing_rule_id
    ON pricing_rule_competitor_gap_tiers (pricing_rule_id);

CREATE INDEX IF NOT EXISTS ix_pricing_rule_competitor_gap_rule_order
    ON pricing_rule_competitor_gap_tiers (pricing_rule_id, sort_order);
