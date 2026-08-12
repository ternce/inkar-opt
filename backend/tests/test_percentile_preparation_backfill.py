from __future__ import annotations

from sqlalchemy import create_engine, text

from backend.app import db as db_module


def test_backfill_percentile_preparations_populates_not_null_defaults_and_is_idempotent(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE price_formats (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE competitor_price_percentiles (price_format_id INTEGER)"))
        conn.execute(
            text(
                """
                CREATE TABLE price_format_competitor_assignments (
                    price_format_id INTEGER,
                    competitor_price_list_id INTEGER,
                    is_active INTEGER,
                    percentile_mode TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE competitor_price_lists (
                    id INTEGER PRIMARY KEY,
                    source_type TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE price_format_percentile_preparations (
                    price_format_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at DATETIME,
                    completed_at DATETIME,
                    failed_at DATETIME,
                    last_error TEXT NOT NULL,
                    source_refresh_id TEXT NOT NULL,
                    source_refreshed_at DATETIME,
                    configuration_fingerprint TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    rows_count INTEGER NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(text("INSERT INTO price_formats (id) VALUES (1), (2), (3)"))
        conn.execute(text("INSERT INTO competitor_price_percentiles (price_format_id) VALUES (2), (2)"))
        conn.execute(
            text(
                """
                INSERT INTO price_format_percentile_preparations (
                    price_format_id,
                    status,
                    last_error,
                    source_refresh_id,
                    configuration_fingerprint,
                    job_id,
                    rows_count,
                    updated_at
                )
                VALUES (3, 'ready', 'keep-error', 'refresh-1', 'fingerprint-1', 'job-1', 7, CURRENT_TIMESTAMP)
                """
            )
        )

    monkeypatch.setattr(db_module, "engine", engine)

    db_module._backfill_percentile_preparations()
    db_module._backfill_percentile_preparations()

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    price_format_id,
                    status,
                    last_error,
                    source_refresh_id,
                    configuration_fingerprint,
                    job_id,
                    rows_count
                FROM price_format_percentile_preparations
                ORDER BY price_format_id
                """
            )
        ).mappings().all()

    assert rows == [
        {
            "price_format_id": 1,
            "status": "not_configured",
            "last_error": "",
            "source_refresh_id": "",
            "configuration_fingerprint": "",
            "job_id": "",
            "rows_count": 0,
        },
        {
            "price_format_id": 2,
            "status": "ready",
            "last_error": "",
            "source_refresh_id": "",
            "configuration_fingerprint": "",
            "job_id": "",
            "rows_count": 2,
        },
        {
            "price_format_id": 3,
            "status": "ready",
            "last_error": "keep-error",
            "source_refresh_id": "refresh-1",
            "configuration_fingerprint": "fingerprint-1",
            "job_id": "job-1",
            "rows_count": 7,
        },
    ]
