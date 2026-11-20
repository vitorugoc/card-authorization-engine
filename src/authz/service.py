"""Transaction scripts. Every money write locks the company row first (ADR 0002)."""

import logging
import time
import uuid
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from authz.db import pool
from authz.models import AuthorizationRequest
from authz.policy import CardState, CompanyState, available, evaluate

log = logging.getLogger("authz")

Conn = Connection[DictRow]


class DomainError(Exception):
    """Business error mapped to an HTTP status by the API layer."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _begin(conn: Conn) -> None:
    # Fail fast inside the network window instead of queueing forever (ADR 0005).
    conn.execute("SET LOCAL lock_timeout = '200ms'")
    conn.execute("SET LOCAL statement_timeout = '1s'")


def _lock_company(conn: Conn, company_id: str) -> DictRow:
    row = conn.execute(
        "SELECT id, currency, credit_limit FROM companies WHERE id = %s FOR UPDATE",
        (company_id,),
    ).fetchone()
    assert row is not None  # FK guarantees it
    return row


def _company_state(conn: Conn, company: DictRow) -> CompanyState:
    totals = conn.execute(
        """SELECT COALESCE(SUM(held_amount), 0)::bigint    AS held,
                  COALESCE(SUM(cleared_amount), 0)::bigint AS posted
           FROM authorizations
           WHERE company_id = %s AND status IN ('pending', 'cleared')""",
        (company["id"],),
    ).fetchone()
    assert totals is not None
    return CompanyState(
        currency=company["currency"],
        credit_limit=company["credit_limit"],
        held=totals["held"],
        posted=totals["posted"],
    )


def _card_state(conn: Conn, card_id: str) -> CardState:
    row = conn.execute(
        """SELECT c.status, c.per_txn_limit, c.monthly_limit, c.blocked_mccs,
                  (SELECT COALESCE(SUM(a.held_amount + a.cleared_amount), 0)::bigint
                   FROM authorizations a
                   WHERE a.card_id = c.id
                     AND a.created_at >= date_trunc('month', now() AT TIME ZONE 'UTC')
                                         AT TIME ZONE 'UTC') AS monthly_spend
           FROM cards c WHERE c.id = %s""",
        (card_id,),
    ).fetchone()
    assert row is not None
    return CardState(
        status=row["status"],
        per_txn_limit=row["per_txn_limit"],
        monthly_limit=row["monthly_limit"],
        blocked_mccs=frozenset(row["blocked_mccs"]),
        monthly_spend=row["monthly_spend"],
    )


# --- authorization -------------------------------------------------------------------------


def authorize(req: AuthorizationRequest) -> dict[str, Any]:
    started = time.perf_counter()
    with pool().connection() as conn:
        _begin(conn)
        card = conn.execute("SELECT company_id FROM cards WHERE id = %s", (req.card_id,)).fetchone()
        if card is None:
            return _response(None, req, "declined", "card_not_found", ["card_not_found"], None)

        company = _company_state(conn, _lock_company(conn, card["company_id"]))
        # Re-read the card under the lock: a concurrent freeze/limit change can't race us.
        card_state = _card_state(conn, req.card_id)
        decision = evaluate(req.amount, req.currency, req.mcc, card_state, company)
        snapshot = {
            "status": card_state.status,
            "per_txn_limit": card_state.per_txn_limit,
            "monthly_limit": card_state.monthly_limit,
            "blocked_mccs": sorted(card_state.blocked_mccs),
            "card_monthly_spend": card_state.monthly_spend,
            "company_available": available(company),
        }

        auth_id = _new_id("auth")
        status = "pending" if decision.approved else "declined"
        response = _response(
            auth_id,
            req,
            "approved" if decision.approved else "declined",
            decision.decline_code,
            list(decision.reasons),
            decision.available_after,
        )
        conn.execute(
            """INSERT INTO authorizations
                 (id, network_transaction_id, card_id, company_id, amount, currency,
                  merchant_amount, merchant_currency, mcc, merchant_name, merchant_country,
                  status, decline_code, reasons, held_amount, policy_snapshot,
                  request_hash, response)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                auth_id, req.network_transaction_id, req.card_id, card["company_id"],
                req.amount, req.currency, req.merchant_amount, req.merchant_currency,
                req.mcc, req.merchant_name, req.merchant_country,
                status, decision.decline_code, list(decision.reasons),
                req.amount if decision.approved else 0,
                Jsonb(snapshot), "", Jsonb(response),
            ),
        )  # fmt: skip
    log.info("authorization.decided %s %.1fms", response, (time.perf_counter() - started) * 1000)
    return response


def _response(
    auth_id: str | None,
    req: AuthorizationRequest,
    decision: str,
    decline_code: str | None,
    reasons: list[str],
    available_after: int | None,
) -> dict[str, Any]:
    return {
        "authorization_id": auth_id,
        "network_transaction_id": req.network_transaction_id,
        "decision": decision,
        "decline_code": decline_code,
        "reasons": reasons,
        "available_credit_after": available_after,
    }


# --- reads ---------------------------------------------------------------------------------


def get_company(company_id: str) -> dict[str, Any]:
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT id, name, currency, credit_limit FROM companies WHERE id = %s", (company_id,)
        ).fetchone()
        if row is None:
            raise DomainError(404, "company_not_found")
        state = _company_state(conn, row)
    return {
        **row,
        "held": state.held,
        "posted": state.posted,
        "available": state.credit_limit - state.held - state.posted,
    }
