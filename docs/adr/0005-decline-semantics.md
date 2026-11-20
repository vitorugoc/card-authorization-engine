# ADR 0005 — Decline semantics and failure mode

**Status:** accepted

**Context.** Networks accept one response code per authorization. Users and admins want to know why a card was declined. The engine must answer inside the network window.

**Decision.**
- A decline is HTTP 200. It is a business outcome, not an error.
- The response has one primary `decline_code`, chosen by fixed precedence, and all failing `reasons[]`.
- An unknown card is a 200 decline, `card_not_found`. Only malformed input gets 422.
- A lock timeout, statement timeout or DB error gets a 200 decline, `system_error`. The engine fails closed.

**Alternatives.**
- Short-circuit on the first failing rule: loses explainability and saves nothing.
- 4xx or 5xx for declines: conflates business results with transport errors.
- Fail open, or stand-in logic in the engine: stand-in is processor configuration and needs reconciliation.

**Consequences.**
- Decline responses can be fully explained.
- A slow DB declines legitimate spend. How generous to be on failure is a business setting at the processor.
