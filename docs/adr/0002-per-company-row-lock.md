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
- All authorizations for one company are serialized. Measured with `make loadtest` on an M3 Pro: about 240 auth/s for one company, flat from 16 to 64 concurrent clients (each auth holds the lock for about 4 ms; extra clients only add queueing). Across 16 companies, throughput reaches about 750 req/s. There were no `system_error` responses.
- Upgrade path: per-card locks plus a company reservation counter, or sharded balances.
