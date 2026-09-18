-- PostgreSQL production migration. Worker IDs are supplied to the separate
-- reconciliation command, never stored as migration constants.
CREATE TABLE IF NOT EXISTS manual_matching_assignments (
    id BIGSERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    assigned_user_id INTEGER NOT NULL REFERENCES app_users(id),
    assigned_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP NULL,
    completed_by_user_id INTEGER NULL REFERENCES app_users(id),
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_manual_matching_assignments_product_id UNIQUE (product_id),
    CONSTRAINT ck_manual_matching_assignments_status CHECK (status IN ('active', 'completed'))
);

CREATE INDEX IF NOT EXISTS ix_manual_matching_assignments_assigned_user_id
    ON manual_matching_assignments (assigned_user_id);
CREATE INDEX IF NOT EXISTS ix_manual_matching_assignments_status
    ON manual_matching_assignments (status);
CREATE INDEX IF NOT EXISTS ix_manual_matching_assignment_owner_status
    ON manual_matching_assignments (assigned_user_id, status);
