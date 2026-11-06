"""Database access: one SQLAlchemy engine, one connection per request.

Queries are plain SQL (sqlalchemy.text) against the schema in db/schema.sql.
The schema itself enforces the business rules (seat limits, permissions on
overrides and large refunds), so writes must run inside a transaction:

    with transaction() as conn:
        conn.execute(text("SELECT ... FOR NO KEY UPDATE"), {...})
        ...
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from flask import Flask, current_app, g
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine


def _normalise_url(url: str) -> str:
    # Replit and most hosts give postgres:// or postgresql://; use psycopg 3.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def init_app(app: Flask) -> None:
    app.extensions["db_engine"] = create_engine(
        _normalise_url(app.config["DATABASE_URL"]),
        pool_pre_ping=True,
        # No server-side prepared statements: they break behind a connection pooler
        # (PgBouncer / Neon "-pooler" addresses), which hosted Postgres may put in front.
        connect_args={"prepare_threshold": None},
        pool_size=5,
        max_overflow=5,
        future=True,
    )
    app.teardown_appcontext(_close_connection)


def engine() -> Engine:
    return current_app.extensions["db_engine"]


def connection() -> Connection:
    """The request's read connection (autocommit off; rolled back at teardown)."""
    if "db_conn" not in g:
        g.db_conn = engine().connect()
    return g.db_conn


def _close_connection(_exc: BaseException | None) -> None:
    conn = g.pop("db_conn", None)
    if conn is not None:
        conn.rollback()
        conn.close()


@contextmanager
def transaction() -> Iterator[Connection]:
    """A separate connection for writes; commits on success, rolls back on error."""
    with engine().begin() as conn:
        yield conn


def fetch_all(sql: str, **params: Any) -> list[dict]:
    return [dict(r) for r in connection().execute(text(sql), params).mappings()]


def fetch_one(sql: str, **params: Any) -> dict | None:
    row = connection().execute(text(sql), params).mappings().first()
    return dict(row) if row else None


def fetch_value(sql: str, **params: Any) -> Any:
    return connection().execute(text(sql), params).scalar()
