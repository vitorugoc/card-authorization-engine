"""Spend policy evaluation. Pure: no IO, no clock — everything arrives as arguments."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CompanyState:
    currency: str
    credit_limit: int
    held: int
    posted: int


@dataclass(frozen=True)
class Decision:
    approved: bool
    decline_code: str | None
    reasons: tuple[str, ...]
    available_after: int


def available(company: CompanyState) -> int:
    """Credit line. A prefunded account would change only this line."""
    return company.credit_limit - company.posted - company.held


def evaluate(amount: int, company: CompanyState) -> Decision:
    checks = (("insufficient_credit", amount > available(company)),)
    reasons = tuple(code for code, failed in checks if failed)
    approved = not reasons
    return Decision(
        approved=approved,
        decline_code=reasons[0] if reasons else None,
        reasons=reasons,
        available_after=available(company) - (amount if approved else 0),
    )
