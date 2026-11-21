"""Exactly-once effects under processor retries (ADR 0003)."""

import os
import threading
import time

import psycopg
import pytest
from conftest import auth_request, insert_card, insert_company, sql
from psycopg.types.json import Jsonb

from authz import service
from authz.domain import Conflict, DomainError


def test_replay_returns_identical_response_no_new_hold(card: str) -> None:
    first = service.authorize(auth_request(amount=12_000))
    replay = service.authorize(auth_request(amount=12_000))

    assert replay == first
    assert sql("SELECT count(*), sum(held_amount) FROM authorizations") == [(1, 12_000)]


def test_replay_of_decline_is_identical_even_after_state_changes(card: str) -> None:
    first = service.authorize(auth_request(mcc="7995"))
    sql("UPDATE cards SET blocked_mccs = '{}'")  # policy changed since the original decision

    assert service.authorize(auth_request(mcc="7995")) == first  # never re-evaluated


def test_same_ntid_different_amount_409_and_no_state_change(card: str) -> None:
    service.authorize(auth_request(amount=12_000))

    with pytest.raises(DomainError) as exc:
        service.authorize(auth_request(amount=99_000))

    assert (type(exc.value), exc.value.code) == (Conflict, "idempotency_mismatch")
    assert sql("SELECT amount FROM authorizations") == [(12_000,)]


def test_same_ntid_other_company_card_409(card: str) -> None:
    insert_company("co_other")
    insert_card("card_other", company_id="co_other")
    service.authorize(auth_request(card_id="card_test"))

    with pytest.raises(DomainError) as exc:
        service.authorize(auth_request(card_id="card_other"))
    assert exc.value.code == "idempotency_mismatch"


def _insert_in_open_transaction(request_hash: str, response: dict[str, object]) -> None:
    """Another company's transaction inserts the same ntid and commits ~100ms later.

    Different companies take different locks, so only the UNIQUE constraint can catch it.
    """
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(
            """INSERT INTO authorizations
                 (id, network_transaction_id, card_id, company_id, amount, currency,
                  merchant_amount, merchant_currency, mcc, merchant_name, merchant_country,
                  status, held_amount, policy_snapshot, request_hash, response)
               VALUES ('auth_racer', 'ntx_1', 'card_other', 'co_other', 100, 'MXN',
                       0, 'MXN', '5812', 'x', 'MX', 'pending', 100, '{}', %s, %s)""",
            (request_hash, Jsonb(response)),
        )
        time.sleep(0.1)
        conn.commit()


@pytest.mark.parametrize("same_payload", [True, False])
def test_cross_company_ntid_race_resolved_by_unique_constraint(
    card: str, same_payload: bool
) -> None:
    insert_company("co_other")
    insert_card("card_other", company_id="co_other")
    req = auth_request()
    racer_response = {"authorization_id": "auth_racer", "decision": "approved"}
    racer_hash = service.request_hash(req.decision_fields()) if same_payload else "different"

    racer = threading.Thread(target=_insert_in_open_transaction, args=(racer_hash, racer_response))
    racer.start()
    time.sleep(0.03)  # racer's row is inserted but not committed yet
    try:
        if same_payload:
            assert service.authorize(req) == racer_response
        else:
            with pytest.raises(DomainError) as exc:
                service.authorize(req)
            assert exc.value.code == "idempotency_mismatch"
    finally:
        racer.join()
    assert sql("SELECT id FROM authorizations") == [("auth_racer",)]
