"""Transaction scripts. Every money write locks the company row first (ADR 0002)."""

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from psycopg import Connection, OperationalError, errors, sql
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from authz.db import pool
from authz.domain import (
    AuthStatus,
    CardStatus,
    Conflict,
    DeclineCode,
    ErrorCode,
    EventType,
    NotFound,
    Outcome,
)
from authz.models import AuthorizationRequest, CardPatch, ClearingRequest, ReversalRequest
from authz.policy import CardState, CompanyState, available, evaluate

log = logging.getLogger("authz")

Conn = Connection[DictRow]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _begin(conn: Conn) -> None:
    # Fail fast inside the network window instead of queueing forever (ADR 0005).
    conn.execute("SET LOCAL lock_timeout = '200ms'")
    conn.execute("SET LOCAL statement_timeout = '1s'")


def _one(row: DictRow | None, what: str) -> DictRow:
    """A row the schema guarantees (FK, aggregate). Missing = integrity bug, never a 4xx."""
    if row is None:
        raise RuntimeError(f"expected {what}")
    return row


def _lock_company(conn: Conn, company_id: str) -> None:
    _one(
        conn.execute("SELECT 1 FROM companies WHERE id = %s FOR UPDATE", (company_id,)).fetchone(),
        "company row",
    )


def _company_state(conn: Conn, company_id: str) -> CompanyState:
    """posted is a running counter (ADR 0004); only open holds are summed, and they are few."""
    row = conn.execute(
        """SELECT c.currency, c.credit_limit, c.posted,
                  (SELECT COALESCE(SUM(a.held_amount), 0)::bigint
                   FROM authorizations a
                   WHERE a.company_id = c.id AND a.status = 'pending') AS held
           FROM companies c WHERE c.id = %s""",
        (company_id,),
    ).fetchone()
    row = _one(row, "company row")
    return CompanyState(
        currency=row["currency"],
        credit_limit=row["credit_limit"],
        held=row["held"],
        posted=row["posted"],
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
    row = _one(row, "card row")
    return CardState(
        status=CardStatus(row["status"]),
        per_txn_limit=row["per_txn_limit"],
        monthly_limit=row["monthly_limit"],
        blocked_mccs=frozenset(row["blocked_mccs"]),
        monthly_spend=row["monthly_spend"],
    )


# --- idempotency (ADR 0003) ----------------------------------------------------------------

_STORED = {
    "authorization": (
        "SELECT request_hash, response FROM authorizations WHERE network_transaction_id = %s"
    ),
    "event": "SELECT request_hash, response FROM authorization_events WHERE event_id = %s",
}


def request_hash(fields: dict[str, Any]) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


Matcher = Callable[[DictRow], bool]


def _same_hash(req_hash: str) -> Matcher:
    return lambda row: bool(row["request_hash"] == req_hash)


def _same_event(ntid: str, type_: EventType, amount: int | None) -> Matcher:
    """Compare the RESOLVED request: an omitted reversal amount means "the whole remaining
    hold", so it matches an explicit amount that released everything, and vice versa."""

    def matches(row: DictRow) -> bool:
        stored = row["response"]
        if (stored["network_transaction_id"], stored["type"]) != (ntid, type_):
            return False
        if amount is None:
            return bool(
                stored["type"] == EventType.REVERSAL and stored["status"] == AuthStatus.REVERSED
            )
        return bool(amount == stored["amount"])

    return matches


def _stored_response(conn: Conn, kind: str, key: str, matches: Matcher) -> dict[str, Any] | None:
    """Stored response for a replay; 409 if the same id arrives with a different payload."""
    row = conn.execute(_STORED[kind], (key,)).fetchone()
    if row is None:
        return None
    if not matches(row):
        log.warning("idempotency.mismatch", extra={"fields": {"kind": kind, "key": key}})
        raise Conflict(ErrorCode.IDEMPOTENCY_MISMATCH)
    response: dict[str, Any] = row["response"]
    return response


_UNIQUE_KEYS = {
    "authorization": "authorizations_network_transaction_id_key",
    "event": "authorization_events_event_id_key",
}


def _replay_after_race(
    exc: errors.UniqueViolation, kind: str, key: str, matches: Matcher
) -> dict[str, Any]:
    """The same id was committed meanwhile under ANOTHER company's lock; UNIQUE caught it."""
    if exc.diag.constraint_name != _UNIQUE_KEYS[kind]:
        raise exc
    with pool().connection() as conn:
        stored = _stored_response(conn, kind, key, matches)
    if stored is None:
        raise RuntimeError(f"UNIQUE violation on {kind} {key} but no committed row")
    return stored


# --- authorization -------------------------------------------------------------------------


def authorize(req: AuthorizationRequest) -> dict[str, Any]:
    started = time.perf_counter()
    req_hash = request_hash(req.decision_fields())
    try:
        response, replay = _authorize(req, req_hash)
    except errors.UniqueViolation as exc:
        response = _replay_after_race(
            exc, "authorization", req.network_transaction_id, _same_hash(req_hash)
        )
        replay = True
    except OperationalError:
        # Lock/statement timeout, pool exhaustion, DB down: fail closed, inside the network
        # window. How generous to be on failure is stand-in config at the processor (ADR 0005).
        log.exception(
            "authorization.system_error",
            extra={"fields": {"network_transaction_id": req.network_transaction_id}},
        )
        code = DeclineCode.SYSTEM_ERROR
        return _response(None, req, Outcome.DECLINED, code, [code], None)
    # Never log holder_name: the only PII we hold.
    fields = {
        **response,
        **req.decision_fields(),
        "replay": replay,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    log.info("authorization.decided", extra={"fields": fields})
    return response


def _authorize(req: AuthorizationRequest, req_hash: str) -> tuple[dict[str, Any], bool]:
    """Returns (response, replay)."""
    with pool().connection() as conn:
        _begin(conn)
        card = conn.execute("SELECT company_id FROM cards WHERE id = %s", (req.card_id,)).fetchone()
        if card is None:
            code = DeclineCode.CARD_NOT_FOUND
            response = _response(None, req, Outcome.DECLINED, code, [code], None)
            return response, False

        _lock_company(conn, card["company_id"])
        # Lookup AFTER the lock: concurrent duplicates serialize and the second sees the first.
        stored = _stored_response(
            conn, "authorization", req.network_transaction_id, _same_hash(req_hash)
        )
        if stored is not None:
            return stored, True
        company = _company_state(conn, card["company_id"])
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
        status = AuthStatus.PENDING if decision.approved else AuthStatus.DECLINED
        response = _response(
            auth_id,
            req,
            Outcome.APPROVED if decision.approved else Outcome.DECLINED,
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
                Jsonb(snapshot), req_hash, Jsonb(response),
            ),
        )  # fmt: skip
    return response, False


def _response(
    auth_id: str | None,
    req: AuthorizationRequest,
    decision: Outcome,
    decline_code: DeclineCode | None,
    reasons: list[DeclineCode],
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


# --- reversal & clearing (SDD §7.2, §7.3) --------------------------------------------------


def reverse(ntid: str, req: ReversalRequest) -> dict[str, Any]:
    """Release all (amount=None) or part of the remaining hold."""
    return _apply_event(ntid, EventType.REVERSAL, req.event_id, req.amount)


def clear(ntid: str, req: ClearingRequest) -> dict[str, Any]:
    """Final settlement: post the amount, release the whole hold. Over-clear is posted."""
    return _apply_event(ntid, EventType.CLEARING, req.event_id, req.amount)


def _apply_event(ntid: str, type_: EventType, event_id: str, amount: int | None) -> dict[str, Any]:
    try:
        response = _apply_event_tx(ntid, type_, event_id, amount)
    except errors.UniqueViolation as exc:
        return _replay_after_race(exc, "event", event_id, _same_event(ntid, type_, amount))
    log.info(f"{type_}.applied", extra={"fields": response})
    return response


def _apply_event_tx(
    ntid: str, type_: EventType, event_id: str, amount: int | None
) -> dict[str, Any]:
    with pool().connection() as conn:
        _begin(conn)
        found = conn.execute(
            "SELECT company_id FROM authorizations WHERE network_transaction_id = %s", (ntid,)
        ).fetchone()
        if found is None:
            raise NotFound(ErrorCode.AUTHORIZATION_NOT_FOUND)
        company_id = found["company_id"]
        _lock_company(conn, company_id)
        stored = _stored_response(conn, "event", event_id, _same_event(ntid, type_, amount))
        if stored is not None:
            return stored

        auth = conn.execute(
            "SELECT id, amount, status, held_amount FROM authorizations"
            " WHERE network_transaction_id = %s",
            (ntid,),
        ).fetchone()
        auth = _one(auth, "authorization row")
        if auth["status"] != AuthStatus.PENDING:
            raise Conflict(ErrorCode.AUTHORIZATION_NOT_PENDING)

        held: int = auth["held_amount"]
        if type_ == EventType.REVERSAL:
            applied = held if amount is None else amount
            if applied > held:
                raise Conflict(ErrorCode.AMOUNT_EXCEEDS_HOLD)
            new_held, cleared = held - applied, 0
            status = AuthStatus.PENDING if new_held else AuthStatus.REVERSED
        else:
            if amount is None:
                raise ValueError("a clearing always carries an amount")
            applied, new_held, cleared, status = amount, 0, amount, AuthStatus.CLEARED
            if amount > held:
                over = {"network_transaction_id": ntid, "over_clear_delta": amount - held}
                log.info("clearing.over_clear", extra={"fields": over})

        conn.execute(
            "UPDATE authorizations SET status = %s, held_amount = %s, cleared_amount = %s"
            " WHERE id = %s",
            (status, new_held, cleared, auth["id"]),
        )
        if cleared:
            # Same transaction, same lock as the row above: the counter cannot drift from it.
            conn.execute(
                "UPDATE companies SET posted = posted + %s WHERE id = %s", (cleared, company_id)
            )
        response = {
            "authorization_id": auth["id"],
            "network_transaction_id": ntid,
            "event_id": event_id,
            "type": type_,
            "amount": applied,
            "status": status,
            "held_amount": new_held,
            "cleared_amount": cleared,
            "available_credit_after": available(_company_state(conn, company_id)),
        }
        conn.execute(
            """INSERT INTO authorization_events
                 (id, event_id, authorization_id, type, amount, request_hash, response)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                _new_id("evt"),
                event_id,
                auth["id"],
                type_,
                applied,
                request_hash({"ntid": ntid, "type": type_, "amount": applied}),  # resolved
                Jsonb(response),
            ),
        )
    return response


# --- card policy changes ----------------------------------------------------------------


def update_card(card_id: str, patch: CardPatch) -> dict[str, Any]:
    """Takes the company lock like every money/policy write: no auth can see half a change."""
    changes = patch.model_dump(exclude_none=True)
    with pool().connection() as conn:
        _begin(conn)
        found = conn.execute("SELECT company_id FROM cards WHERE id = %s", (card_id,)).fetchone()
        if found is None:
            raise NotFound(ErrorCode.CARD_NOT_FOUND)
        _lock_company(conn, found["company_id"])
        if changes:
            assignments = sql.SQL(", ").join(
                sql.SQL("{} = %s").format(sql.Identifier(column)) for column in changes
            )
            conn.execute(
                sql.SQL("UPDATE cards SET {} WHERE id = %s").format(assignments),
                (*changes.values(), card_id),
            )
    log.info("card.updated", extra={"fields": {"card_id": card_id, "changes": changes}})
    return get_card(card_id)


# --- reads ---------------------------------------------------------------------------------


def ping() -> None:
    with pool().connection() as conn:
        conn.execute("SELECT 1")


def get_company(company_id: str) -> dict[str, Any]:
    with pool().connection() as conn:
        row = conn.execute("SELECT id, name FROM companies WHERE id = %s", (company_id,)).fetchone()
        if row is None:
            raise NotFound(ErrorCode.COMPANY_NOT_FOUND)
        state = _company_state(conn, company_id)
    return {
        **row,
        "currency": state.currency,
        "credit_limit": state.credit_limit,
        "held": state.held,
        "posted": state.posted,
        "available": available(state),
    }


def get_card(card_id: str) -> dict[str, Any]:
    with pool().connection() as conn:
        row = conn.execute(
            """SELECT id, company_id, holder_name, last4, status, per_txn_limit,
                      monthly_limit, blocked_mccs
               FROM cards WHERE id = %s""",
            (card_id,),
        ).fetchone()
        if row is None:
            raise NotFound(ErrorCode.CARD_NOT_FOUND)
        spend = _card_state(conn, card_id).monthly_spend
    return {**row, "monthly_spend": spend}


def list_card_authorizations(card_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with pool().connection() as conn:
        rows = conn.execute(
            """SELECT id, network_transaction_id, amount, currency, merchant_amount,
                      merchant_currency, mcc, merchant_name, merchant_country, status,
                      decline_code, reasons, held_amount, cleared_amount, created_at
               FROM authorizations WHERE card_id = %s
               ORDER BY created_at DESC, id DESC LIMIT %s""",
            (card_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
