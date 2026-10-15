-- Phase 3B: make cache invalidation precise enough for safe write reduction.
-- This migration is additive with respect to application data.  It replaces
-- trigger definitions only; cache reads remain controlled by existing flags.

-- A physical source UPDATE can change matching or materialization inputs while
-- retaining the same surrogate row id.  Invalidate both the old and new source
-- owners, but only when a canonical source-content field actually changed.
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
              WHERE ROW(
                  old_row.price_list_id,
                  old_row.product_id,
                  old_row.provisor_id,
                  old_row.provisor_goods_id,
                  old_row.filial_id,
                  old_row.name,
                  old_row.reg_number,
                  old_row.distributor_goods_name,
                  old_row.distributor_goods_id,
                  old_row.distributor_price,
                  old_row.stock,
                  old_row.package_count,
                  old_row.expiry_date,
                  old_row.match_key,
                  old_row.match_type,
                  old_row.match_score,
                  old_row.matched_sku,
                  old_row.raw_name,
                  old_row.raw_manufacturer,
                  old_row.normalized_name,
                  old_row.normalized_manufacturer,
                  old_row.parsed_base_name,
                  old_row.parsed_form,
                  old_row.parsed_forms_json,
                  old_row.parsed_dosage,
                  old_row.parsed_dosage_volume,
                  old_row.parsed_quantity,
                  old_row.parsed_volume,
                  old_row.parsed_weight,
                  old_row.parsed_percent_strength,
                  old_row.parsed_concentration,
                  old_row.parsed_iu_dosage,
                  old_row.parsed_strength_signature,
                  old_row.parsed_dimensions_json,
                  old_row.parsed_critical_tokens_json,
                  old_row.raw_json
              ) IS DISTINCT FROM ROW(
                  new_row.price_list_id,
                  new_row.product_id,
                  new_row.provisor_id,
                  new_row.provisor_goods_id,
                  new_row.filial_id,
                  new_row.name,
                  new_row.reg_number,
                  new_row.distributor_goods_name,
                  new_row.distributor_goods_id,
                  new_row.distributor_price,
                  new_row.stock,
                  new_row.package_count,
                  new_row.expiry_date,
                  new_row.match_key,
                  new_row.match_type,
                  new_row.match_score,
                  new_row.matched_sku,
                  new_row.raw_name,
                  new_row.raw_manufacturer,
                  new_row.normalized_name,
                  new_row.normalized_manufacturer,
                  new_row.parsed_base_name,
                  new_row.parsed_form,
                  new_row.parsed_forms_json,
                  new_row.parsed_dosage,
                  new_row.parsed_dosage_volume,
                  new_row.parsed_quantity,
                  new_row.parsed_volume,
                  new_row.parsed_weight,
                  new_row.parsed_percent_strength,
                  new_row.parsed_concentration,
                  new_row.parsed_iu_dosage,
                  new_row.parsed_strength_signature,
                  new_row.parsed_dimensions_json,
                  new_row.parsed_critical_tokens_json,
                  new_row.raw_json
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

DROP TRIGGER IF EXISTS trg_comp_source_rows_update_invalidate ON competitor_price_list_items;
CREATE TRIGGER trg_comp_source_rows_update_invalidate
AFTER UPDATE ON competitor_price_list_items
REFERENCING OLD TABLE AS old_source_rows NEW TABLE AS new_source_rows
FOR EACH STATEMENT
EXECUTE FUNCTION invalidate_comp_source_publications_from_updated_rows();

-- INSERT and DELETE retain their prior fail-closed, statement-level behavior.
-- UPDATE is field-sensitive and uses transition tables so a bulk write bumps a
-- dependency scope at most once (matching_cache_bump_dependency also de-dupes
-- scopes across all statements in the transaction).

CREATE OR REPLACE FUNCTION matching_cache_invalidate_products_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM old_rows o FULL JOIN new_rows n USING (id)
        WHERE o.id IS NULL OR n.id IS NULL
           OR ROW(o.code, o.name, o.top_rank, o.provisor_goods_id)
              IS DISTINCT FROM ROW(n.code, n.name, n.top_rank, n.provisor_goods_id)
    ) THEN
        PERFORM matching_cache_bump_dependency('product_catalog');
    END IF;
    IF EXISTS (
        SELECT 1 FROM old_rows o FULL JOIN new_rows n USING (id)
        WHERE o.id IS NULL OR n.id IS NULL
           OR o.provisor_goods_id IS DISTINCT FROM n.provisor_goods_id
    ) THEN
        PERFORM matching_cache_bump_dependency('provisor_references');
    END IF;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION matching_cache_invalidate_product_extras_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM old_rows o FULL JOIN new_rows n USING (product_id)
        WHERE o.product_id IS NULL OR n.product_id IS NULL
           OR o.manufacturer IS DISTINCT FROM n.manufacturer
    ) THEN
        PERFORM matching_cache_bump_dependency('product_catalog');
    END IF;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION matching_cache_invalidate_internal_normalized_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM old_rows o FULL JOIN new_rows n USING (id)
        WHERE o.id IS NULL OR n.id IS NULL OR ROW(
            o.product_id, o.raw_name, o.raw_manufacturer, o.normalized_name,
            o.base_name, o.normalized_manufacturer, o.dosage_value,
            o.dosage_unit, o.strength_components, o.concentration_value,
            o.concentration_unit, o.volume_value, o.volume_unit,
            o.weight_value, o.weight_unit, o.package_volume, o.package_weight,
            o.pack_count, o.dosage_form, o.variant_text,
            o.identity_tokens_json, o.normalized_signature,
            o.parse_warnings_json, o.parse_confidence
        ) IS DISTINCT FROM ROW(
            n.product_id, n.raw_name, n.raw_manufacturer, n.normalized_name,
            n.base_name, n.normalized_manufacturer, n.dosage_value,
            n.dosage_unit, n.strength_components, n.concentration_value,
            n.concentration_unit, n.volume_value, n.volume_unit,
            n.weight_value, n.weight_unit, n.package_volume, n.package_weight,
            n.pack_count, n.dosage_form, n.variant_text,
            n.identity_tokens_json, n.normalized_signature,
            n.parse_warnings_json, n.parse_confidence
        )
    ) THEN
        PERFORM matching_cache_bump_dependency('product_catalog');
    END IF;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION matching_cache_invalidate_code_mappings_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM old_rows o FULL JOIN new_rows n USING (id)
        WHERE o.id IS NULL OR n.id IS NULL OR ROW(
            o.platform, o.source_external_key, o.source_match_key,
            o.source_name, o.source_manufacturer, o.source_dosage_form,
            o.source_normalized_name, o.our_product_id, o.our_sku,
            o.status, o.confidence
        ) IS DISTINCT FROM ROW(
            n.platform, n.source_external_key, n.source_match_key,
            n.source_name, n.source_manufacturer, n.source_dosage_form,
            n.source_normalized_name, n.our_product_id, n.our_sku,
            n.status, n.confidence
        )
    ) THEN
        PERFORM matching_cache_bump_dependency('manual_code_mappings');
    END IF;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION matching_cache_invalidate_provisor_references_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM old_rows o FULL JOIN new_rows n USING (id)
        WHERE o.id IS NULL OR n.id IS NULL
           OR ROW(o.price_format_id, o.goods_id, o.product_id)
          IS DISTINCT FROM ROW(n.price_format_id, n.goods_id, n.product_id)
    ) THEN
        PERFORM matching_cache_bump_dependency('provisor_references');
    END IF;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION matching_cache_invalidate_substitutes_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM old_rows o FULL JOIN new_rows n USING (id)
        WHERE o.id IS NULL OR n.id IS NULL OR ROW(
            o.product_id, o.source_type, o.source_goods_id,
            o.source_distributor_goods_id, o.source_name,
            o.source_manufacturer, o.status, o.priority
        ) IS DISTINCT FROM ROW(
            n.product_id, n.source_type, n.source_goods_id,
            n.source_distributor_goods_id, n.source_name,
            n.source_manufacturer, n.status, n.priority
        )
    ) THEN
        PERFORM matching_cache_bump_dependency('provisor_substitutes');
    END IF;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION matching_cache_invalidate_vidman_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM old_rows o FULL JOIN new_rows n USING (id)
        WHERE o.id IS NULL OR n.id IS NULL
           OR ROW(o.canonical_product_id, o.product_id, o.status, o.match_type, o.confidence)
          IS DISTINCT FROM ROW(n.canonical_product_id, n.product_id, n.status, n.match_type, n.confidence)
    ) THEN
        PERFORM matching_cache_bump_dependency('vidman_authoritative');
    END IF;
    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS trg_match_cache_products ON products;
