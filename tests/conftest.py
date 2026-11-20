import os

# Tests never touch the demo database.
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

from collections.abc import Iterable
from typing import Any

import psycopg
import pytest

from authz.models import AuthorizationRequest

TABLES = "authorization_events, authorizations, cards, companies"
COMPANY = "co_test"
CARD = "card_test"


def truncate() -> None:
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute(f"TRUNCATE {TABLES} CASCADE")


@pytest.fixture(autouse=True)
def clean_db() -> None:
    truncate()


def sql(query: str, params: Iterable[Any] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        cur = conn.execute(query, tuple(params))
        return cur.fetchall() if cur.description else []


def insert_company(id: str = COMPANY, credit_limit: int = 1_000_000, currency: str = "MXN") -> str:
    sql(
        "INSERT INTO companies (id, name, currency, credit_limit) VALUES (%s, %s, %s, %s)",
        (id, f"Company {id}", currency, credit_limit),
    )
    return id


def insert_card(
    id: str = CARD,
    company_id: str = COMPANY,
    status: str = "active",
    per_txn_limit: int = 50_000,
    monthly_limit: int = 100_000,
    blocked_mccs: tuple[str, ...] = ("7995",),
) -> str:
    sql(
        """INSERT INTO cards (id, company_id, holder_name, last4, status,
                              per_txn_limit, monthly_limit, blocked_mccs)
           VALUES (%s, %s, 'Ana Test', '4242', %s, %s, %s, %s)""",
        (id, company_id, status, per_txn_limit, monthly_limit, list(blocked_mccs)),
    )
    return id


@pytest.fixture
def card() -> str:
    """Default company (10,000.00 MXN credit) + card (500.00/txn, 1,000.00/month, blocks 7995)."""
    insert_company()
    return insert_card()


def auth_request(
    ntid: str = "ntx_1", card_id: str = CARD, amount: int = 12_000, **overrides: Any
) -> AuthorizationRequest:
    fields: dict[str, Any] = {
        "network_transaction_id": ntid,
        "card_id": card_id,
        "amount": amount,
        "currency": "MXN",
        "merchant_amount": 700,
        "merchant_currency": "USD",
        "mcc": "5812",
        "merchant_name": "Taqueria El Paso",
        "merchant_country": "US",
    }
    return AuthorizationRequest(**{**fields, **overrides})


def client_post(client: Any, req: AuthorizationRequest) -> dict[str, Any]:
    res = client.post("/v1/authorizations", json=req.model_dump())
    assert res.status_code == 200, res.text
    body: dict[str, Any] = res.json()
    return body
