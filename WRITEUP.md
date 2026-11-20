# Write-up: Corporate Card Authorization Engine

## The problem and why I picked it
A spend policy can **prevent** out-of-policy spend at exactly one point: when the card is swiped. Anywhere later, it can only flag it in an expense review.
That swipe-time authorization is also the code path where bugs cost the most:
- an overspend loses money;
- a false decline leaves an employee stranded abroad.

For a corporate card company with LatAm and cross-border customers, this is core business. I picked the smallest piece where correctness is not optional.

## What I built
A Python service that a card processor calls synchronously for each swipe. It:
- runs 6 policy checks (card status, currency, blocked MCC, per-transaction limit, card monthly limit, company credit);
- answers approve/decline with every failing reason plus one primary network code;
- places a hold on the company credit line;
- handles reversals (full/partial) and a final clearing.

There are 59 tests against a real Postgres, and a narrated demo (`make demo`).

**Two guarantees, each proven by tests:**
1. **No overspend under concurrency.** Every money or policy write locks the company row (`SELECT … FOR UPDATE`). Balances are derived by `SUM` under that lock, and CHECK constraints enforce the hold invariants. A burst of 20 concurrent auths approves exactly as many as fit. When I removed the lock, the test failed.
2. **Exactly-once effects under retries.** `network_transaction_id` and `event_id` are unique keys. I store each request's hash and its response:
   - a replay returns the stored response and never re-evaluates;
   - the same id with a different payload returns 409.

   A cross-company race on the same id is caught by the UNIQUE constraint and resolved.

## Key decisions and trade-offs
- **Pessimistic per-company lock vs SERIALIZABLE/optimistic.** It gives a guarantee instead of retry loops, and one lock target means no deadlocks. The ceiling is one hot row per company. The upgrade path is per-card locks plus a reservation counter.
- **Derived balances, no double-entry ledger (yet).** Authorization rows are the source of truth, so no counter can drift. The cost is `SUM` over history. A ledger fed by clearings is the next step.
- **Declines are HTTP 200; timeouts fail closed** (`system_error` after a 200ms lock wait). Stand-in generosity is processor configuration, not engine logic.
- **Over-clear is posted** even past the limit. Policy is enforced at authorization; clearing records what the network already settled.
- **Simplifications I accepted:**
  - integer minor units, one currency per company (the network does FX);
  - UTC month window;
  - no hold expiry;
  - no HMAC/ops authentication (named as the first production gaps).

## How I used AI
1. **Design by "grilling".** I chose the problem and the stack. An AI agent prompted as a staff fintech engineer then answered a structured design interview in rounds (19 decisions, each with rejected alternatives). I reviewed and approved every decision at a checkpoint. Log: `docs/grill-log.md`.
2. **Spec, then TDD.** The answers became a phased spec (`docs/sdd.md`) and 6 ADRs. Claude Code implemented phase by phase, red then green; the git history shows each test commit before its implementation.
3. **Tests as the guardrail, verified by mutation.** I deliberately broke the code to check that the tests catch it:
   - removing the lock;
   - removing the race handler;
   - `>` → `>=` on limits.

   This exposed a weak test. The first property test could not catch a monthly-limit off-by-one, because the company limit always bound first. I fixed it with two cards and boundary-biased generators.

   Other AI mistakes were caught the same way, such as a test that set up its data in the wrong order.

## What I'd do next
- Double-entry ledger and repayments.
- Hold expiry.
- HMAC webhook verification and ops authentication.
- Stand-in reconciliation.
- Multi-clearing and force-post.
- Company timezone for monthly limits.
- Per-card locks if one company's throughput demands it.
- Metrics (p99 latency per decision).
