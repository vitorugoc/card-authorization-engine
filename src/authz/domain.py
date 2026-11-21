"""Domain vocabulary shared by every layer: states, codes and business errors. No IO, no HTTP.

StrEnum members are real `str`s, so they compare, hash, JSON-encode and bind to SQL exactly
like the strings stored in Postgres (the CHECK constraints in db/schema.sql list the same values).
"""

from enum import StrEnum


class CardStatus(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"


class AuthStatus(StrEnum):
    DECLINED = "declined"
    PENDING = "pending"  # the only status that holds credit
    REVERSED = "reversed"
    CLEARED = "cleared"


class Outcome(StrEnum):
    APPROVED = "approved"
    DECLINED = "declined"


class DeclineCode(StrEnum):
    """Declaration order IS the precedence: the first failing code is the network code."""

    CARD_NOT_FOUND = "card_not_found"
    CARD_INACTIVE = "card_inactive"
    CURRENCY_MISMATCH = "currency_mismatch"
    MCC_BLOCKED = "mcc_blocked"
    EXCEEDS_PER_TRANSACTION_LIMIT = "exceeds_per_transaction_limit"
    EXCEEDS_CARD_MONTHLY_LIMIT = "exceeds_card_monthly_limit"
    INSUFFICIENT_CREDIT = "insufficient_credit"
    SYSTEM_ERROR = "system_error"


class EventType(StrEnum):
    REVERSAL = "reversal"
    CLEARING = "clearing"


class ErrorCode(StrEnum):
    AUTHORIZATION_NOT_FOUND = "authorization_not_found"
    CARD_NOT_FOUND = "card_not_found"
    COMPANY_NOT_FOUND = "company_not_found"
    AUTHORIZATION_NOT_PENDING = "authorization_not_pending"
    AMOUNT_EXCEEDS_HOLD = "amount_exceeds_hold"
    IDEMPOTENCY_MISMATCH = "idempotency_mismatch"


class DomainError(Exception):
    """A business rule said no. Carries a stable code; the API layer picks the HTTP status."""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code)
        self.code = code


class NotFound(DomainError):
    pass


class Conflict(DomainError):
    """The request is valid but contradicts current state or an earlier request."""
