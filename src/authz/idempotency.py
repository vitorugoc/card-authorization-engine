"""Exactly-once effects under processor retries (ADR 0003).

Every request carries a key (network_transaction_id, event_id) that is UNIQUE in Postgres. The
first request stores its response; a retry gets that response back without re-evaluating, and
the same key with a different payload is a Conflict. Callers look up AFTER taking the company
lock, so concurrent duplicates serialize; a duplicate under another company's lock is caught by
the UNIQUE constraint and resolved by `replay_after_race`.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any, Literal

from psycopg import errors
from psycopg.rows import DictRow

from authz.db import Conn, pool
from authz.domain import AuthStatus, Conflict, ErrorCode, EventType

log = logging.getLogger("authz")

Kind = Literal["authorization", "event"]

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


def same_hash(req_hash: str) -> Matcher:
    return lambda row: bool(row["request_hash"] == req_hash)


def same_event(ntid: str, type_: EventType, amount: int | None) -> Matcher:
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


def stored_response(conn: Conn, kind: Kind, key: str, matches: Matcher) -> dict[str, Any] | None:
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


def replay_after_race(
    exc: errors.UniqueViolation, kind: Kind, key: str, matches: Matcher
) -> dict[str, Any]:
    """The same id was committed meanwhile under ANOTHER company's lock; UNIQUE caught it."""
    if exc.diag.constraint_name != _UNIQUE_KEYS[kind]:
        raise exc
    with pool().connection() as conn:
        stored = stored_response(conn, kind, key, matches)
    if stored is None:
        raise RuntimeError(f"UNIQUE violation on {kind} {key} but no committed row")
    return stored
