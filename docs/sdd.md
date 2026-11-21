# SDD — Corporate Card Authorization Engine

Status: approved (2026-11-20) · implemented (see §16 for as-built notes) · Source of decisions: [`grill-log.md`](grill-log.md) · ADRs: [`adr/`](adr/)

## 1. Problem

A corporate card program lets employees spend company money. The **authorization** step is the only
place where a spend policy can *prevent* out-of-policy spend, rather than flagging it later in an
expense review. Authorization is also where bugs cost the most. An overspend loses money. A false
decline strands an employee abroad.

The card processor calls this engine synchronously for every swipe. The engine must answer
approve/decline within the network window, with a reason. It must place a hold on the company's
credit. It must stay correct when the processor **retries** and when several authorizations for the
same company arrive **at the same time**.

## 2. Goals / Non-goals

**Guarantees (the two things the demo proves):**
1. **No overspend under concurrency.** For any company, `held + posted ≤ credit_limit` holds after
   any interleaving of authorizations. Over-clear is the one documented exception (§7.3).
2. **Exactly-once effects under retries.** Replaying a request with the same id never creates a
   second hold or a second posting, and it never changes the original decision.

**In scope:** authorization with 6 policy checks, full/partial reversal, single final clearing
(under/equal/over), card freeze, read endpoints, a narrated demo script.

**Out of scope (named in the write-up):** disputes, incremental auth, FX conversion, hold expiry,
multi-clearing / force-post, late clearing after reversal, double-entry ledger, repayments,
ops authentication, TLS, rate limiting, stand-in logic, company
timezones, metrics endpoint.

## 3. Glossary

| Term | Meaning |
|------|---------|
| Company account | The `companies` row. Holds `credit_limit` and is the **lock target** for every money write |
| Authorization (auth) | One swipe decision. Approved auths start `pending` with a hold |
| Hold (`held_amount`) | Credit reserved by a pending auth, not yet posted |
| Posted (`cleared_amount`) | Amount settled by a clearing |
| Available credit | `credit_limit − Σ posted − Σ held` for the company |
| Card monthly spend | `Σ (held + cleared)` of the card's auths with `created_at` in the current UTC month |
| `network_transaction_id` (ntid) | Processor id of an authorization. Idempotency key |
| `event_id` | Processor id of a reversal/clearing. Idempotency key |
| Billing amount | `amount`/`currency`: what the company pays, already converted by the network |
| Merchant amount | `merchant_amount`/`merchant_currency`: what the merchant charged, display only |

## 4. Domain model and invariants

```
Company 1 ── * Card 1 ── * Authorization 1 ── * AuthorizationEvent
```

Authorization state machine:

```
            ┌─(policy fails)──► declined   (terminal, held=0, cleared=0)
request ────┤
            └─(policy passes)─► pending ──(full reversal)──► reversed  (terminal, held=0, cleared=0)
                                   │  └─(partial reversal)─► pending (held reduced)
                                   └──(clearing)──────────► cleared   (terminal, held=0, cleared=amount)
```

Invariants (enforced by DB CHECKs where possible):
- I1 `0 ≤ held_amount ≤ amount`
- I2 `status ≠ 'pending' ⇒ held_amount = 0`
- I3 `cleared_amount > 0 ⇒ status = 'cleared'`
- I4 (service, under lock) an auth is approved only if `amount ≤ available` and `amount ≤ card remaining monthly`
- I5 every change to `held_amount`/`cleared_amount`/`status` after insert has exactly one `authorization_events` row

## 5. API contract

All amounts are integers in minor units. All IDs are prefixed text (`co_`, `card_`, `auth_`, `evt_`).

### 5.1 `POST /v1/authorizations` (processor-facing)

