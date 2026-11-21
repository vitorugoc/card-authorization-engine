"""Reversal and clearing (SDD §7.2, §7.3)."""

from typing import Any

import pytest
from conftest import COMPANY, auth_request, sql

from authz import service
from authz.domain import Conflict, DomainError, NotFound
from authz.models import ClearingRequest, ReversalRequest


def approve(ntid: str = "ntx_1", amount: int = 12_000) -> None:
    assert service.authorize(auth_request(ntid, amount=amount))["decision"] == "approved"


def reverse(event_id: str, amount: int | None = None, ntid: str = "ntx_1") -> dict[str, Any]:
    return service.reverse(ntid, ReversalRequest(event_id=event_id, amount=amount))


def clear(event_id: str, amount: int, ntid: str = "ntx_1") -> dict[str, Any]:
    return service.clear(ntid, ClearingRequest(event_id=event_id, amount=amount))


def auth_row() -> tuple[str, int, int]:
    [row] = sql("SELECT status, held_amount, cleared_amount FROM authorizations")
    return row  # type: ignore[return-value]


def error_code(call: Any) -> tuple[type[DomainError], str]:
    with pytest.raises(DomainError) as exc:
        call()
    return type(exc.value), exc.value.code


def test_partial_reversal_reduces_hold(card: str) -> None:
    approve(amount=12_000)

    res = reverse("evt_r1", amount=2_000)

    assert (res["status"], res["held_amount"]) == ("pending", 10_000)
    assert res["available_credit_after"] == 1_000_000 - 10_000
    assert auth_row() == ("pending", 10_000, 0)


def test_full_reversal_without_amount_sets_reversed(card: str) -> None:
    approve(amount=12_000)
    reverse("evt_r1", amount=2_000)

    res = reverse("evt_r2")

    assert (res["status"], res["held_amount"]) == ("reversed", 0)
    assert service.get_company(COMPANY)["available"] == 1_000_000


def test_reversal_exceeding_hold_conflict(card: str) -> None:
    approve(amount=12_000)
    assert error_code(lambda: reverse("evt_r1", amount=12_001)) == (Conflict, "amount_exceeds_hold")
    assert auth_row() == ("pending", 12_000, 0)


def test_clearing_under_hold_releases_remainder(card: str) -> None:
    approve(amount=12_000)

    res = clear("evt_c1", amount=11_000)

    assert (res["status"], res["held_amount"], res["cleared_amount"]) == ("cleared", 0, 11_000)
    company = service.get_company(COMPANY)
    assert (company["held"], company["posted"]) == (0, 11_000)


def test_clearing_after_partial_reversal_with_tip(card: str) -> None:
    approve(amount=12_000)
    reverse("evt_r1", amount=2_000)

    res = clear("evt_c1", amount=13_800)  # tip added: more than the remaining 10,000 hold

    assert (res["status"], res["held_amount"], res["cleared_amount"]) == ("cleared", 0, 13_800)


def test_over_clear_posts_full_amount_even_past_the_limit() -> None:
    from conftest import insert_card, insert_company

    insert_company(credit_limit=12_000)
    insert_card()
    approve(amount=12_000)

    clear("evt_c1", amount=15_000)  # network already settled it: the books must say so

    assert service.get_company(COMPANY)["available"] == -3_000


def test_clearing_on_reversed_conflict(card: str) -> None:
    approve()
    reverse("evt_r1")
    assert error_code(lambda: clear("evt_c1", 12_000)) == (Conflict, "authorization_not_pending")


def test_reversal_after_clearing_conflict(card: str) -> None:
    approve()
    clear("evt_c1", 12_000)
    assert error_code(lambda: reverse("evt_r1")) == (Conflict, "authorization_not_pending")


def test_reversal_of_declined_conflict(card: str) -> None:
    service.authorize(auth_request(mcc="7995"))
    assert error_code(lambda: reverse("evt_r1")) == (Conflict, "authorization_not_pending")


def test_unknown_ntid_not_found(card: str) -> None:
    assert error_code(lambda: reverse("evt_r1", ntid="ntx_nope")) == (
        NotFound,
        "authorization_not_found",
    )
    assert error_code(lambda: clear("evt_c1", 1, ntid="ntx_nope")) == (
        NotFound,
        "authorization_not_found",
    )


def test_event_replay_is_idempotent(card: str) -> None:
    approve(amount=12_000)
    first = reverse("evt_r1", amount=2_000)

    assert reverse("evt_r1", amount=2_000) == first
    assert auth_row() == ("pending", 10_000, 0)
    assert sql("SELECT count(*) FROM authorization_events") == [(1,)]


def test_event_id_mismatch_conflict(card: str) -> None:
    approve(amount=12_000)
    reverse("evt_r1", amount=2_000)
    assert error_code(lambda: reverse("evt_r1", amount=3_000)) == (Conflict, "idempotency_mismatch")


def test_every_change_has_an_event_row(card: str) -> None:
    approve(amount=12_000)
    reverse("evt_r1", amount=2_000)
    clear("evt_c1", amount=10_000)

    assert sql("SELECT event_id, type, amount FROM authorization_events ORDER BY created_at") == [
        ("evt_r1", "reversal", 2_000),
        ("evt_c1", "clearing", 10_000),
    ]


def test_full_reversal_retry_with_explicit_amount_is_a_replay(card: str) -> None:
    approve(amount=12_000)
    first = reverse("evt_r1")  # amount omitted = whole remaining hold

    assert reverse("evt_r1", amount=12_000) == first  # same event, amount now explicit
    assert sql("SELECT count(*) FROM authorization_events") == [(1,)]


def test_explicit_full_reversal_retry_without_amount_is_a_replay(card: str) -> None:
    approve(amount=12_000)
    first = reverse("evt_r1", amount=12_000)

    assert reverse("evt_r1") == first


def test_partial_reversal_retry_without_amount_conflict(card: str) -> None:
    approve(amount=12_000)
    reverse("evt_r1", amount=2_000)

    # Omitted amount means "release everything"; the original event released 2,000 only.
    assert error_code(lambda: reverse("evt_r1")) == (Conflict, "idempotency_mismatch")


def test_clearing_adds_to_company_posted_counter_once(card: str) -> None:
    """Posted spend is a running total on the company row (ADR 0004), not a SUM over history."""
    approve("ntx_1", amount=12_000)
    approve("ntx_2", amount=5_000)

    clear("evt_c1", amount=13_800, ntid="ntx_1")
    clear("evt_c1", amount=13_800, ntid="ntx_1")  # replay: must not count twice
    reverse("evt_r2", ntid="ntx_2")  # reversals never touch posted

    assert sql("SELECT posted FROM companies") == [(13_800,)]
    assert service.get_company(COMPANY)["posted"] == 13_800
