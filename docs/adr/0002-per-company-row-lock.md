# ADR 0002 — Per-company row lock for concurrency control

**Status:** accepted

**Context.** Concurrent authorizations for the same company must never overspend the company limit or a card's monthly limit. The decision needs several reads (card, totals) and returns several reasons.

**Decision.**
- Every money or policy write (authorize, reverse, clear, PATCH card) starts with `SELECT … FROM companies WHERE id = $1 FOR UPDATE`.
- It is the only lock target, and it is always taken first.
- The card is re-read after the lock, so a freeze cannot race a decision.
- Each transaction sets `lock_timeout = 200ms` and `statement_timeout = 1s`.

**Alternatives.**
- SERIALIZABLE with retries: correct, but the demo would show retries instead of a guarantee.
- Optimistic version column: a burst of conflicts is exactly the workload we have.
- Single conditional UPDATE: awkward with two limits and multi-reason output.
- Per-card plus per-company locks: two lock targets add a lock order to get wrong.

**Consequences.**
- One lock target means no deadlocks between our own transactions.
- All authorizations for one company are serialized. That is fine up to hundreds of authorizations per second per company.
- Upgrade path: per-card locks plus a company reservation counter, or sharded balances.
