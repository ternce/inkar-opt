ALTER TABLE app_users
    ADD COLUMN IF NOT EXISTS password_hash TEXT NOT NULL DEFAULT '';

UPDATE app_users
    SET password_hash = ''
    WHERE password_hash IS NULL;

ALTER TABLE app_users
    ALTER COLUMN password_hash SET DEFAULT '',
    ALTER COLUMN password_hash SET NOT NULL;

ALTER TABLE app_users
    ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS app_sessions (
    id SERIAL PRIMARY KEY,
    session_token_hash VARCHAR(64) NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES app_users(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP
);

ALTER TABLE app_sessions
    ALTER COLUMN session_token_hash SET NOT NULL,
    ALTER COLUMN user_id SET NOT NULL,
    ALTER COLUMN expires_at SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = 'app_sessions'
          AND n.nspname = current_schema()
          AND c.contype = 'u'
          AND (
              SELECT array_agg(a.attname ORDER BY k.ordinality)
              FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ordinality)
              JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
          ) = ARRAY['session_token_hash']
    ) THEN
        ALTER TABLE app_sessions
            ADD CONSTRAINT uq_app_sessions_session_token_hash UNIQUE (session_token_hash);
    END IF;
END $$;

UPDATE app_sessions
    SET created_at = CURRENT_TIMESTAMP
    WHERE created_at IS NULL;

UPDATE app_sessions
    SET last_seen_at = CURRENT_TIMESTAMP
    WHERE last_seen_at IS NULL;

ALTER TABLE app_sessions
    ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP,
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN last_seen_at SET DEFAULT CURRENT_TIMESTAMP,
    ALTER COLUMN last_seen_at SET NOT NULL;

CREATE INDEX IF NOT EXISTS ix_app_sessions_session_token_hash
    ON app_sessions (session_token_hash);

CREATE INDEX IF NOT EXISTS ix_app_sessions_user_id
    ON app_sessions (user_id);

CREATE INDEX IF NOT EXISTS ix_app_sessions_expires_at
    ON app_sessions (expires_at);

CREATE INDEX IF NOT EXISTS ix_app_sessions_revoked_at
    ON app_sessions (revoked_at);

CREATE INDEX IF NOT EXISTS ix_app_sessions_active_lookup
    ON app_sessions (session_token_hash, expires_at, revoked_at);
