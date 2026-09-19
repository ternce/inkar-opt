-- Targeted manual mapping updates use these durable source identities.
-- Run outside an explicit PostgreSQL transaction because these indexes are
-- built concurrently on the production-sized competitor item table.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_cpli_distributor_goods_id
ON competitor_price_list_items (distributor_goods_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_cpli_normalized_identity
ON competitor_price_list_items (normalized_name, normalized_manufacturer);
