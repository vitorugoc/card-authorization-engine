# ADR 0003 — Idempotency via network ids, stored hash and response

**Status:** accepted

**Context.** Processors retry webhooks using the same `network_transaction_id`. They do not send Idempotency-Key headers.

**Decision.**
- `network_transaction_id` is UNIQUE on authorizations, and `event_id` is UNIQUE on events.
- Each row stores a `request_hash` (sha256 of the canonical decision-relevant fields) and the exact `response`.
- Same id with the same hash: return the stored response and never re-evaluate.
- Same id with a different hash: return 409 `idempotency_mismatch` and log a WARNING.
- Declines are persisted too, so a declined request also replays identically.
- The lookup happens after the company lock. A cross-company race on the same id is caught as `UniqueViolation` and resolved by re-running the lookup.

**Alternatives.**
- Header-only key: processors don't send one.
- Both a header and the network id: two keys that can disagree.
- Re-evaluate on replay: a retry could flip approve to decline after funds moved.
- Return the original response on a mismatch: hides a processor contract violation.

**Consequences.**
- Retries are safe and auditable.
- In real JIT flows a non-200 can trigger processor stand-in, so a 409 must be alerted on in production.
