"""Spend policy evaluation. Pure: no IO, no clock — everything arrives as arguments."""

from dataclasses import dataclass

from authz.domain import CardStatus, DeclineCode


@dataclass(frozen=True)
class CompanyState:
    currency: str
    credit_limit: int
    held: int
    posted: int


@dataclass(frozen=True)
class CardState:
    status: CardStatus
    per_txn_limit: int
    monthly_limit: int
    blocked_mccs: frozenset[str]
    monthly_spend: int  # held + cleared of this card's auths created this UTC month


@dataclass(frozen=True)
class Decision:
    approved: bool
    decline_code: DeclineCode | None
    reasons: tuple[DeclineCode, ...]
    available_after: int


def available(company: CompanyState) -> int:
    """Credit line (ADR 0001). A prefunded account would change only this line."""
    return company.credit_limit - company.posted - company.held


def evaluate(
    amount: int, currency: str, mcc: str, card: CardState, company: CompanyState
) -> Decision:
    """Run every rule; all failures go in `reasons`, the first one is the network code.

    Order below IS the precedence (ADR 0005), and matches DeclineCode's declaration order.
    """
    checks = (
        (DeclineCode.CARD_INACTIVE, card.status != CardStatus.ACTIVE),
        (DeclineCode.CURRENCY_MISMATCH, currency != company.currency),
        (DeclineCode.MCC_BLOCKED, mcc in card.blocked_mccs),
        (DeclineCode.EXCEEDS_PER_TRANSACTION_LIMIT, amount > card.per_txn_limit),
        (
            DeclineCode.EXCEEDS_CARD_MONTHLY_LIMIT,
            card.monthly_spend + amount > card.monthly_limit,
        ),
        (DeclineCode.INSUFFICIENT_CREDIT, amount > available(company)),
    )
    reasons = tuple(code for code, failed in checks if failed)
    approved = not reasons
    return Decision(
        approved=approved,
        decline_code=reasons[0] if reasons else None,
        reasons=reasons,
        available_after=available(company) - (amount if approved else 0),
    )
