DROP TRIGGER IF EXISTS trg_comp_source_rows_update_invalidate ON competitor_price_list_items;
DROP FUNCTION IF EXISTS invalidate_comp_source_publications_from_updated_rows();

DROP TRIGGER IF EXISTS trg_match_cache_products_update ON products;
DROP TRIGGER IF EXISTS trg_match_cache_product_extras_update ON product_extras;
DROP TRIGGER IF EXISTS trg_match_cache_internal_normalized_update ON internal_product_normalized;
DROP TRIGGER IF EXISTS trg_match_cache_competitor_code_mappings_update ON competitor_code_mappings;
DROP TRIGGER IF EXISTS trg_match_cache_provisor_goods_map_update ON provisor_goods_map;
DROP TRIGGER IF EXISTS trg_match_cache_product_substitutes_update ON product_substitute_matches;
DROP TRIGGER IF EXISTS trg_match_cache_vidman_matches_update ON vidman_product_matches;

DROP FUNCTION IF EXISTS matching_cache_invalidate_products_update();
DROP FUNCTION IF EXISTS matching_cache_invalidate_product_extras_update();
DROP FUNCTION IF EXISTS matching_cache_invalidate_internal_normalized_update();
DROP FUNCTION IF EXISTS matching_cache_invalidate_code_mappings_update();
DROP FUNCTION IF EXISTS matching_cache_invalidate_provisor_references_update();
DROP FUNCTION IF EXISTS matching_cache_invalidate_substitutes_update();
DROP FUNCTION IF EXISTS matching_cache_invalidate_vidman_update();

-- Restore the Phase 2B.3 fail-closed trigger definitions.
DROP TRIGGER IF EXISTS trg_match_cache_products ON products;
CREATE TRIGGER trg_match_cache_products
AFTER INSERT OR UPDATE OR DELETE ON products
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_catalog_and_reference();

DROP TRIGGER IF EXISTS trg_match_cache_product_extras ON product_extras;
CREATE TRIGGER trg_match_cache_product_extras
AFTER INSERT OR UPDATE OR DELETE ON product_extras
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_catalog();

DROP TRIGGER IF EXISTS trg_match_cache_internal_normalized ON internal_product_normalized;
CREATE TRIGGER trg_match_cache_internal_normalized
AFTER INSERT OR UPDATE OR DELETE ON internal_product_normalized
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_catalog();

DROP TRIGGER IF EXISTS trg_match_cache_competitor_code_mappings ON competitor_code_mappings;
CREATE TRIGGER trg_match_cache_competitor_code_mappings
AFTER INSERT OR UPDATE OR DELETE ON competitor_code_mappings
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_manual_mappings();

DROP TRIGGER IF EXISTS trg_match_cache_provisor_goods_map ON provisor_goods_map;
CREATE TRIGGER trg_match_cache_provisor_goods_map
AFTER INSERT OR UPDATE OR DELETE ON provisor_goods_map
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_provisor_references();

DROP TRIGGER IF EXISTS trg_match_cache_product_substitutes ON product_substitute_matches;
CREATE TRIGGER trg_match_cache_product_substitutes
AFTER INSERT OR UPDATE OR DELETE ON product_substitute_matches
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_provisor_substitutes();

DROP TRIGGER IF EXISTS trg_match_cache_vidman_matches ON vidman_product_matches;
CREATE TRIGGER trg_match_cache_vidman_matches
AFTER INSERT OR UPDATE OR DELETE ON vidman_product_matches
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_vidman_authoritative();

CREATE OR REPLACE FUNCTION matching_cache_invalidate_approved_source_goods()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    old_method TEXT := CASE WHEN TG_OP = 'INSERT' THEN '' ELSE COALESCE(OLD.match_method, '') END;
    new_method TEXT := CASE WHEN TG_OP = 'DELETE' THEN '' ELSE COALESCE(NEW.match_method, '') END;
BEGIN
    IF lower(old_method) LIKE '%manual%' OR lower(old_method) LIKE '%approved%'
       OR lower(new_method) LIKE '%manual%' OR lower(new_method) LIKE '%approved%' THEN
        PERFORM matching_cache_bump_dependency('manual_code_mappings');
    END IF;
    RETURN NULL;
END
$$;
