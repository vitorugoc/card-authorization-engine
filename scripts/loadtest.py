"""Measure latency and throughput of POST /v1/authorizations under two contention profiles.

  hot     every request hits ONE company: worst case for the per-company row lock
  spread  requests spread over 16 companies: locks rarely collide
  history hot again, after HISTORY settled clearings land on that company (about a year of
          spend for a mid-size customer): shows whether the balance cost grows with history

Wipes the demo database (run `make demo` afterwards to restore the demo data).
Client and server share one laptop, so read the numbers as relative, not absolute.
"""

import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import psycopg

from authz.signature import sign

API = os.environ.get("API_URL", "http://localhost:8000")
SECRET = os.environ["WEBHOOK_SECRET"]
WORKERS = int(os.environ.get("WORKERS", "16"))
REQUESTS = int(os.environ.get("REQUESTS", "2000"))
COMPANIES = 16
HISTORY = int(os.environ.get("HISTORY", "500000"))


def seed() -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("TRUNCATE authorization_events, authorizations, cards, companies CASCADE")
        for i in range(COMPANIES):
            conn.execute(
                "INSERT INTO companies (id, name, currency, credit_limit)"
                " VALUES (%s, %s, 'MXN', 1000000000000)",
                (f"lt_co_{i}", f"Load {i}"),
            )
            conn.execute(
                """INSERT INTO cards (id, company_id, holder_name, last4, status,
                                      per_txn_limit, monthly_limit)
                   VALUES (%s, %s, 'Load Test', '0000', 'active', 1000000, 100000000000)""",
                (f"lt_card_{i}", f"lt_co_{i}"),
            )


def add_history() -> None:
    """Settled clearings from 40 days ago: they count for the credit line, not the month."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(
            """INSERT INTO authorizations
                 (id, network_transaction_id, card_id, company_id, amount, currency,
                  merchant_amount, merchant_currency, mcc, merchant_name, merchant_country,
                  status, held_amount, cleared_amount, policy_snapshot, request_hash, response,
                  created_at)
               SELECT 'lt_hist_' || g, 'lt_hist_' || g, 'lt_card_0', 'lt_co_0', 1000, 'MXN',
                      1000, 'MXN', '5812', 'Load', 'MX', 'cleared', 0, 1000, '{}', '', '{}',
                      now() - interval '40 days'
               FROM generate_series(1, %s) g""",
            (HISTORY,),
        )
        conn.execute("ANALYZE authorizations")


def run(name: str, card_for: Any) -> None:
    client = httpx.Client(base_url=API, timeout=5, limits=httpx.Limits(max_connections=WORKERS))

    def one(i: int) -> tuple[float, float, str]:
        body = json.dumps({
            "network_transaction_id": f"lt_{name}_{i}", "card_id": card_for(i),
            "amount": 1000, "currency": "MXN", "merchant_amount": 1000,
            "merchant_currency": "MXN", "mcc": "5812", "merchant_name": "Load",
            "merchant_country": "MX",
        }).encode()  # fmt: skip
        headers = {
            "Content-Type": "application/json",
            "X-Signature": sign(SECRET, body, int(time.time())),
        }
        started = time.perf_counter()
        res = client.post("/v1/authorizations", content=body, headers=headers)
        total_ms = (time.perf_counter() - started) * 1000
        server_ms = float(res.headers["x-process-time"].removesuffix("ms"))
        if res.status_code != 200:
            return total_ms, server_ms, f"http_{res.status_code}"
        return total_ms, server_ms, res.json()["decline_code"] or "approved"

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(one, range(REQUESTS)))
    elapsed = time.perf_counter() - started

    def pct(values: list[float], p: int) -> float:
        return statistics.quantiles(values, n=100, method="inclusive")[p - 1]

    total = [r[0] for r in results]
    server = [r[1] for r in results]
    approved = sum(r[2] == "approved" for r in results)
    print(
        f"{name:<7} {REQUESTS / elapsed:>7.0f} req/s"
        f"   client p50 {pct(total, 50):>6.1f}  p95 {pct(total, 95):>6.1f}"
        f"  p99 {pct(total, 99):>6.1f} ms"
        f"   server p99 {pct(server, 99):>6.1f} ms"
        f"   approved {approved}/{REQUESTS}"
        f"   system_error {sum(r[2] == 'system_error' for r in results)}"
    )


if __name__ == "__main__":
    seed()
    print(f"{REQUESTS} signed authorizations per scenario, {WORKERS} concurrent clients\n")
    run("warmup", lambda i: f"lt_card_{i % COMPANIES}")
    seed()
    run("hot", lambda i: "lt_card_0")
    run("spread", lambda i: f"lt_card_{i % COMPANIES}")
    add_history()
    run("history", lambda i: "lt_card_0")
