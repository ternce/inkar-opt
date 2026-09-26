ALTER TABLE vidman_competitor_price_list_sources
    ADD COLUMN IF NOT EXISTS update_mode VARCHAR(16) NOT NULL DEFAULT 'auto';

UPDATE vidman_competitor_price_list_sources
SET update_mode = 'auto'
WHERE update_mode IS NULL OR BTRIM(update_mode) = '';

CREATE INDEX IF NOT EXISTS ix_vidman_competitor_plk_source_update_mode
    ON vidman_competitor_price_list_sources (update_mode);
