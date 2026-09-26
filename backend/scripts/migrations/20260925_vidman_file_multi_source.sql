ALTER TABLE competitor_price_lists
    ADD COLUMN IF NOT EXISTS update_mode VARCHAR(16) NOT NULL DEFAULT 'auto';

UPDATE competitor_price_lists
SET update_mode = 'auto'
WHERE update_mode IS NULL OR BTRIM(update_mode) = '';

CREATE INDEX IF NOT EXISTS ix_competitor_price_lists_update_mode
    ON competitor_price_lists (update_mode);
