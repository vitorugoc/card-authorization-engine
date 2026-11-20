# ADR 0001 — Credit-line funding model

**Status:** accepted

**Context.** Corporate cards draw either on a prefunded balance or on a credit line. Jeeves sells both. Its headline product is a corporate credit card.

**Decision.** Each company has one credit line: `available = credit_limit − Σ posted − Σ held`. Repayments are out of scope. The formula lives in one function, `policy.available()`.

**Alternatives.**
- Prefunded balance: the same math with the sign flipped, but it needs a top-up flow before the demo is interesting.
- Both models behind an interface: one real implementation makes the interface speculative.

**Consequences.**
- The demo needs no funding flow.
- Supporting prefunded accounts later changes only `available()` plus a funding endpoint.
