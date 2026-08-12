from __future__ import annotations

from sqlalchemy import create_engine, text

from backend.app import db as db_module


def test_backfill_percentile_preparations_populates_not_null_last_error_and_is_idempotent(monkeypatch):
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
                    status TEXT,
                    last_error TEXT NOT NULL,
                    rows_count INTEGER,
                    completed_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(text("INSERT INTO price_formats (id) VALUES (1)"))

    monkeypatch.setattr(db_module, "engine", engine)

    db_module._backfill_percentile_preparations()
    db_module._backfill_percentile_preparations()

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT price_format_id, status, last_error, rows_count
                FROM price_format_percentile_preparations
                """
            )
        ).mappings().all()

    assert rows == [
        {
            "price_format_id": 1,
            "status": "not_configured",
            "last_error": "",
            "rows_count": 0,
        }
    ]
