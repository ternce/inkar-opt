-- Phase 2B.2 job lease and stale-dependency fields. Apply transactionally
-- after 20261014_phase2b1_matching_cache_foundation.up.sql.

ALTER TABLE competitor_match_jobs
    ADD COLUMN dependency_fingerprint VARCHAR(64) NOT NULL DEFAULT '',
    ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3,
    ADD COLUMN lease_expires_at TIMESTAMP,
    ADD CONSTRAINT ck_comp_match_job_max_attempts CHECK (max_attempts >= 1);

CREATE INDEX ix_comp_match_job_recovery
    ON competitor_match_jobs (status, lease_expires_at);
