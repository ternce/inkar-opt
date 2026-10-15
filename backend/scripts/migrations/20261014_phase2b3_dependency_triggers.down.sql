DROP TRIGGER IF EXISTS trg_match_cache_approved_source_goods ON source_goods_matches;
DROP TRIGGER IF EXISTS trg_match_cache_vidman_matches ON vidman_product_matches;
DROP TRIGGER IF EXISTS trg_match_cache_product_substitutes ON product_substitute_matches;
DROP TRIGGER IF EXISTS trg_match_cache_provisor_goods_map ON provisor_goods_map;
DROP TRIGGER IF EXISTS trg_match_cache_competitor_code_mappings ON competitor_code_mappings;
DROP TRIGGER IF EXISTS trg_match_cache_internal_normalized ON internal_product_normalized;
DROP TRIGGER IF EXISTS trg_match_cache_product_extras ON product_extras;
DROP TRIGGER IF EXISTS trg_match_cache_products ON products;

DROP FUNCTION IF EXISTS matching_cache_invalidate_approved_source_goods();
DROP FUNCTION IF EXISTS matching_cache_invalidate_vidman_authoritative();
DROP FUNCTION IF EXISTS matching_cache_invalidate_provisor_substitutes();
DROP FUNCTION IF EXISTS matching_cache_invalidate_provisor_references();
DROP FUNCTION IF EXISTS matching_cache_invalidate_manual_mappings();
DROP FUNCTION IF EXISTS matching_cache_invalidate_catalog();
DROP FUNCTION IF EXISTS matching_cache_invalidate_catalog_and_reference();
DROP FUNCTION IF EXISTS matching_cache_bump_dependency(TEXT);
