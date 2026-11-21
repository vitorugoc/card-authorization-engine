# Write-up: Corporate Card Authorization Engine

## The problem
A spend policy can only *prevent* bad spend at one moment: when the card is swiped. After that, it can only flag it in an expense review. That swipe-time decision is also where bugs hurt most. Approve too much and the company loses money. Decline wrongly and an employee is stuck at a hotel desk abroad. For a corporate card company in LatAm, this is the core of the business, so I picked the smallest piece of it where correctness is not optional.

## What I built
A Python service that the card processor calls on every swipe. It checks six rules (card status, currency, blocked merchant category, per-transaction limit, card monthly limit, company credit). It answers approve or decline with every failing reason, places a hold on the company's credit line, and later handles reversals and the final clearing.

I focused on two guarantees and proved each one with tests:
1. **No overspend under concurrency.** Every write locks the company row first, and balances are computed under that lock. In the demo, 20 simultaneous swipes approve exactly the 8 that fit. When I removed the lock on purpose, the test failed.
2. **Exactly-once under retries.** Processors retry. Each transaction id is unique and its response is stored, so a retry gets the same answer and a changed payload gets a 409.

It runs with one command (`make up && make demo`). It has 74 tests against a real Postgres, HMAC-signed webhooks, CI and a load test.

## Trade-offs I chose
- **One lock per company instead of optimistic retries.** It is simple to reason about and cannot deadlock. The cost is a throughput ceiling per company. I measured it at about 240 auth/s for a single company, and about 750/s spread across 16 companies, with zero errors. If one customer ever needs more, the next step is per-card locks.
- **Balances derived from the authorizations, no ledger yet.** No counter can drift, at the cost of a `SUM` per decision. A double-entry ledger is the natural next step.
- **Fail closed.** If the database is slow (200 ms lock wait), the answer is a decline, not a guess. Whether to approve offline is a processor setting, not engine logic.
- **Scope cuts:** one currency per company, UTC months, no hold expiry, no auth on the admin routes.

## How I used AI
I used Claude Code throughout, but deliberately, with me in the driver's seat.
- **Design first, code later.** Before writing code, I had the AI grill me with design questions round by round: locking, idempotency, failure modes, data model. For each one it laid out the options; I chose, and wrote down what I rejected and why (`docs/grill-log.md`, 6 ADRs).
- **Spec, then tests, then code.** The decisions became a phased spec (`docs/sdd.md`). The AI implemented it phase by phase, always with a failing test first. The git history shows each red commit before its green one.
- **I didn't trust green tests blindly.** I broke the code on purpose to see if the tests noticed: I removed the lock, removed the race handling, and changed `>` to `>=`. One check exposed a weak property test that could never hit the monthly-limit boundary, so I fixed the generator. The same habit caught other AI mistakes, like a test that set up its data in the wrong order.
- **I asked for a critical review at the end.** That review added the explicit errors, the typed API responses, the signed webhooks, the load test and CI.

AI made me faster. I still owned every decision, and I can explain each line.

## Next steps
Double-entry ledger and repayments, hold expiry, auth on the admin routes, stand-in reconciliation, multiple clearings, company time zones, per-card locks if needed, and latency metrics per decision.
