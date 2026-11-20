import os

# Tests never touch the demo database.
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

import psycopg
import pytest

TABLES = "authorization_events, authorizations, cards, companies"


@pytest.fixture(autouse=True)
def clean_db() -> None:
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute(f"TRUNCATE {TABLES} CASCADE")
