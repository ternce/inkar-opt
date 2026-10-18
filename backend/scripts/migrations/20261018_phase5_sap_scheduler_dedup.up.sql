-- Phase 5: durable, per-business-date SAP scheduler deduplication.
-- Apply after 20261016_phase5_stock_snapshots.up.sql.

CREATE TABLE IF NOT EXISTS sap_balance_scheduled_runs (
    id BIGSERIAL PRIMARY KEY,
    business_date DATE NOT NULL,
    branch_id TEXT NOT NULL,
    plant VARCHAR(16) NOT NULL,
    refresh_job_id INTEGER NOT NULL REFERENCES refresh_jobs(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_sap_scheduled_run_date_branch UNIQUE (business_date, branch_id),
    CONSTRAINT uq_sap_scheduled_run_date_plant UNIQUE (business_date, plant)
);

CREATE INDEX IF NOT EXISTS ix_sap_scheduled_runs_date
    ON sap_balance_scheduled_runs (business_date);
CREATE INDEX IF NOT EXISTS ix_sap_balance_scheduled_runs_refresh_job_id
    ON sap_balance_scheduled_runs (refresh_job_id);
