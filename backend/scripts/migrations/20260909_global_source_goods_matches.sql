ALTER TABLE source_goods_matches
    ALTER COLUMN price_format_id DROP NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_goods_match_global_goods
    ON source_goods_matches (
        source_type,
        goods_id
    )
    WHERE price_format_id IS NULL
      AND goods_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_goods_match_global_sku
    ON source_goods_matches (
        source_type,
        distributor_goods_id
    )
    WHERE price_format_id IS NULL
      AND goods_id IS NULL
      AND distributor_goods_id <> ''
      AND distributor_goods_id <> '0';

CREATE INDEX IF NOT EXISTS ix_source_goods_match_global_lookup
    ON source_goods_matches (
        source_type,
        goods_id,
        product_id
    )
    WHERE price_format_id IS NULL;