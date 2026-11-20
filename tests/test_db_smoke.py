from authz.db import pool


def test_schema_has_the_four_tables() -> None:
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    assert {r["table_name"] for r in rows} == {
        "companies",
        "cards",
        "authorizations",
        "authorization_events",
    }