Request:
```json
{
  "network_transaction_id": "ntx_001",
  "card_id": "card_demo",
  "amount": 12000, "currency": "MXN",
  "merchant_amount": 700, "merchant_currency": "USD",
  "mcc": "5812", "merchant_name": "Taqueria El Paso", "merchant_country": "US"
}
```
Validation (pydantic, `extra="ignore"`): `amount` StrictInt `1..10^12`; `merchant_amount` StrictInt `≥ 0`;
currencies `^[A-Z]{3}$`; `mcc` `^\d{4}$`; `merchant_country` `^[A-Z]{2}$`; ids ≤ 64 chars;
`merchant_name` ≤ 255. Invalid input returns **422**.

Response **200** (approved or declined):
```json
{
  "authorization_id": "auth_…", "network_transaction_id": "ntx_001",
  "decision": "approved", "decline_code": null, "reasons": [],
  "available_credit_after": 988000
}
```
Response **409** `{"error": "idempotency_mismatch"}`: same ntid, different request hash.

Decline codes, in **precedence order** (the first failing one becomes `decline_code`; every failing one goes in `reasons`):

| Code | Rule |
|------|------|
| `card_not_found` | Card id unknown (pre-check, not persisted) |
| `card_inactive` | `card.status ≠ 'active'` |
| `currency_mismatch` | `currency ≠ company.currency` |
| `mcc_blocked` | `mcc ∈ card.blocked_mccs` |
| `exceeds_per_transaction_limit` | `amount > card.per_txn_limit` |
| `exceeds_card_monthly_limit` | `card_monthly_spend + amount > card.monthly_limit` |
| `insufficient_credit` | `amount > available_credit` |
| `system_error` | Lock/statement timeout or DB error (fail closed, not persisted) |

### 5.2 `POST /v1/authorizations/{ntid}/reversals`

Request: `{"event_id": "evt_r1", "amount": 2000}`. `amount` is optional; omitting it reverses the whole remaining hold.
- 200: `{"authorization_id", "status", "held_amount", "cleared_amount", "available_credit_after"}`
- 404: unknown ntid · 409 `authorization_not_pending` · 409 `amount_exceeds_hold` · 409 `idempotency_mismatch`
- If the reversal amount equals the remaining hold, status becomes `reversed`. Otherwise status stays `pending`.

### 5.3 `POST /v1/authorizations/{ntid}/clearings`

Request: `{"event_id": "evt_c1", "amount": 13800}`. Always final: it posts `amount`, sets `held_amount = 0` and status `cleared`.
Responses are the same as for reversal. Over-clear is accepted (§7.3).

### 5.4 Ops (P6)

- `GET /v1/companies/{id}`: `{id, currency, credit_limit, held, posted, available}`
- `GET /v1/cards/{id}`: card fields plus `monthly_spend`
- `GET /v1/cards/{id}/authorizations`: most recent first, limit 50
- `PATCH /v1/cards/{id}`: `{status?, per_txn_limit?, monthly_limit?, blocked_mccs?}` with `extra="forbid"`. Takes the company lock.

## 6. Schema (`db/schema.sql`)

