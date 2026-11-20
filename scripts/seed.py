"""Reset the demo database to a known state with fixed IDs."""

import os

import psycopg

with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    conn.execute("TRUNCATE authorization_events, authorizations, cards, companies CASCADE")
    conn.execute(
        "INSERT INTO companies (id, name, currency, credit_limit)"
        " VALUES ('co_demo', 'Acme Mexico', 'MXN', 1000000)"  # 10,000.00 MXN credit line
    )
    conn.execute(
        """INSERT INTO cards (id, company_id, holder_name, last4, status,
                              per_txn_limit, monthly_limit, blocked_mccs)
           VALUES ('card_demo', 'co_demo', 'Ana Souza', '4242', 'active',
                   50000, 100000, '{7995}')"""  # 500.00/txn, 1,000.00/month, no gambling
    )
print(
    "seeded: co_demo (10,000.00 MXN credit), card_demo (500.00/txn, 1,000.00/month, MCC 7995 blocked)"
)
