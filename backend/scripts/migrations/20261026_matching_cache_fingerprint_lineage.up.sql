BEGIN;

ALTER TABLE competitor_source_publications
    ADD COLUMN IF NOT EXISTS fingerprint_format_version VARCHAR(64) NOT NULL DEFAULT '';

COMMIT;
