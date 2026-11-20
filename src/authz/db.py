"""Connection pool. All SQL lives in service.py next to the transaction it belongs to."""

import os

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool[Connection[DictRow]] | None = None


def pool() -> ConnectionPool[Connection[DictRow]]:
    global _pool
    if _pool is None:
        # max_size covers the uvicorn threadpool burst in the demo; extra waiters queue in the pool
        _pool = ConnectionPool(
            os.environ["DATABASE_URL"],
            min_size=1,
            max_size=25,
            kwargs={"row_factory": dict_row},
            connection_class=Connection[DictRow],
            open=True,
        )
    return _pool