```sql
CREATE TABLE companies (
  id            text PRIMARY KEY,
  name          text NOT NULL,
  currency      char(3) NOT NULL,
  credit_limit  bigint NOT NULL CHECK (credit_limit >= 0),
  posted        bigint NOT NULL DEFAULT 0 CHECK (posted >= 0),  -- running total (ADR 0004)
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cards (
  id             text PRIMARY KEY,
  company_id     text NOT NULL REFERENCES companies(id),
  holder_name    text NOT NULL,
  last4          char(4) NOT NULL,
  status         text NOT NULL CHECK (status IN ('active','frozen')),
  per_txn_limit  bigint NOT NULL CHECK (per_txn_limit > 0),
  monthly_limit  bigint NOT NULL CHECK (monthly_limit > 0),
  blocked_mccs   char(4)[] NOT NULL DEFAULT '{}',
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE authorizations (
  id                      text PRIMARY KEY,
  network_transaction_id  text NOT NULL UNIQUE,
  card_id                 text NOT NULL REFERENCES cards(id),
  company_id              text NOT NULL REFERENCES companies(id),
  amount                  bigint NOT NULL CHECK (amount > 0),
  currency                char(3) NOT NULL,
  merchant_amount         bigint NOT NULL,
  merchant_currency       char(3) NOT NULL,
  mcc                     char(4) NOT NULL,
  merchant_name           text NOT NULL,
  merchant_country        char(2) NOT NULL,
  status                  text NOT NULL CHECK (status IN ('declined','pending','reversed','cleared')),
  decline_code            text,
  reasons                 text[] NOT NULL DEFAULT '{}',
  held_amount             bigint NOT NULL CHECK (held_amount >= 0 AND held_amount <= amount),
  cleared_amount          bigint NOT NULL DEFAULT 0 CHECK (cleared_amount >= 0),
  policy_snapshot         jsonb NOT NULL,
  request_hash            text NOT NULL,
  response                jsonb NOT NULL,
  created_at              timestamptz NOT NULL DEFAULT now(),
  CHECK (status = 'pending' OR held_amount = 0),
  CHECK (cleared_amount = 0 OR status = 'cleared')
);
CREATE INDEX authorizations_company_pending ON authorizations (company_id) INCLUDE (held_amount)
  WHERE status = 'pending';
CREATE INDEX authorizations_card_created ON authorizations (card_id, created_at);

CREATE TABLE authorization_events (
  id                text PRIMARY KEY,
  event_id          text NOT NULL UNIQUE,
  authorization_id  text NOT NULL REFERENCES authorizations(id),
  type              text NOT NULL CHECK (type IN ('reversal','clearing')),
  amount            bigint NOT NULL CHECK (amount > 0),
  request_hash      text NOT NULL,
  response          jsonb NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);
```

`db/init.sh` creates two databases from this schema: `authz` (app and demo) and `authz_test` (tests).

## 7. Algorithms

### 7.1 Authorize (`service.authorize`)

```
BEGIN
  SET LOCAL lock_timeout = '200ms'; SET LOCAL statement_timeout = '1s'
  company_id ← SELECT company_id FROM cards WHERE id = :card_id        -- unlocked; immutable column
  if none → return decline(card_not_found)                              -- not persisted
  company   ← SELECT … FROM companies WHERE id = :company_id FOR UPDATE -- THE lock
  existing  ← SELECT request_hash, response FROM authorizations WHERE network_transaction_id = :ntid
  if existing: return existing.response if hash matches else raise IdempotencyMismatch (409)
  card      ← SELECT … FROM cards WHERE id = :card_id                   -- re-read after lock (no TOCTOU)
  totals    ← held SUM over pending holds + companies.posted counter; card monthly spend SUM
  decision  ← policy.evaluate(card, company, totals, request)           -- pure
  INSERT authorization (status pending|declined, held = amount|0, policy_snapshot, request_hash, response)
COMMIT
```
- **Cross-company ntid race.** A request may reuse an ntid with a card from another company. That request takes a different lock and hits the `UNIQUE` constraint on insert. Handle it this way: catch `UniqueViolation`, roll back, and re-run the idempotency lookup in a fresh transaction. If the hash matches, replay the stored response. Otherwise return 409.
- **Timeouts.** A lock timeout, statement timeout or `OperationalError` rolls back and returns 200 `system_error`. The decline is logged at ERROR and not persisted.

### 7.2 Reverse

Find the auth by ntid (404 if none) → lock its company → event idempotency check on `event_id` → re-read auth →
`status ≠ pending` returns 409 `authorization_not_pending` → `amount > held_amount` returns 409 `amount_exceeds_hold` →
`held -= amount` (status becomes `reversed` when held reaches 0) → insert event → commit.

### 7.3 Clear

Steps are the same as for reverse up to the status check. Then set `cleared_amount = amount`, `held_amount = 0` and status `cleared`. Insert the event and commit.
**Over-clear** (`amount > original hold`) is posted in full, and available credit may go negative. It is logged with `over_clear_delta`. Policy is enforced at authorization; clearing records what the network settled.

### 7.4 Request hash

The hash is `sha256` of canonical JSON (sorted keys) built from the decision-relevant fields.
- Auth: `card_id, amount, currency, mcc, merchant_country`.
- Event: `ntid, type, amount` (the resolved amount for a full reversal is the *requested* amount, which is `null` when omitted).

