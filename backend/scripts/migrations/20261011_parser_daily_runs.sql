ALTER TABLE price_source_accounts
    ADD COLUMN IF NOT EXISTS run_in_morning_pipeline BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS parser_daily_runs (
    id SERIAL PRIMARY KEY,
    business_date DATE NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    emit_job_id INTEGER REFERENCES refresh_jobs(id),
    emit_attempt_count INTEGER NOT NULL DEFAULT 0,
    plk_job_id INTEGER REFERENCES refresh_jobs(id),
    suspicious_reason TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL DEFAULT '',
    trigger_source VARCHAR(32) NOT NULL DEFAULT 'manual',
    notification_status VARCHAR(32),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    CONSTRAINT uq_parser_daily_runs_business_date UNIQUE (business_date),
    CONSTRAINT ck_parser_daily_runs_status CHECK (
        status IN ('PENDING','EMIT_RUNNING','EMIT_VALIDATING','EMIT_SUSPICIOUS','EMIT_RETRYING','PLK_RUNNING','SUCCESS','SUSPICIOUS','FAILED','SKIPPED_ALREADY_RUNNING')
    )
);

ALTER TABLE refresh_jobs
    ADD COLUMN IF NOT EXISTS daily_run_id INTEGER REFERENCES parser_daily_runs(id);

CREATE INDEX IF NOT EXISTS ix_parser_daily_runs_business_date
    ON parser_daily_runs (business_date);
CREATE INDEX IF NOT EXISTS ix_parser_daily_runs_status
    ON parser_daily_runs (status);
CREATE INDEX IF NOT EXISTS ix_parser_daily_runs_status_updated
    ON parser_daily_runs (status, updated_at);
CREATE INDEX IF NOT EXISTS ix_refresh_jobs_daily_run_id
    ON refresh_jobs (daily_run_id);
