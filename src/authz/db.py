"""Connection pool, opened and closed by the app lifespan. All SQL lives next to its transaction."""

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

Conn = Connection[DictRow]

_pool: ConnectionPool[Conn] | None = None


def open_pool(database_url: str) -> None:
    global _pool
    close_pool()
    # ponytail: fixed size; past 25 in-flight requests callers queue here for at most
    # `timeout` seconds, then PoolTimeout (an OperationalError) fails closed as system_error.
    _pool = ConnectionPool(
        database_url,
        min_size=1,
        max_size=25,
        timeout=1.0,  # default is 30s: far past any card network's response window
        kwargs={"row_factory": dict_row},
        connection_class=Conn,
        open=True,
    )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def pool() -> ConnectionPool[Conn]:
    if _pool is None:
        raise RuntimeError("connection pool is not open: the app lifespan opens it")
    return _pool