DROP TRIGGER IF EXISTS trg_match_cache_products_update ON products;
CREATE TRIGGER trg_match_cache_products
AFTER INSERT OR DELETE ON products
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_catalog_and_reference();
CREATE TRIGGER trg_match_cache_products_update
AFTER UPDATE ON products
REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_products_update();

DROP TRIGGER IF EXISTS trg_match_cache_product_extras ON product_extras;
DROP TRIGGER IF EXISTS trg_match_cache_product_extras_update ON product_extras;
CREATE TRIGGER trg_match_cache_product_extras
AFTER INSERT OR DELETE ON product_extras
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_catalog();
CREATE TRIGGER trg_match_cache_product_extras_update
AFTER UPDATE ON product_extras
REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_product_extras_update();

DROP TRIGGER IF EXISTS trg_match_cache_internal_normalized ON internal_product_normalized;
DROP TRIGGER IF EXISTS trg_match_cache_internal_normalized_update ON internal_product_normalized;
CREATE TRIGGER trg_match_cache_internal_normalized
AFTER INSERT OR DELETE ON internal_product_normalized
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_catalog();
CREATE TRIGGER trg_match_cache_internal_normalized_update
AFTER UPDATE ON internal_product_normalized
REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_internal_normalized_update();

DROP TRIGGER IF EXISTS trg_match_cache_competitor_code_mappings ON competitor_code_mappings;
DROP TRIGGER IF EXISTS trg_match_cache_competitor_code_mappings_update ON competitor_code_mappings;
CREATE TRIGGER trg_match_cache_competitor_code_mappings
AFTER INSERT OR DELETE ON competitor_code_mappings
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_manual_mappings();
CREATE TRIGGER trg_match_cache_competitor_code_mappings_update
AFTER UPDATE ON competitor_code_mappings
REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_code_mappings_update();

