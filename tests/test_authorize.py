from conftest import COMPANY, auth_request, insert_card, insert_company, sql

from authz import service


def test_approve_creates_hold_and_reduces_available(card: str) -> None:
    res = service.authorize(auth_request(amount=12_000))

    assert res["decision"] == "approved"
    assert res["decline_code"] is None
    assert res["reasons"] == []
    assert res["available_credit_after"] == 1_000_000 - 12_000
    assert sql("SELECT status, held_amount FROM authorizations") == [("pending", 12_000)]
    company = service.get_company(COMPANY)
    assert (company["held"], company["posted"], company["available"]) == (12_000, 0, 988_000)


def test_insufficient_credit_declines_and_persists() -> None:
    insert_company(credit_limit=10_000)
    insert_card()

    res = service.authorize(auth_request(amount=12_000))

    assert res["decision"] == "declined"
    assert res["decline_code"] == "insufficient_credit"
    assert res["available_credit_after"] == 10_000
    assert sql("SELECT status, held_amount, decline_code FROM authorizations") == [
        ("declined", 0, "insufficient_credit")
    ]


def test_unknown_card_declines_card_not_found() -> None:
    res = service.authorize(auth_request(card_id="card_nope"))

    assert res["decision"] == "declined"
    assert res["decline_code"] == "card_not_found"
    assert res["authorization_id"] is None
    assert sql("SELECT count(*) FROM authorizations") == [(0,)]


def test_casino_600_declines_with_mcc_and_per_txn_reasons(card: str) -> None:
    res = service.authorize(auth_request(amount=60_000, mcc="7995"))

    assert res["decision"] == "declined"
    assert res["decline_code"] == "mcc_blocked"
    assert res["reasons"] == ["mcc_blocked", "exceeds_per_transaction_limit"]
    assert sql("SELECT status, held_amount, reasons FROM authorizations") == [
        ("declined", 0, ["mcc_blocked", "exceeds_per_transaction_limit"])
    ]


def test_decision_stores_policy_snapshot(card: str) -> None:
    service.authorize(auth_request(amount=12_000))

    [(snapshot,)] = sql("SELECT policy_snapshot FROM authorizations")
    assert snapshot["status"] == "active"
    assert snapshot["per_txn_limit"] == 50_000
    assert snapshot["monthly_limit"] == 100_000
    assert snapshot["blocked_mccs"] == ["7995"]
    assert snapshot["card_monthly_spend"] == 0
    assert snapshot["company_available"] == 1_000_000


def test_monthly_limit_counts_holds_and_cleared_current_month_only(card: str) -> None:
    # An auth from last month must not count against this month's limit.
    assert service.authorize(auth_request("ntx_old", amount=50_000))["decision"] == "approved"
    sql(
        "UPDATE authorizations SET created_at = date_trunc('month', now()) - interval '1 day'"
        " WHERE network_transaction_id = 'ntx_old'"
    )
    service.authorize(auth_request("ntx_a", amount=40_000))
    service.authorize(auth_request("ntx_b", amount=30_000))
    sql(
        "UPDATE authorizations SET status = 'cleared', held_amount = 0, cleared_amount = 30000"
        " WHERE network_transaction_id = 'ntx_b'"
    )

    # 40,000 held + 30,000 cleared this month: 30,000 left of 100,000.
    assert service.authorize(auth_request("ntx_ok", amount=30_000))["decision"] == "approved"
    over = service.authorize(auth_request("ntx_over", amount=1))
    assert over["decline_code"] == "exceeds_card_monthly_limit"


def test_frozen_card_declines_card_inactive(card: str) -> None:
    sql("UPDATE cards SET status = 'frozen'")
    assert service.authorize(auth_request())["decline_code"] == "card_inactive"


def test_currency_mismatch_declines(card: str) -> None:
    res = service.authorize(auth_request(currency="USD"))
    assert res["decline_code"] == "currency_mismatch"
