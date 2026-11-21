# Write-up: Corporate Card Authorization Engine

## The problem
A spend policy can only *prevent* bad spend at one moment: the swipe. After that, it can only flag it. That decision is also where bugs hurt most: approve too much and the company loses money; decline wrongly and an employee is stuck at a hotel desk abroad. I picked the smallest core piece of a corporate card business where correctness is not optional.

## What I built
A Python service the card processor calls on every swipe. It checks six rules (card status, currency, blocked merchant category, per-transaction, monthly and company credit limits), answers with every failing reason, holds the amount on the company's credit line, and handles reversals and clearing. Two guarantees, each proven by tests:
1. **No overspend under concurrency.** Every write locks the company row first. 20 simultaneous swipes approve exactly the 8 that fit; without the lock, the test fails.
2. **Exactly-once under retries.** A retried transaction gets the stored answer; a changed payload gets a 409.

`make up && make demo` runs it. 74 tests on a real Postgres, signed webhooks, CI, load test.

## Trade-offs I chose
- **One lock per company instead of optimistic retries.** Simple and deadlock-free, but capped per company: I measured ~240 auth/s for one company, ~750/s across 16, zero errors. Per-card locks are the upgrade path.
- **Balances derived from the authorizations, no ledger yet.** No counter can drift; the cost is a `SUM` per decision.
- **Fail closed.** A slow database (200 ms lock wait) means decline, not a guess.
- **Scope cuts:** one currency per company, UTC months, no hold expiry, no auth on the admin routes.

## How I used AI
I used Claude Code throughout, with me in the driver's seat.
- **Design first, code later.** Before any code, I had the AI grill me round by round: locking, idempotency, failure modes, data model. It laid out options; I chose and recorded what I rejected and why (`docs/grill-log.md`, 6 ADRs).
- **Spec, then tests, then code.** The decisions became a phased spec (`docs/sdd.md`), implemented phase by phase with a failing test first; git history shows each red commit before its green.
- **I didn't trust green tests blindly.** I broke the code on purpose (removed the lock, the race handling, flipped `>` to `>=`) to see if tests noticed. That exposed a property test that never hit the monthly-limit boundary, which I fixed.
- **A critical review at the end** added typed responses, signed webhooks, the load test and CI.

AI made me faster. I still owned every decision, and I can explain each line.

## Next steps
Double-entry ledger and repayments, hold expiry, admin auth, stand-in reconciliation, multiple clearings, company time zones, latency metrics.