## 8. Policy engine (`policy.py`, pure)

- Inputs: `amount`, `currency`, `mcc` and frozen dataclasses `CardState(status, per_txn_limit, monthly_limit, blocked_mccs, monthly_spend)` and `CompanyState(currency, credit_limit, held, posted)`.
- The rules are one ordered tuple of `(decline_code, failed: bool)` pairs. The tuple order IS the precedence.
- `evaluate()` collects every failing code. It returns `Decision(approved, decline_code=first, reasons=all, available_after)`.
- No IO and no clock. `monthly_spend` is computed by the service in SQL (UTC month start) and passed in.

## 9. Code layout

```
src/authz/models.py   pydantic request models: the trust boundary
src/authz/policy.py   pure rules + evaluate + available()
src/authz/service.py  transaction scripts: authorize, reverse, clear, freeze/update card
src/authz/db.py       psycopg_pool; DATABASE_URL env (SQL lives next to its transaction in service.py)
src/authz/api.py      FastAPI routes, pydantic models, status mapping, timing middleware
src/authz/logging.py  ~15-line JSON formatter
scripts/seed.py  scripts/demo.py
tests/…  db/schema.sql  db/init.sh
```
There are no interfaces, repositories or rule registries. Each module has one implementation, so an abstraction would be speculative.

## 10. Observability

- **Logs.** Each business event writes one JSON log line: `authorization.decided`, `reversal.applied`, `clearing.posted`, `idempotency.mismatch` (WARNING) and `authorization.system_error` (ERROR).
- **Decision log fields:** `authorization_id`, `network_transaction_id`, `card_id`, `company_id`, `amount`, `currency`, `mcc`, `merchant_country`, `decision`, `decline_code`, `reasons`, `available_after`, `replay`, `duration_ms`.
- **Never logged:** `holder_name`.
- **Latency.** Every response carries an `X-Process-Time` header.
- **Audit.** The `authorizations` rows (with `response` and `policy_snapshot`) plus the append-only `authorization_events` rows form the audit trail.

## 11. Security baseline

- **Input.** Strict validation at the boundary (§5) and parameterized SQL only.
- **Card data.** No PAN, CVV or expiry is stored. `card_id` is the processor token, and only `last4` is kept.
- **Errors.** `system_error` responses are generic and expose no internals.
- **Authentication.** Processor routes require an HMAC signature. Ops routes require a shared bearer token (`OPS_TOKEN`). Required secrets are checked at boot.
- **Out of scope (named in the write-up):** per-user ops identity and audit (SSO), TLS, rate limiting and secrets management.

## 12. Test strategy

| Layer | Tool | What |
|-------|------|------|
| Unit | pytest | each rule at boundary (`== limit` approves, `+1` declines), multi-reason output, precedence, `available()`, request hash stability |
| Service integration | pytest + real Postgres (`authz_test`, TRUNCATE before each test) | approve/decline persistence, replay, mismatch, lifecycle, timeouts |
| Concurrency | `ThreadPoolExecutor` + `threading.Barrier` on `service.authorize` | exactly `floor(remaining/amount)` approvals; `held + posted ≤ limit` |
| API | FastAPI `TestClient` | status mapping 200/404/409/422, response shape |
| Property (stretch) | Hypothesis, ~50 examples | random auth/reverse/clear sequences keep I1–I4 |

TDD rule: each phase writes its acceptance test and unit tests first. Commit them red (`test: … (red)`), then implement to green (`feat: …`).

## 13. Delivery phases

Each phase is a vertical slice. It ends with green tests and adds one step to `demo.py`.

Demo seed (fixed IDs):
- Company `co_demo`: MXN, `credit_limit` 1,000,000 (10,000.00).
- Card `card_demo`: `per_txn_limit` 50,000, `monthly_limit` 100,000, blocked MCC `7995`.

