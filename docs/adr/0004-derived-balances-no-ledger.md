# ADR 0004 — Balances derived from authorization rows (no ledger in v1)

**Status:** accepted

**Context.** Available credit and card monthly spend must be exact.

**Decision.**
- Company held and posted totals and card monthly spend are computed by `SUM` over `authorizations` under the company lock.
- No stored counters and no double-entry ledger.
- CHECK constraints enforce the hold invariants.
- `authorization_events` is the append-only record of every change after insert.

**Alternatives.**
- Stored counters: fast, but they can drift from the rows.
- A double-entry ledger: the correct long-term model, but too much for the time box.

**Consequences.**
- Totals are always consistent with the rows.
- `SUM` cost grows with history (mitigated by partial indexes).
- Next step: a ledger fed by clearings, with materialized running balances.
