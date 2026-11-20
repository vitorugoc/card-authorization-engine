"""Model-based property test: random auth/reverse/clear sequences vs a reference model.

The engine must make the same approve/decline call as a trivially-correct Python model,
and balances must match it after every step. Clearing is drawn <= the remaining hold so the
credit invariant must always hold (over-clear is covered by its own example test).
"""

from dataclasses import dataclass

from conftest import COMPANY, auth_request, insert_card, insert_company, truncate
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from authz import service
from authz.models import ClearingRequest, ReversalRequest

# Two cards share one credit line, so either a card limit or the company limit can bind.
LIMIT, PER_TXN, MONTHLY = 60_000, 20_000, 40_000
CARDS = ("card_a", "card_b")


@dataclass
class Auth:
    ntid: str
    card: str
    held: int
    cleared: int = 0
    status: str = "pending"


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.data())
def test_random_lifecycle_matches_reference_model(data: st.DataObject) -> None:
    truncate()  # fixtures run per test, not per Hypothesis example
    insert_company(credit_limit=LIMIT)
    for card in CARDS:
        insert_card(card, per_txn_limit=PER_TXN, monthly_limit=MONTHLY, blocked_mccs=())
    model: list[Auth] = []

    for step in range(data.draw(st.integers(1, 15), label="steps")):
        pending = [a for a in model if a.status == "pending"]
        op = data.draw(st.sampled_from(["auth", "auth", "reverse", "clear"]), label="op")

        if op == "auth" or not pending:
            card = data.draw(st.sampled_from(CARDS), label="card")
            used = sum(a.held + a.cleared for a in model)
            card_used = sum(a.held + a.cleared for a in model if a.card == card)
            # Bias towards exact limits (and +1): random ints alone rarely hit a boundary.
            limits = (PER_TXN, MONTHLY - card_used, LIMIT - used)
            edges = [e for b in limits for e in (b, b + 1) if e > 0]
            amount = data.draw(st.integers(1, 25_000) | st.sampled_from(edges), label="amount")
            expected = (
                amount <= PER_TXN and card_used + amount <= MONTHLY and amount <= LIMIT - used
            )
            res = service.authorize(auth_request(f"ntx_{step}", card_id=card, amount=amount))
            assert (res["decision"] == "approved") is expected, (amount, card_used, used, res)
            if expected:
                model.append(Auth(f"ntx_{step}", card, held=amount))

        elif op == "reverse":
            a = data.draw(st.sampled_from(pending), label="auth")
            amount = data.draw(st.none() | st.integers(1, a.held), label="reversal")
            service.reverse(a.ntid, ReversalRequest(event_id=f"evt_{step}", amount=amount))
            a.held -= a.held if amount is None else amount
            a.status = "pending" if a.held else "reversed"

        else:
            a = data.draw(st.sampled_from(pending), label="auth")
            amount = data.draw(st.integers(1, a.held), label="clearing")
            service.clear(a.ntid, ClearingRequest(event_id=f"evt_{step}", amount=amount))
            a.held, a.cleared, a.status = 0, amount, "cleared"

        company = service.get_company(COMPANY)
        assert company["held"] == sum(a.held for a in model)
        assert company["posted"] == sum(a.cleared for a in model)
        assert 0 <= company["held"] + company["posted"] <= LIMIT
