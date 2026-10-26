BEGIN;

-- Restore the Phase 3B behavior: only explicitly manual/approved mappings
-- invalidate matching-cache dependencies.
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

COMMIT;
