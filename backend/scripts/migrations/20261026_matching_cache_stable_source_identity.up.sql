BEGIN;

-- Matching writes product_id/match_* back to ordinary physical source rows.
-- They are derived outputs, not uploaded PLK content, and must not invalidate
-- lineage.  manual_vidman is the exception: those fields are authoritative
-- source input and continue to invalidate its publication.
CREATE OR REPLACE FUNCTION invalidate_comp_source_publications_from_updated_rows()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE competitor_source_publications AS publication
    SET status = 'failed',
        error = 'physical source rows changed before lineage republication',
        failed_at = CURRENT_TIMESTAMP
    WHERE publication.status = 'published'
      AND publication.competitor_price_list_id IN (
          SELECT DISTINCT changed.price_list_id
          FROM (
              SELECT old_row.price_list_id
              FROM old_source_rows AS old_row
              JOIN new_source_rows AS new_row USING (id)
              JOIN competitor_price_lists AS source
                ON source.id = old_row.price_list_id
              WHERE ROW(
                  old_row.price_list_id, old_row.provisor_id,
                  old_row.provisor_goods_id, old_row.filial_id, old_row.name,
                  old_row.reg_number, old_row.distributor_goods_name,
                  old_row.distributor_goods_id, old_row.distributor_price,
                  old_row.stock, old_row.package_count, old_row.expiry_date,
                  old_row.raw_name, old_row.raw_manufacturer,
                  old_row.normalized_name, old_row.normalized_manufacturer,
                  old_row.parsed_base_name, old_row.parsed_form,
                  old_row.parsed_forms_json, old_row.parsed_dosage,
                  old_row.parsed_dosage_volume, old_row.parsed_quantity,
                  old_row.parsed_volume, old_row.parsed_weight,
                  old_row.parsed_percent_strength, old_row.parsed_concentration,
                  old_row.parsed_iu_dosage, old_row.parsed_strength_signature,
                  old_row.parsed_dimensions_json,
                  old_row.parsed_critical_tokens_json, old_row.raw_json
              ) IS DISTINCT FROM ROW(
                  new_row.price_list_id, new_row.provisor_id,
                  new_row.provisor_goods_id, new_row.filial_id, new_row.name,
                  new_row.reg_number, new_row.distributor_goods_name,
                  new_row.distributor_goods_id, new_row.distributor_price,
                  new_row.stock, new_row.package_count, new_row.expiry_date,
                  new_row.raw_name, new_row.raw_manufacturer,
                  new_row.normalized_name, new_row.normalized_manufacturer,
                  new_row.parsed_base_name, new_row.parsed_form,
                  new_row.parsed_forms_json, new_row.parsed_dosage,
                  new_row.parsed_dosage_volume, new_row.parsed_quantity,
                  new_row.parsed_volume, new_row.parsed_weight,
                  new_row.parsed_percent_strength, new_row.parsed_concentration,
                  new_row.parsed_iu_dosage, new_row.parsed_strength_signature,
                  new_row.parsed_dimensions_json,
                  new_row.parsed_critical_tokens_json, new_row.raw_json
              )
              OR (
                  source.source_type = 'manual_vidman'
                  AND ROW(
                      old_row.product_id, old_row.match_key, old_row.match_type,
                      old_row.match_score, old_row.matched_sku
                  ) IS DISTINCT FROM ROW(
                      new_row.product_id, new_row.match_key, new_row.match_type,
                      new_row.match_score, new_row.matched_sku
                  )
              )
              UNION
              SELECT new_row.price_list_id
              FROM old_source_rows AS old_row
              JOIN new_source_rows AS new_row USING (id)
              WHERE old_row.price_list_id IS DISTINCT FROM new_row.price_list_id
          ) AS changed
      );
    RETURN NULL;
END
$$;

COMMIT;
