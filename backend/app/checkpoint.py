"""LangGraph checkpointer factory, chosen from ``DATABASE_URL``.

* ``sqlite:///:memory:`` (tests) -> ``InMemorySaver``
* ``sqlite:///path/app.db`` -> ``AsyncSqliteSaver`` on a separate file derived from the app DB's name
  (``path/app.checkpoints.db``), so checkpoint writes never contend with the app store's connection and two app
  databases in one directory never share checkpoints
* ``postgresql://...`` -> ``AsyncPostgresSaver`` on a small psycopg pool, with ``setup()`` run once

``open_checkpointer`` is an async context manager: the saver's connections close when the context exits.
Checkpoints only exist for in-flight or paused (clarification) runs; the runner deletes a thread's checkpoints
when the run reaches a terminal status.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from app.store.factory import is_postgres_url
from app.store.postgres import normalize_pg_url
from app.store.sqlite import sqlite_path_from_url

CHECKPOINT_SUFFIX = ".checkpoints.db"


def checkpoint_path_for(database_url: str) -> str | None:
    """File path of the SQLite checkpoint DB, or None for in-memory/Postgres."""
    if not database_url.startswith("sqlite:"):
        return None
    path = sqlite_path_from_url(database_url)
    if path in (":memory:", "") or path.startswith("file::memory:"):
        return None
    db = Path(path).expanduser()
    stem = db.stem if db.suffix else db.name  # "app.db" -> "app", "app" -> "app"
    return str(db.with_name(stem + CHECKPOINT_SUFFIX))


@asynccontextmanager
async def open_checkpointer(database_url: str) -> AsyncIterator[Any]:
    if is_postgres_url(database_url):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            normalize_pg_url(database_url), min_size=1, max_size=5, open=False, name="checkpointer",
            kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
        )
        await pool.open(wait=True, timeout=15)
        try:
            pg_saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
            await pg_saver.setup()
            yield pg_saver
        finally:
            await pool.close()
        return

    path = checkpoint_path_for(database_url)
    if path is None:
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        await saver.setup()
        yield saver
