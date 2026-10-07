from __future__ import annotations

from backend.app.db import _configure_connection


class _Cursor:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.closed = False

    def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self) -> None:
        self.connection_cursor = _Cursor()
        self.commits = 0

    def cursor(self) -> _Cursor:
        return self.connection_cursor

    def commit(self) -> None:
        self.commits += 1


def test_web_postgresql_connections_get_safety_timeouts():
    connection = _Connection()

    _configure_connection(
        connection,
        url="postgresql+psycopg://db/test",
        process_role="web",
    )

    assert connection.connection_cursor.statements == [
        "SET statement_timeout = '120s'",
        "SET lock_timeout = '10s'",
        "SET idle_in_transaction_session_timeout = '180s'",
    ]
    assert connection.commits == 1
    assert connection.connection_cursor.closed is True


def test_worker_postgresql_connections_do_not_get_web_timeouts():
    connection = _Connection()

    _configure_connection(
        connection,
        url="postgresql+psycopg://db/test",
        process_role="worker",
    )

    assert connection.connection_cursor.statements == []
    assert connection.commits == 0
    assert connection.connection_cursor.closed is True
