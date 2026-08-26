CREATE TABLE IF NOT EXISTS vidman_accounts (
    id SERIAL PRIMARY KEY,
    login TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vidman_price_lists (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES vidman_accounts(id),
    main_id BIGINT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    detected_pages INTEGER NOT NULL DEFAULT 0,
    last_collected_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_vidman_price_lists_account_main UNIQUE (account_id, main_id)
);

CREATE INDEX IF NOT EXISTS ix_vidman_price_lists_account_main
    ON vidman_price_lists (account_id, main_id);

CREATE TABLE IF NOT EXISTS vidman_import_runs (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES vidman_accounts(id),
    status VARCHAR(32) NOT NULL DEFAULT 'queued',
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    total_plks INTEGER NOT NULL DEFAULT 0,
    completed_plks INTEGER NOT NULL DEFAULT 0,
    failed_plks INTEGER NOT NULL DEFAULT 0,
    total_pages INTEGER NOT NULL DEFAULT 0,
    total_rows INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_vidman_import_runs_status
    ON vidman_import_runs (status);

CREATE INDEX IF NOT EXISTS ix_vidman_import_runs_account_id
    ON vidman_import_runs (account_id);

CREATE TABLE IF NOT EXISTS vidman_import_pages (
    id SERIAL PRIMARY KEY,
    import_run_id INTEGER NOT NULL REFERENCES vidman_import_runs(id),
    price_list_id INTEGER NOT NULL REFERENCES vidman_price_lists(id),
    main_id BIGINT NOT NULL,
    page_number INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'queued',
    rows_count INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_vidman_import_pages_run_list_page UNIQUE (import_run_id, price_list_id, page_number)
);

CREATE INDEX IF NOT EXISTS ix_vidman_import_pages_resume
    ON vidman_import_pages (import_run_id, status, main_id, page_number);

CREATE INDEX IF NOT EXISTS ix_vidman_import_pages_price_list_id
    ON vidman_import_pages (price_list_id);

CREATE TABLE IF NOT EXISTS vidman_raw_items (
    id SERIAL PRIMARY KEY,
    import_run_id INTEGER NOT NULL REFERENCES vidman_import_runs(id),
    account_id INTEGER NOT NULL REFERENCES vidman_accounts(id),
    price_list_id INTEGER NOT NULL REFERENCES vidman_price_lists(id),
    main_id BIGINT NOT NULL,
    page_number INTEGER NOT NULL,
    row_number INTEGER NOT NULL,
    raw_name TEXT NOT NULL DEFAULT '',
    raw_manufacturer TEXT NOT NULL DEFAULT '',
    raw_expiry_text TEXT NOT NULL DEFAULT '',
    expiry_date DATE,
    raw_price_text TEXT NOT NULL DEFAULT '',
    price NUMERIC(18, 4),
    raw_pack_qty TEXT NOT NULL DEFAULT '',
    pack_qty NUMERIC(18, 4),
    raw_min_order TEXT NOT NULL DEFAULT '',
    min_order NUMERIC(18, 4),
    raw_stock TEXT NOT NULL DEFAULT '',
    stock NUMERIC(18, 4),
    raw_html TEXT NOT NULL DEFAULT '',
    row_hash VARCHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_vidman_raw_items_run_list_page
    ON vidman_raw_items (import_run_id, price_list_id, page_number);

CREATE INDEX IF NOT EXISTS ix_vidman_raw_items_account_id
    ON vidman_raw_items (account_id);

CREATE INDEX IF NOT EXISTS ix_vidman_raw_items_main_id
    ON vidman_raw_items (main_id);

CREATE INDEX IF NOT EXISTS ix_vidman_raw_items_row_hash
    ON vidman_raw_items (row_hash);
