-- Reverses only the additive Phase 2B.1 foundation.  Existing pricing,
-- matching, source and assignment records are not touched.

DROP TABLE IF EXISTS price_format_competitor_materializations;
DROP TABLE IF EXISTS competitor_match_jobs;
DROP TABLE IF EXISTS competitor_match_snapshot_items;
DROP TABLE IF EXISTS competitor_match_snapshots;
DROP TABLE IF EXISTS competitor_match_dependency_revisions;
DROP TABLE IF EXISTS competitor_source_version_items;
DROP TABLE IF EXISTS competitor_source_versions;
