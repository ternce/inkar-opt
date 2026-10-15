DROP INDEX IF EXISTS ix_comp_match_job_recovery;

ALTER TABLE competitor_match_jobs
    DROP CONSTRAINT IF EXISTS ck_comp_match_job_max_attempts,
    DROP COLUMN IF EXISTS lease_expires_at,
    DROP COLUMN IF EXISTS max_attempts,
    DROP COLUMN IF EXISTS dependency_fingerprint;
