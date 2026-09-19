-- Supports current-stock priority aggregation and urgent-task existence checks.
-- Run outside a transaction because PostgreSQL forbids CONCURRENTLY in one.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_branch_stock_positive_product_branch
    ON branch_stock (product_id, branch_id)
    WHERE stock > 0;
