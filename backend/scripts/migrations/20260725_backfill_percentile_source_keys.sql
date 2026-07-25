-- Idempotent backfill for competitor_price_percentiles.source_key.
-- Only updates blank percentile source keys when exactly one active physical
-- assignment matches the same price format, branch, and competitor.
--
-- Rollback strategy:
--   Restore from backup, or revert only rows updated by this migration using
--   the pre-migration verification export. The old blank identity is unsafe,
--   so a blind downgrade that clears source_key is intentionally not included.

WITH candidates AS (
    SELECT
        cpp.price_format_id,
        cpp.branch_name,
        cpp.competitor_name,
        CASE
            WHEN btrim(coalesce(cpl.source_key, '')) <> '' THEN btrim(cpl.source_key)
            WHEN cpl.source_type = 'provisor'
                 AND btrim(coalesce(cpl.account_id, '')) <> ''
                 AND btrim(coalesce(cpl.external_price_list_id, '')) <> ''
                THEN btrim(cpl.account_id) || ':' || btrim(cpl.external_price_list_id)
            WHEN cpl.source_type = 'provisor'
                 AND btrim(coalesce(cpl.external_price_list_id, '')) <> ''
                THEN 'plk:' || btrim(cpl.external_price_list_id)
            WHEN cpl.source_type = 'vidman'
                 AND btrim(coalesce(cpl.account_id, '')) <> ''
                 AND btrim(coalesce(cpl.external_price_list_id, '')) <> ''
                THEN btrim(cpl.account_id) || ':' || btrim(cpl.external_price_list_id)
            ELSE ''
        END AS canonical_source_key
    FROM competitor_price_percentiles cpp
    JOIN price_format_competitor_assignments a
      ON a.price_format_id = cpp.price_format_id
     AND a.is_active IS TRUE
    JOIN competitor_price_lists cpl
      ON cpl.id = a.competitor_price_list_id
     AND cpl.branch_name = cpp.branch_name
     AND cpl.competitor_name = cpp.competitor_name
    WHERE btrim(coalesce(cpp.source_key, '')) = ''
),
unambiguous AS (
    SELECT
        price_format_id,
        branch_name,
        competitor_name,
        min(canonical_source_key) AS canonical_source_key
    FROM candidates
    WHERE canonical_source_key <> ''
    GROUP BY price_format_id, branch_name, competitor_name
    HAVING count(DISTINCT canonical_source_key) = 1
)
UPDATE competitor_price_percentiles cpp
SET source_key = u.canonical_source_key
FROM unambiguous u
WHERE cpp.price_format_id = u.price_format_id
  AND cpp.branch_name = u.branch_name
  AND cpp.competitor_name = u.competitor_name
  AND btrim(coalesce(cpp.source_key, '')) = '';

-- Ambiguous groups to review after running the migration:
--
-- SELECT cpp.price_format_id, cpp.branch_name, cpp.competitor_name, count(*) AS rows_count
-- FROM competitor_price_percentiles cpp
-- WHERE btrim(coalesce(cpp.source_key, '')) = ''
-- GROUP BY cpp.price_format_id, cpp.branch_name, cpp.competitor_name
-- ORDER BY rows_count DESC;
