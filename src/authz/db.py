"""Connection pool. All SQL lives in service.py next to the transaction it belongs to."""

import os

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool[Connection[DictRow]] | None = None


def pool() -> ConnectionPool[Connection[DictRow]]:
    global _pool
    if _pool is None:
        # ponytail: fixed size; past 25 in-flight requests callers queue here for at most
        # `timeout` seconds, then PoolTimeout (an OperationalError) fails closed as system_error.
        _pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=25,
            timeout=1.0,  # default is 30s: far past any card network's response window
            kwargs={"row_factory": dict_row},
            connection_class=Connection[DictRow],
            open=True,
        )
    return _pool
