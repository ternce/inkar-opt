BEGIN;

ALTER TABLE competitor_match_snapshots
    ADD COLUMN IF NOT EXISTS matching_context_key VARCHAR(64) NOT NULL DEFAULT '';

ALTER TABLE competitor_match_jobs
    ADD COLUMN IF NOT EXISTS matching_context_key VARCHAR(64) NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS ix_comp_match_snapshot_source_context_status
    ON competitor_match_snapshots (source_version_id, matching_context_key, status);

CREATE INDEX IF NOT EXISTS ix_comp_match_job_context
    ON competitor_match_jobs (matching_context_key, status);

COMMIT;
