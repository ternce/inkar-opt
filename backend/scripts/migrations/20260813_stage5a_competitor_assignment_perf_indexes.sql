-- Stage 5A Competitor Assignment read-path indexes.
-- Run outside an explicit transaction on PostgreSQL because these use
-- CREATE INDEX CONCURRENTLY.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_cpli_pl_positive_matched_count
ON competitor_price_list_items (price_list_id)
WHERE distributor_price IS NOT NULL
  AND distributor_price > 0
  AND (
      product_id IS NOT NULL
      OR provisor_goods_id IS NOT NULL
      OR matched_sku <> ''
      OR distributor_goods_id <> ''
  );

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_rcpp_source_summary
ON regular_competitor_price_percentiles
    (competitor_identity, percentile, product_id)
INCLUDE (competitor_name, source_count, calculated_at);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_cpp_source_summary
ON competitor_price_percentiles (
    price_format_id,
    source_key,
    branch_name,
    competitor_name,
    percentile_scope,
    percentile,
    product_id
)
INCLUDE (
    source_type,
    competitor_price_list_id,
    source_count,
    updated_at
);