DROP TRIGGER IF EXISTS trg_match_cache_provisor_goods_map ON provisor_goods_map;
DROP TRIGGER IF EXISTS trg_match_cache_provisor_goods_map_update ON provisor_goods_map;
CREATE TRIGGER trg_match_cache_provisor_goods_map
AFTER INSERT OR DELETE ON provisor_goods_map
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_provisor_references();
CREATE TRIGGER trg_match_cache_provisor_goods_map_update
AFTER UPDATE ON provisor_goods_map
REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_provisor_references_update();

DROP TRIGGER IF EXISTS trg_match_cache_product_substitutes ON product_substitute_matches;
DROP TRIGGER IF EXISTS trg_match_cache_product_substitutes_update ON product_substitute_matches;
CREATE TRIGGER trg_match_cache_product_substitutes
AFTER INSERT OR DELETE ON product_substitute_matches
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_provisor_substitutes();
CREATE TRIGGER trg_match_cache_product_substitutes_update
AFTER UPDATE ON product_substitute_matches
REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_substitutes_update();

DROP TRIGGER IF EXISTS trg_match_cache_vidman_matches ON vidman_product_matches;
DROP TRIGGER IF EXISTS trg_match_cache_vidman_matches_update ON vidman_product_matches;
CREATE TRIGGER trg_match_cache_vidman_matches
AFTER INSERT OR DELETE ON vidman_product_matches
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_vidman_authoritative();
CREATE TRIGGER trg_match_cache_vidman_matches_update
AFTER UPDATE ON vidman_product_matches
REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_vidman_update();

CREATE OR REPLACE FUNCTION matching_cache_invalidate_approved_source_goods()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    old_method TEXT := CASE WHEN TG_OP = 'INSERT' THEN '' ELSE COALESCE(OLD.match_method, '') END;
    new_method TEXT := CASE WHEN TG_OP = 'DELETE' THEN '' ELSE COALESCE(NEW.match_method, '') END;
    relevant_changed BOOLEAN := TRUE;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        relevant_changed := ROW(
            OLD.price_format_id, OLD.source_type, OLD.distributor_goods_id,
            OLD.goods_id, OLD.product_id, OLD.match_method
        ) IS DISTINCT FROM ROW(
            NEW.price_format_id, NEW.source_type, NEW.distributor_goods_id,
            NEW.goods_id, NEW.product_id, NEW.match_method
        );
    END IF;
    IF relevant_changed AND (
        lower(old_method) LIKE '%manual%' OR lower(old_method) LIKE '%approved%'
        OR lower(new_method) LIKE '%manual%' OR lower(new_method) LIKE '%approved%'
    ) THEN
        PERFORM matching_cache_bump_dependency('manual_code_mappings');
    END IF;
    RETURN NULL;
END
$$;
