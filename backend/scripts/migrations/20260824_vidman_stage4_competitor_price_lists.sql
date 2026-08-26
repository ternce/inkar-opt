CREATE TABLE IF NOT EXISTS vidman_competitor_price_list_sources (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES vidman_accounts(id),
    main_id BIGINT NOT NULL,
    price_format_code TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    region TEXT NOT NULL DEFAULT '',
    branch_id TEXT NOT NULL DEFAULT '',
    branch_code TEXT NOT NULL DEFAULT '',
    branch_name TEXT NOT NULL DEFAULT '',
    competitor_name TEXT NOT NULL DEFAULT '',
    price_coefficient NUMERIC(18, 6) NOT NULL DEFAULT 1,
    last_successful_import_run_id INTEGER REFERENCES vidman_import_runs(id),
    competitor_price_list_id INTEGER REFERENCES competitor_price_lists(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_vidman_competitor_plk_source UNIQUE (account_id, main_id, price_format_code)
);

CREATE INDEX IF NOT EXISTS ix_vidman_competitor_plk_source_active
    ON vidman_competitor_price_list_sources (is_active, price_format_code);

CREATE INDEX IF NOT EXISTS ix_vidman_competitor_price_list_sources_account_id
    ON vidman_competitor_price_list_sources (account_id);

CREATE INDEX IF NOT EXISTS ix_vidman_competitor_price_list_sources_main_id
    ON vidman_competitor_price_list_sources (main_id);

CREATE INDEX IF NOT EXISTS ix_vidman_competitor_price_list_sources_last_successful_import_run_id
    ON vidman_competitor_price_list_sources (last_successful_import_run_id);

CREATE INDEX IF NOT EXISTS ix_vidman_competitor_price_list_sources_competitor_price_list_id
    ON vidman_competitor_price_list_sources (competitor_price_list_id);
