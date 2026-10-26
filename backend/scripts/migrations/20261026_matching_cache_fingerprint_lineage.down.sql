BEGIN;

ALTER TABLE competitor_source_publications
    DROP COLUMN IF EXISTS fingerprint_format_version;

COMMIT;
