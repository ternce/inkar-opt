CREATE TABLE IF NOT EXISTS vidman_logical_competitors (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    price_format_id INTEGER REFERENCES price_formats(id),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    collision_status VARCHAR(64) NOT NULL DEFAULT 'UNRESOLVED',
    collision_notes TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_vidman_logical_competitor_scope UNIQUE (name, region, price_format_id)
);

CREATE INDEX IF NOT EXISTS ix_vidman_logical_competitors_name
    ON vidman_logical_competitors (name);

CREATE INDEX IF NOT EXISTS ix_vidman_logical_competitors_region
    ON vidman_logical_competitors (region);

CREATE INDEX IF NOT EXISTS ix_vidman_logical_competitors_price_format_id
    ON vidman_logical_competitors (price_format_id);

CREATE INDEX IF NOT EXISTS ix_vidman_logical_competitors_active
    ON vidman_logical_competitors (active);

CREATE INDEX IF NOT EXISTS ix_vidman_logical_competitors_collision_status
    ON vidman_logical_competitors (collision_status);

CREATE TABLE IF NOT EXISTS vidman_logical_competitor_sources (
    id SERIAL PRIMARY KEY,
    logical_competitor_id INTEGER NOT NULL REFERENCES vidman_logical_competitors(id),
    account_id INTEGER NOT NULL REFERENCES vidman_accounts(id),
    main_id BIGINT NOT NULL,
    role VARCHAR(32) NOT NULL DEFAULT 'FALLBACK',
    priority INTEGER NOT NULL DEFAULT 100,
    approved_manually BOOLEAN NOT NULL DEFAULT FALSE,
    approved_at TIMESTAMP,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_vidman_logical_competitor_source_source UNIQUE (account_id, main_id)
);

CREATE INDEX IF NOT EXISTS ix_vidman_logical_competitor_sources_logical_competitor_id
    ON vidman_logical_competitor_sources (logical_competitor_id);

CREATE INDEX IF NOT EXISTS ix_vidman_logical_competitor_sources_account_id
    ON vidman_logical_competitor_sources (account_id);

CREATE INDEX IF NOT EXISTS ix_vidman_logical_competitor_sources_main_id
    ON vidman_logical_competitor_sources (main_id);

CREATE INDEX IF NOT EXISTS ix_vidman_logical_competitor_sources_role
    ON vidman_logical_competitor_sources (role);

CREATE INDEX IF NOT EXISTS ix_vidman_logical_competitor_sources_priority
    ON vidman_logical_competitor_sources (priority);

CREATE INDEX IF NOT EXISTS ix_vidman_logical_competitor_sources_approved_manually
    ON vidman_logical_competitor_sources (approved_manually);

CREATE INDEX IF NOT EXISTS ix_vidman_logical_competitor_sources_active
    ON vidman_logical_competitor_sources (active);

CREATE INDEX IF NOT EXISTS ix_vidman_logical_source_competitor_role
    ON vidman_logical_competitor_sources (logical_competitor_id, role, active);

CREATE UNIQUE INDEX IF NOT EXISTS uq_vidman_logical_one_active_primary
    ON vidman_logical_competitor_sources (logical_competitor_id)
    WHERE active IS TRUE AND role = 'PRIMARY';
