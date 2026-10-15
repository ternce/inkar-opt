-- Additive physical lineage for immutable matching-cache source versions.
-- Cache reads remain disabled by default.

CREATE TABLE competitor_source_publications (
    id BIGSERIAL PRIMARY KEY,
    competitor_price_list_id INTEGER NOT NULL REFERENCES competitor_price_lists(id),
    source_version_id BIGINT NOT NULL REFERENCES competitor_source_versions(id),
    row_count BIGINT NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMP,
    failed_at TIMESTAMP,
    CONSTRAINT ck_comp_source_publication_status CHECK (
        status IN ('pending','published','failed','superseded')
    )
);

CREATE INDEX ix_competitor_source_publications_competitor_price_list_id
    ON competitor_source_publications (competitor_price_list_id);
CREATE INDEX ix_competitor_source_publications_source_version_id
    ON competitor_source_publications (source_version_id);
CREATE INDEX ix_competitor_source_publications_status
    ON competitor_source_publications (status);
CREATE INDEX ix_comp_source_publication_source_status
    ON competitor_source_publications (competitor_price_list_id, status, id);
CREATE UNIQUE INDEX uq_comp_source_publication_published
    ON competitor_source_publications (competitor_price_list_id)
    WHERE status = 'published';

CREATE TABLE competitor_source_publication_items (
    id BIGSERIAL PRIMARY KEY,
    source_publication_id BIGINT NOT NULL
        REFERENCES competitor_source_publications(id) ON DELETE CASCADE,
    source_version_item_id BIGINT NOT NULL
        REFERENCES competitor_source_version_items(id),
    physical_source_item_id INTEGER NOT NULL
        REFERENCES competitor_price_list_items(id) ON DELETE CASCADE,
    CONSTRAINT uq_comp_source_publication_version_item UNIQUE (
        source_publication_id, source_version_item_id
    ),
    CONSTRAINT uq_comp_source_publication_physical_item UNIQUE (
        source_publication_id, physical_source_item_id
    )
);

CREATE INDEX ix_competitor_source_publication_items_source_publication_id
    ON competitor_source_publication_items (source_publication_id);
CREATE INDEX ix_competitor_source_publication_items_source_version_item_id
    ON competitor_source_publication_items (source_version_item_id);
CREATE INDEX ix_competitor_source_publication_items_physical_source_item_id
    ON competitor_source_publication_items (physical_source_item_id);

CREATE OR REPLACE FUNCTION invalidate_comp_source_publications_from_rows()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE competitor_source_publications
    SET status = 'failed',
        error = 'physical source rows changed before lineage republication',
        failed_at = CURRENT_TIMESTAMP
    WHERE status = 'published'
      AND competitor_price_list_id IN (
          SELECT DISTINCT price_list_id FROM changed_source_rows
      );
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION invalidate_comp_source_publications_from_lineage()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE competitor_source_publications
    SET status = 'failed',
        error = 'physical source lineage became incomplete',
        failed_at = CURRENT_TIMESTAMP
    WHERE status = 'published'
      AND id IN (
          SELECT DISTINCT source_publication_id FROM deleted_lineage_rows
      );
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION invalidate_all_comp_source_publications()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE competitor_source_publications
    SET status = 'failed',
        error = 'physical source table was truncated before lineage republication',
        failed_at = CURRENT_TIMESTAMP
    WHERE status = 'published';
    RETURN NULL;
END
$$;

CREATE TRIGGER trg_comp_source_rows_insert_invalidate
AFTER INSERT ON competitor_price_list_items
REFERENCING NEW TABLE AS changed_source_rows
FOR EACH STATEMENT
EXECUTE FUNCTION invalidate_comp_source_publications_from_rows();

CREATE TRIGGER trg_comp_source_rows_delete_invalidate
AFTER DELETE ON competitor_price_list_items
REFERENCING OLD TABLE AS changed_source_rows
FOR EACH STATEMENT
EXECUTE FUNCTION invalidate_comp_source_publications_from_rows();

CREATE TRIGGER trg_comp_source_rows_truncate_invalidate
AFTER TRUNCATE ON competitor_price_list_items
FOR EACH STATEMENT
EXECUTE FUNCTION invalidate_all_comp_source_publications();

CREATE TRIGGER trg_comp_source_lineage_delete_invalidate
AFTER DELETE ON competitor_source_publication_items
REFERENCING OLD TABLE AS deleted_lineage_rows
FOR EACH STATEMENT
EXECUTE FUNCTION invalidate_comp_source_publications_from_lineage();
