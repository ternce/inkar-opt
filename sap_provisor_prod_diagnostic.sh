#!/usr/bin/env bash
set -euo pipefail

APP_TZ="$(docker exec --workdir /app/backend --env PYTHONPATH=/app/backend inkar_opt_web \
  python -c 'from app.timezone import app_timezone; print(app_timezone().key)')"
test -n "$APP_TZ"
printf 'Application timezone: %s\n' "$APP_TZ"

docker exec -i --env DIAGNOSTIC_APP_TIMEZONE="$APP_TZ" inkar_opt_db \
  sh -c 'exec psql -X --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 --set=app_timezone="$DIAGNOSTIC_APP_TIMEZONE" --pset=pager=off' <<'SQL'
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '60s';
SET LOCAL lock_timeout = '5s';

\echo '=== Transaction and application timezone ==='
SHOW transaction_isolation;
SHOW transaction_read_only;
SELECT :'app_timezone' application_timezone,
       (CURRENT_TIMESTAMP AT TIME ZONE :'app_timezone') application_time;

\echo '=== Independent SAP matching-source batches ==='
SELECT id,source_version,source_type,nullif(plant,'') plant,lgort,effective_date,
       active,record_count,imported_by,imported_at,payload_sha256
FROM sap_matching_source_batches
ORDER BY active DESC,plant,imported_at DESC;

\echo '=== Active source provenance and identity quality ==='
WITH allowed(plant) AS (VALUES (''),('1001'),('1002'),('1003'),('1004'),('1005'),('1006'),
                               ('1007'),('1008'),('1009'),('1010'),('1011'),('1013'))
SELECT b.plant,b.lgort,b.source_version,b.source_type,b.record_count,
 count(i.id) persisted_items,
 count(*) FILTER (WHERE i.identity_status='resolved') resolved_rows,
 count(*) FILTER (WHERE i.identity_status='missing') missing_rows,
 count(*) FILTER (WHERE i.identity_status='ambiguous') ambiguous_rows,
 CASE WHEN b.lgort<>'1000' THEN 'INVALID_WAREHOUSE'
      WHEN b.plant NOT IN (SELECT plant FROM allowed) THEN 'UNAPPROVED_PLANT'
      WHEN count(i.id)<>b.record_count THEN 'COUNT_MISMATCH'
      ELSE 'VALID' END health
FROM sap_matching_source_batches b
LEFT JOIN sap_matching_source_items i ON i.batch_id=b.id
WHERE b.active
GROUP BY b.id
ORDER BY b.plant,b.source_version;

\echo '=== Exact deduplicated matching population ==='
WITH allowed(plant) AS (VALUES (''),('1001'),('1002'),('1003'),('1004'),('1005'),('1006'),
                               ('1007'),('1008'),('1009'),('1010'),('1011'),('1013')),
active_rows AS (
 SELECT i.* FROM sap_matching_source_items i
 JOIN sap_matching_source_batches b ON b.id=i.batch_id
 WHERE b.active AND b.lgort='1000' AND b.plant IN (SELECT plant FROM allowed)
),
unique_source AS (
 SELECT normalized_sku,min(material) material,
        count(DISTINCT material) material_variants,
        count(DISTINCT product_id) product_variants,
        min(product_id) product_id,
        bool_or(identity_status='missing') has_missing,
        bool_or(identity_status='ambiguous') has_ambiguous
 FROM active_rows GROUP BY normalized_sku
),
classified AS (
 SELECT u.*,p.provisor_goods_id,
   EXISTS(SELECT 1 FROM competitor_code_mappings c
          WHERE c.platform='provisor' AND c.status='mapped' AND c.our_product_id=u.product_id) canonical_mapped
 FROM unique_source u LEFT JOIN products p ON p.id=u.product_id
)
SELECT count(*) unique_sap_skus,
 count(*) FILTER(WHERE product_id IS NOT NULL AND product_variants=1 AND NOT has_ambiguous) resolved_internal_products,
 count(*) FILTER(WHERE has_missing) missing_internal_identity,
 count(*) FILTER(WHERE has_ambiguous OR material_variants>1 OR product_variants>1) ambiguous_internal_identity,
 count(*) FILTER(WHERE provisor_goods_id IS NOT NULL) direct_goods_id,
 count(*) FILTER(WHERE provisor_goods_id IS NULL AND canonical_mapped) canonical_only,
 count(*) FILTER(WHERE product_id IS NOT NULL AND product_variants=1 AND NOT has_ambiguous
                  AND provisor_goods_id IS NULL AND NOT canonical_mapped) genuinely_unmatched
FROM classified;

\echo '=== Duplicate cross-branch evidence (expected and deduplicated by normalized SKU) ==='
SELECT i.normalized_sku,min(i.material) material,count(DISTINCT b.plant) plant_count,
       string_agg(DISTINCT nullif(b.plant,''),',' ORDER BY nullif(b.plant,'')) plants
FROM sap_matching_source_items i JOIN sap_matching_source_batches b ON b.id=i.batch_id
WHERE b.active AND b.lgort='1000'
GROUP BY i.normalized_sku HAVING count(DISTINCT b.plant)>1
ORDER BY plant_count DESC,i.normalized_sku LIMIT 500;

\echo '=== Published stock remains a separate subsystem ==='
SELECT s.source_type,count(DISTINCT s.id) active_snapshot_count,count(DISTINCT r.branch_id) branch_count
FROM reference_update_statuses r JOIN stock_snapshots s ON s.id=r.active_stock_snapshot_id
WHERE r.data_type='stock' GROUP BY s.source_type ORDER BY s.source_type;

ROLLBACK;
SQL
