-- Stage 5B Competitor Assignment persisted read models.
-- This migration creates storage only. Run the explicit backfill script after it.

ALTER TABLE competitor_price_lists
    ADD COLUMN IF NOT EXISTS items_count INTEGER DEFAULT 0;

ALTER TABLE competitor_price_lists
    ADD COLUMN IF NOT EXISTS matched_positive_items_count INTEGER DEFAULT 0;

CREATE TABLE IF NOT EXISTS competitor_price_percentile_source_summaries (
    id SERIAL PRIMARY KEY,
    price_format_id INTEGER NOT NULL REFERENCES price_formats(id),
    source_type VARCHAR(32) NOT NULL DEFAULT '',
    source_key TEXT NOT NULL DEFAULT '',
    competitor_price_list_id INTEGER REFERENCES competitor_price_lists(id),
    branch_name TEXT NOT NULL DEFAULT '',
    competitor_name TEXT NOT NULL DEFAULT '',
    percentile_scope VARCHAR(32) NOT NULL DEFAULT 'regional',
    percentile INTEGER NOT NULL,
    sku_count INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    generated_at TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_comp_pct_source_summary UNIQUE (
        price_format_id,
        source_type,
        source_key,
        competitor_price_list_id,
        branch_name,
        competitor_name,
        percentile_scope,
        percentile
    )
);

CREATE INDEX IF NOT EXISTS ix_comp_pct_source_summary_lookup
ON competitor_price_percentile_source_summaries (
    price_format_id,
    source_key,
    branch_name,
    competitor_name,
    percentile_scope,
    percentile
);

CREATE TABLE IF NOT EXISTS regular_competitor_price_percentile_source_summaries (
    id SERIAL PRIMARY KEY,
    competitor_identity TEXT NOT NULL,
    competitor_name TEXT NOT NULL DEFAULT '',
    percentile INTEGER NOT NULL,
    sku_count INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    generated_at TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_regular_comp_pct_source_summary UNIQUE (
        competitor_identity,
        percentile
    )
);

CREATE INDEX IF NOT EXISTS ix_regular_comp_pct_source_summary_lookup
ON regular_competitor_price_percentile_source_summaries (
    competitor_identity,
    percentile
);
