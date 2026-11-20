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
