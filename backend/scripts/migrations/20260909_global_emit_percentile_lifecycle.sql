-- Normalize direct Emit competitor PLKs as global catalog sources.
-- The compatibility percentile tables still keep price_format_id-scoped rows
-- for pricing/export readers; the source PLK itself is no longer owned by the
-- price format that happened to trigger the refresh.

UPDATE competitor_price_lists
SET price_format_id = NULL
WHERE source_key LIKE 'emit:%';

UPDATE price_format_percentile_preparations p
SET status = 'ready',
    last_error = '',
    failed_at = NULL,
    completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
    updated_at = CURRENT_TIMESTAMP,
    rows_count = (
        SELECT COUNT(*)
        FROM competitor_price_percentiles cpp
        WHERE cpp.price_format_id = p.price_format_id
    )
WHERE EXISTS (
    SELECT 1
    FROM competitor_price_percentiles cpp
    WHERE cpp.price_format_id = p.price_format_id
);
