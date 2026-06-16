ALTER TABLE calculated_prices ADD COLUMN IF NOT EXISTS applied_rule_type VARCHAR(64) DEFAULT '';
ALTER TABLE calculated_prices ADD COLUMN IF NOT EXISTS applied_rule_value NUMERIC(18, 6);
ALTER TABLE calculated_prices ADD COLUMN IF NOT EXISTS applied_list_id INTEGER REFERENCES universal_lists(id);

CREATE INDEX IF NOT EXISTS ix_calculated_prices_applied_list_id ON calculated_prices (applied_list_id);

UPDATE universal_lists
SET type = CASE
    WHEN type IN ('fixed_price', 'min_price', 'max_price', 'fixed_markup', 'min_markup', 'critical_markup', 'max_markup', 'no_bend', 'percentile_override', 'exclude_from_pricing') THEN type
    WHEN lower(type) IN ('exclusion', 'exclude') THEN 'exclude_from_pricing'
    WHEN lower(type) = 'markup' THEN 'fixed_markup'
    ELSE type
END;