### P0 — Skeleton (≈1.5h)
- **Build:** `pyproject.toml` (uv, fastapi, uvicorn, psycopg[binary,pool], pydantic; dev: pytest, httpx, ruff, mypy, hypothesis, pytest-cov), `Dockerfile`, `docker-compose.yml` (postgres:16 + app), `db/schema.sql`, `db/init.sh`, `Makefile` (`up down seed demo test lint fmt`), `conftest.py` with the TRUNCATE fixture.
- **Test first:** `test_db_smoke`, which connects to `authz_test` and checks that the 4 tables exist.
- **Acceptance:** `make up && make test` passes and `make lint` is clean.

### P1 — Approve path (≈2h)
- **Build:** `POST /v1/authorizations` running the full §7.1 transaction (lock from day one), with the funds rule only; `GET /v1/companies/{id}`.
- **Tests first:** `test_approve_creates_hold_and_reduces_available`, `test_insufficient_credit_declines_and_persists`, `test_unknown_card_declines_card_not_found`, `test_api_rejects_float_amount_422`.
- **Demo step 1:** approve 120.00 at a restaurant; available credit drops.

### P2 — Policy engine (≈2.5h)
- **Build:** `policy.py` with all rules, precedence and multi-reason output; `policy_snapshot` persisted; monthly window passed in from the service.
- **Tests first:**
  - `test_policy.py` covers each rule at its boundaries plus precedence.
  - `test_casino_600_declines_with_mcc_and_per_txn_reasons`
  - `test_monthly_limit_counts_holds_and_cleared_current_month_only`
  - `test_currency_mismatch_declines`
- **Demo step 2:** 600.00 at a casino declines with `[mcc_blocked, exceeds_per_transaction_limit]`.

### P3 — Idempotency (≈1.5h)
- **Build:** request hash, stored response, replay, 409 on mismatch, and `UniqueViolation` handling for the cross-company race.
- **Tests first:**
  - `test_replay_returns_identical_response_no_new_hold`
  - `test_replay_of_decline_is_identical`
  - `test_same_ntid_different_amount_409`
  - `test_same_ntid_other_company_card_409`
- **Demo step 3:** a replay returns an identical response; a tampered amount returns 409.

### P4 — Concurrency proof (≈2h)
- **Build:** session timeouts, and `system_error` fail-closed mapping.
- **Tests first:**
  - `test_burst_20x100_with_880_remaining_approves_exactly_8`
  - `test_burst_duplicate_ntid_creates_single_hold`
  - `test_lock_timeout_returns_system_error`: hold the company lock in another connection and assert a decline within about 1s.
- **Demo step 4:** a burst of 20 concurrent 100.00 auths approves exactly 8, and the invariant line prints OK.

### P5 — Reversal + clearing (≈3h)
- **Build:** `authorization_events`, the §7.2 and §7.3 flows, and the endpoints.
- **Tests first:**
  - `test_partial_reversal_reduces_hold`
  - `test_full_reversal_sets_reversed`
  - `test_reversal_exceeding_hold_409`
  - `test_clearing_under_hold_releases_remainder`
  - `test_over_clear_posts_full_amount`
  - `test_clearing_on_reversed_409`
  - `test_event_replay_idempotent`
  - `test_event_id_mismatch_409`
  - `test_unknown_ntid_404`
- **Demo steps 5–6:** partial reversal of 20.00, then a clearing at 138.00 with the tip added.

**── HARD CUT LINE: P0–P5 ship no matter what ──**

### P6 — Ops surface (≈1.5h)
- **Build:** `scripts/seed.py`, `GET` card and its authorizations, `PATCH` card (takes the company lock), `requests.http`.
- **Tests first:** `test_patch_freeze_then_auth_declines_card_inactive`, `test_patch_rejects_unknown_fields_422`.
- **Demo step 7:** freeze the card live; the next auth declines with `card_inactive`.

### P7 — Polish (≈2h)
- **Build:** JSON logging, Hypothesis property test, README, 6 ADRs, `WRITEUP.md`.
- **Acceptance:** `make lint` and `make test` pass, `make demo` runs end-to-end, and the write-up is one page or less.

