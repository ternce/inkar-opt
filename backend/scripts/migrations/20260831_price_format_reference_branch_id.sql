-- Adds an optional calculation/reference branch for price formats.
-- This migration does not move any production PriceFormat to another branch.

ALTER TABLE price_formats
    ADD COLUMN IF NOT EXISTS reference_branch_id TEXT DEFAULT '';
