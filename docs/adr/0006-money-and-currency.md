# ADR 0006 — Money and currency representation

**Status:** accepted

**Context.** Cross-border spend is central for Jeeves' LatAm customers. FX conversion is done by the network.

**Decision.**
- Amounts are integer minor units (`BIGINT`, `int`). Pydantic `StrictInt` rejects floats.
- Each company has one currency.
- Policy runs on the network's billing `amount`/`currency`. `merchant_amount`/`merchant_currency` are stored for display only.
- If the billing currency differs from the company currency, the auth declines with `currency_mismatch`.

**Alternatives.**
- Decimal: exponent and quantization questions with no benefit.
- Declining foreign spend: kills the cross-border use case.
- FX conversion inside the engine: the network's job.

**Consequences.**
- SQL sums are exact.
- Currency exponents differ (CLP 0, MXN 2), so amounts are formatted only at display time.
