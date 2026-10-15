-- Phase 2B.3 fail-safe invalidation for Core SQL, raw SQL and external writers.
-- Apply transactionally after the Phase 2B.1 and 2B.2 migrations.

CREATE OR REPLACE FUNCTION matching_cache_bump_dependency(p_scope TEXT)
RETURNS VOID
LANGUAGE plpgsql
AS $$
DECLARE
    setting_name TEXT := 'matching_cache.invalidated_' || p_scope;
BEGIN
    -- One revision per dependency scope per transaction is sufficient and
    -- prevents bulk INSERT/DELETE and row-trigger paths from revision storms.
    IF current_setting(setting_name, TRUE) = '1' THEN
        RETURN;
    END IF;
    PERFORM set_config(setting_name, '1', TRUE);
    INSERT INTO competitor_match_dependency_revisions
        (scope_type, scope_key, revision, fingerprint, metadata_json, created_at, updated_at)
    VALUES (p_scope, 'global', 1, '', '{"writer":"database_trigger"}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
    ON CONFLICT (scope_type, scope_key) DO UPDATE
    SET revision = competitor_match_dependency_revisions.revision + 1,
        metadata_json = EXCLUDED.metadata_json,
        updated_at = EXCLUDED.updated_at;
END;
$$;

CREATE OR REPLACE FUNCTION matching_cache_invalidate_catalog_and_reference()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    PERFORM matching_cache_bump_dependency('product_catalog');
    PERFORM matching_cache_bump_dependency('provisor_references');
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION matching_cache_invalidate_catalog()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    PERFORM matching_cache_bump_dependency('product_catalog');
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION matching_cache_invalidate_manual_mappings()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    PERFORM matching_cache_bump_dependency('manual_code_mappings');
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION matching_cache_invalidate_provisor_references()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    PERFORM matching_cache_bump_dependency('provisor_references');
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION matching_cache_invalidate_provisor_substitutes()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    PERFORM matching_cache_bump_dependency('provisor_substitutes');
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION matching_cache_invalidate_vidman_authoritative()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    PERFORM matching_cache_bump_dependency('vidman_authoritative');
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION matching_cache_invalidate_approved_source_goods()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    old_method TEXT := CASE WHEN TG_OP = 'INSERT' THEN '' ELSE COALESCE(OLD.match_method, '') END;
    new_method TEXT := CASE WHEN TG_OP = 'DELETE' THEN '' ELSE COALESCE(NEW.match_method, '') END;
BEGIN
    IF lower(old_method) LIKE '%manual%' OR lower(old_method) LIKE '%approved%'
       OR lower(new_method) LIKE '%manual%' OR lower(new_method) LIKE '%approved%' THEN
        PERFORM matching_cache_bump_dependency('manual_code_mappings');
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER trg_match_cache_products
AFTER INSERT OR UPDATE OR DELETE ON products
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_catalog_and_reference();

CREATE TRIGGER trg_match_cache_product_extras
AFTER INSERT OR UPDATE OR DELETE ON product_extras
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_catalog();

CREATE TRIGGER trg_match_cache_internal_normalized
AFTER INSERT OR UPDATE OR DELETE ON internal_product_normalized
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_catalog();

CREATE TRIGGER trg_match_cache_competitor_code_mappings
AFTER INSERT OR UPDATE OR DELETE ON competitor_code_mappings
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_manual_mappings();

CREATE TRIGGER trg_match_cache_provisor_goods_map
AFTER INSERT OR UPDATE OR DELETE ON provisor_goods_map
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_provisor_references();

CREATE TRIGGER trg_match_cache_product_substitutes
AFTER INSERT OR UPDATE OR DELETE ON product_substitute_matches
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_provisor_substitutes();

CREATE TRIGGER trg_match_cache_vidman_matches
AFTER INSERT OR UPDATE OR DELETE ON vidman_product_matches
FOR EACH STATEMENT EXECUTE FUNCTION matching_cache_invalidate_vidman_authoritative();

CREATE TRIGGER trg_match_cache_approved_source_goods
AFTER INSERT OR UPDATE OR DELETE ON source_goods_matches
FOR EACH ROW EXECUTE FUNCTION matching_cache_invalidate_approved_source_goods();
