"""Pure unit tests: no database. Boundaries first — exactly-at-limit must approve."""

from dataclasses import replace

import pytest

from authz.domain import CardStatus
from authz.policy import CardState, CompanyState, available, evaluate

CARD = CardState(
    status=CardStatus.ACTIVE,
    per_txn_limit=50_000,
    monthly_limit=100_000,
    blocked_mccs=frozenset({"7995"}),
    monthly_spend=0,
)
COMPANY = CompanyState(currency="MXN", credit_limit=1_000_000, held=0, posted=0)


def decide(
    amount: int = 10_000,
    currency: str = "MXN",
    mcc: str = "5812",
    card: CardState = CARD,
    company: CompanyState = COMPANY,
) -> tuple[bool, str | None, tuple[str, ...]]:
    d = evaluate(amount, currency, mcc, card, company)
    return d.approved, d.decline_code, d.reasons


def test_available_is_limit_minus_posted_minus_held() -> None:
    assert available(replace(COMPANY, held=300, posted=200)) == 1_000_000 - 500


def test_approves_clean_request() -> None:
    assert decide() == (True, None, ())


def test_frozen_card_declines() -> None:
    assert decide(card=replace(CARD, status=CardStatus.FROZEN))[1] == "card_inactive"


def test_currency_mismatch_declines() -> None:
    assert decide(currency="USD")[1] == "currency_mismatch"


def test_blocked_mcc_declines() -> None:
    assert decide(mcc="7995")[1] == "mcc_blocked"


@pytest.mark.parametrize(("amount", "approved"), [(50_000, True), (50_001, False)])
def test_per_transaction_limit_boundary(amount: int, approved: bool) -> None:
    assert decide(amount=amount)[0] is approved


@pytest.mark.parametrize(("amount", "approved"), [(40_000, True), (40_001, False)])
def test_monthly_limit_boundary_counts_existing_spend(amount: int, approved: bool) -> None:
    ok, code, _ = decide(amount=amount, card=replace(CARD, monthly_spend=60_000))
    assert ok is approved
    assert code == (None if approved else "exceeds_card_monthly_limit")


@pytest.mark.parametrize(("amount", "approved"), [(10_000, True), (10_001, False)])
def test_company_credit_boundary(amount: int, approved: bool) -> None:
    company = replace(COMPANY, credit_limit=30_000, held=15_000, posted=5_000)
    ok, code, _ = decide(amount=amount, company=company)
    assert ok is approved
    assert code == (None if approved else "insufficient_credit")


def test_all_failing_reasons_returned_in_precedence_order() -> None:
    card = replace(CARD, status=CardStatus.FROZEN, monthly_spend=100_000)
    company = replace(COMPANY, credit_limit=0)
    _, code, reasons = decide(amount=60_000, currency="USD", mcc="7995", card=card, company=company)
    assert code == "card_inactive"
    assert reasons == (
        "card_inactive",
        "currency_mismatch",
        "mcc_blocked",
        "exceeds_per_transaction_limit",
        "exceeds_card_monthly_limit",
        "insufficient_credit",
    )


def test_available_after_only_drops_when_approved() -> None:
    assert evaluate(10_000, "MXN", "5812", CARD, COMPANY).available_after == 990_000
    assert evaluate(10_000, "MXN", "7995", CARD, COMPANY).available_after == 1_000_000
