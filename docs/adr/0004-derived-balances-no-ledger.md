# ADR 0004 — Derived holds, running posted total (no ledger in v1)

**Status:** accepted, amended 2026-11-20 after measuring (see History)

**Context.** Available credit and card monthly spend must be exact, and every authorization computes them while it holds the company lock (ADR 0002). Whatever that computation costs is paid on every swipe, and it blocks every other swipe for that company.

**Decision.**
- **Holds are derived.** `held = SUM(held_amount)` over the company's `pending` authorizations, through a partial covering index. Open holds are the small, live working set, so the sum stays cheap.
- **Posted spend is a running total**, `companies.posted`. A clearing adds to it in the same transaction, and under the same company lock, as the authorization update it summarizes.
- **Card monthly spend is derived.** It is a `SUM` over the current month only, using the `(card_id, created_at)` index, so it is bounded by one month of one card.
- No double-entry ledger.
- CHECK constraints enforce the hold invariants and `posted >= 0`.
- `authorization_events` is the append-only record of every change after insert.

**Why the counter cannot drift.** There is one writer path, the clearing transaction. It runs under the company lock, and it updates the authorization row and the counter atomically. The property test reconciles `companies.posted` against `SUM(cleared_amount)` after every random step.

**Alternatives.**
- **Derive everything** (the original v1 decision). It was simplest, but the cost grew with all history. See History.
- **A counter for held as well.** Holds change on every authorization, reversal and clearing, which means more writer paths to keep in sync. Holds are few, so deriving them is cheap and keeps the counter surface small.
- **A double-entry ledger.** It is the correct long-term model (repayments, fees, FX), but too much for the time box.

**Consequences.**
- The balance cost no longer depends on how long a customer has been with us.
- One denormalized number now exists. Any write that bypasses `service.py`, such as a manual SQL fix, must update it, and the reconciliation query detects one that doesn't:
  `SELECT id FROM companies c WHERE posted <> (SELECT COALESCE(SUM(cleared_amount), 0) FROM authorizations WHERE company_id = c.id)`.
- Migrating an existing database takes two steps:
  1. Add the column with `DEFAULT 0`.
  2. Backfill each company from that same `SUM`, holding the company lock.
- Repayments, when they exist, are a second writer path. That is the point to move to a ledger.

**History.** v1 derived both held and posted from every `pending` and `cleared` row. The `make loadtest` history scenario adds 500k settled clearings to one company, which is roughly a year for a mid-size customer. With derived balances, the company `SUM` took about 27 ms under the lock. The scenario fell to 59 req/s, and 909 of 2,000 authorizations failed closed on the 200 ms `lock_timeout`. With the running total, the same scenario runs at 312 req/s with zero errors.
