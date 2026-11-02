"""Build a Store from ``DATABASE_URL``.

* ``sqlite:///:memory:`` -> in-memory SQLite (tests)
* ``sqlite:///./data/app.db`` (relative to the working directory) or ``sqlite:////abs/path.db`` -> SQLite file
* ``postgresql://...`` / ``postgres://...`` / ``postgresql+psycopg://...`` -> Postgres (psycopg 3 pool)

The store is returned un-initialised; call ``await store.init()`` (the app lifespan does).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.store.postgres import PostgresStore
from app.store.sqlite import SQLiteStore, sqlite_path_from_url


def is_postgres_url(url: str) -> bool:
    return url.startswith(("postgresql://", "postgres://", "postgresql+psycopg://", "postgresql+psycopg2://"))


def create_store(database_url: str, clock: Callable[[], datetime] | None = None) -> Any:
    """Return a SQLiteStore or PostgresStore (both satisfy app.store.base.Store)."""
    if database_url.startswith("sqlite:"):
        return SQLiteStore(sqlite_path_from_url(database_url), clock=clock)
    if is_postgres_url(database_url):
        return PostgresStore(database_url, clock=clock)
    raise ValueError("DATABASE_URL must start with sqlite:/// or postgresql://")
