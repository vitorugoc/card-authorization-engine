"""No overspend under concurrency (ADR 0002). The money test.

Remove the `FOR UPDATE` in service._lock_company and these tests fail.
"""

import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
from conftest import COMPANY, auth_request, insert_card, insert_company, sql

from authz import service
from authz.models import ClearingRequest, ReversalRequest
from authz.service import DomainError


def burst(n: int, call: Callable[[int], dict[str, Any]]) -> list[dict[str, Any]]:
    """Run n calls released at the same instant by a barrier."""
    barrier = threading.Barrier(n)

    def run(i: int) -> dict[str, Any]:
        barrier.wait()
        return call(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


def assert_invariants() -> None:
    company = service.get_company(COMPANY)
    assert company["held"] + company["posted"] <= company["credit_limit"]
    [(card_spend_ok,)] = sql(
        """SELECT bool_and(spend <= monthly_limit) FROM (
             SELECT c.monthly_limit, COALESCE(SUM(a.held_amount + a.cleared_amount), 0) AS spend
             FROM cards c LEFT JOIN authorizations a ON a.card_id = c.id
             GROUP BY c.id) t"""
    )
    assert card_spend_ok


def test_burst_20x100_with_880_remaining_approves_exactly_8(card: str) -> None:
    service.authorize(auth_request("ntx_first", amount=12_000))  # 880.00 left of 1,000.00

    results = burst(20, lambda i: service.authorize(auth_request(f"ntx_{i}", amount=10_000)))

    assert sum(r["decision"] == "approved" for r in results) == 8
    assert {r["decline_code"] for r in results} == {None, "exceeds_card_monthly_limit"}
    assert_invariants()


def test_company_credit_shared_across_cards_never_overspent() -> None:
    insert_company(credit_limit=50_000)
    cards = [insert_card(f"card_{i}") for i in range(4)]

    results = burst(
        20,
        lambda i: service.authorize(auth_request(f"ntx_{i}", card_id=cards[i % 4], amount=10_000)),
    )

    assert sum(r["decision"] == "approved" for r in results) == 5
    assert service.get_company(COMPANY)["available"] == 0
    assert_invariants()


def test_burst_duplicate_ntid_creates_single_hold(card: str) -> None:
    results = burst(10, lambda _: service.authorize(auth_request("ntx_dup", amount=10_000)))

    assert all(r == results[0] for r in results)
    assert sql("SELECT count(*), sum(held_amount) FROM authorizations") == [(1, 10_000)]


def test_lock_timeout_fails_closed_with_system_error(card: str) -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as blocker:
        blocker.execute("SELECT 1 FROM companies WHERE id = %s FOR UPDATE", (COMPANY,))

        started = time.perf_counter()
        res = service.authorize(auth_request())
        elapsed = time.perf_counter() - started

    assert res["decision"] == "declined"
    assert res["decline_code"] == "system_error"
    assert res["authorization_id"] is None
    assert elapsed < 1.0
    assert sql("SELECT count(*) FROM authorizations") == [(0,)]


def outcome(call: Callable[[], object]) -> str:
    try:
        call()
    except DomainError as exc:
        return exc.code
    return "ok"


def test_concurrent_clearings_with_distinct_event_ids_settle_once(card: str) -> None:
    service.authorize(auth_request("ntx_1", amount=12_000))

    results = burst(
        10,
        lambda i: {
            "r": outcome(
                lambda: service.clear("ntx_1", ClearingRequest(event_id=f"evt_{i}", amount=12_000))
            )
        },
    )

    assert sorted(r["r"] for r in results) == ["authorization_not_pending"] * 9 + ["ok"]
    assert sql("SELECT posted FROM companies") == [(12_000,)]
    assert sql("SELECT count(*) FROM authorization_events") == [(1,)]


def test_concurrent_reversal_and_clearing_exactly_one_wins(card: str) -> None:
    service.authorize(auth_request("ntx_1", amount=12_000))
    calls: list[Callable[[], object]] = [
        lambda: service.reverse("ntx_1", ReversalRequest(event_id="evt_r")),
        lambda: service.clear("ntx_1", ClearingRequest(event_id="evt_c", amount=11_000)),
    ]

    results = burst(2, lambda i: {"r": outcome(calls[i])})

    assert sorted(r["r"] for r in results) == ["authorization_not_pending", "ok"]
    [(status, held, cleared)] = sql(
        "SELECT status, held_amount, cleared_amount FROM authorizations"
    )
    [(posted,)] = sql("SELECT posted FROM companies")
    assert (status, held, cleared, posted) in {
        ("reversed", 0, 0, 0),
        ("cleared", 0, 11_000, 11_000),
    }


def test_freeze_committed_while_auth_waits_for_the_lock_is_honoured(card: str) -> None:
    """TOCTOU: the auth reads the card before the freeze commits, then waits on the lock.
    It must re-read the card under the lock and decline, never approve on a stale read."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as ops:
        ops.execute("SELECT 1 FROM companies WHERE id = %s FOR UPDATE", (COMPANY,))
        ops.execute("UPDATE cards SET status = 'frozen' WHERE id = %s", (card,))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(service.authorize, auth_request())
            time.sleep(0.05)  # the auth is now queued on the company lock (timeout 200ms)
            ops.commit()
            res = pending.result()

    assert res["decline_code"] == "card_inactive"