**If time runs short, cut in this order:** Hypothesis → PATCH (seed a frozen card instead) → partial reversal → JSON formatter.
**Never cut:** concurrency test, idempotency, README, write-up.

**Day 3 is reserved for the video (~5 min) and the write-up:**
- about 30s on the problem
- about 3.5 min of demo
- about 1 min on trade-offs and AI usage

## 14. Known limits and next steps

- **Per-company hot row.** All authorizations for one company are serialized. Upgrade path: per-card locks plus a company reservation counter, or sharded balances.
- **Posted is a denormalized counter** (ADR 0004). It is safe while clearings are its only writer. Repayments would add a second writer, and that is the point to move to a ledger.
- **Model gaps:** no double-entry ledger, hold expiry, multi-clearing, force-post or late clearing after reversal, and no company timezone for the monthly window.
- **Production hardening:** per-user ops identity, stand-in reconciliation, Prometheus metrics.

## 15. Definition of done

- `docker compose up` followed by `make demo` and `make test` works on a clean machine with Docker only.
- Both guarantees (§2) are covered by tests that fail if the lock or the idempotency check is removed.
- README, WRITEUP.md (one page or less) and the 6 ADRs are present.
- Git history shows red/green commits per phase.

## 16. As-built notes

Places where the implementation differs from the original plan:

- **Policy rules** are a single ordered tuple of `(code, failed)` pairs, not one function per rule. It reads the same and is shorter.
- **SQL lives in `service.py`**, next to the transaction it belongs to. `db.py` holds only the pool. Splitting the SQL out would have separated each lock from the queries it protects.
- **`models.py` was added.** The pydantic request models are shared by the API, the service and the tests, and keeping them in `api.py` would have created an import cycle.
- **Reversal and clearing timeouts return 503 `system_error`** instead of a 200 decline, so the processor retries. The retry is safe because events are idempotent by `event_id`.
- **Property test** uses two cards that share one credit line, so either the card monthly limit or the company limit can be the one that binds. Its amount generator is biased towards exact limits and limit+1.
- **The `X-Process-Time` header** reports end-to-end handler time. The `duration_ms` field in the decision log reports service time.
- **Verified by mutation:**
  - Removing `FOR UPDATE` makes both burst tests fail.
  - Removing the UNIQUE-race handling makes the race tests fail.
  - A `>`→`>=` change on the monthly limit or the credit check is caught by the unit boundary tests and by the property test.
- **Added after the scope review (2026-11-20):**
  - **HMAC-SHA256 webhook signatures** (`signature.py`). The header is `X-Signature: t=<unix>,v1=<hex>` over `"<t>." + raw body`, with 300s tolerance and constant-time compare. The check runs as a router dependency before body validation, so an unsigned call gets 401, not 422.
  - **Typed response models** with documented error schemas in OpenAPI.
  - **Explicit integrity errors** replace `assert` in production code.
  - **Event replays compare the resolved amount.** An omitted reversal amount means "the whole remaining hold", so it matches an explicit amount that released everything.
  - **`make loadtest`** produces the measured numbers in README → Performance.
  - **CI** (`.github/workflows/ci.yml`) runs lint, test and the demo.
- **Added after the second review (2026-11-20):**
  - **Posted became a running counter on `companies`** (ADR 0004, amended). The loadtest `history` scenario (500k settled clearings) showed that derived balances fell to 59 req/s with 909/2000 `system_error`. After the change, the same scenario runs at about 310 req/s with zero errors. The property test reconciles the counter against `SUM(cleared_amount)` after every step.
  - **Concurrency tests for events and TOCTOU:**
    - 10 concurrent clearings settle once;
    - a reversal racing a clearing has exactly one winner;
    - a freeze committed while an authorization waits on the lock is honoured.
    Mutation-checked: removing `FOR UPDATE` or reading the card before the lock fails them.
  - **Bearer token on ops routes** (`OPS_TOKEN`, constant-time compare).
  - **Fail-fast config:** missing secrets stop the boot.
  - **`/health` checks the database** and returns 503 when it is unreachable.
