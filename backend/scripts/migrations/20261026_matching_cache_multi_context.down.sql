BEGIN;

DROP INDEX IF EXISTS ix_comp_match_job_context;
DROP INDEX IF EXISTS ix_comp_match_snapshot_source_context_status;

ALTER TABLE competitor_match_jobs
    DROP COLUMN IF EXISTS matching_context_key;

ALTER TABLE competitor_match_snapshots
    DROP COLUMN IF EXISTS matching_context_key;

COMMIT;
