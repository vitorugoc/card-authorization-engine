# Grill Log — Design Decisions

How this design was produced: a "grilling" session (design tree explored in rounds of questions).
Round 0 was answered by the candidate. Later rounds were answered by a separate AI agent prompted as a
Staff fintech backend engineer, then reviewed and approved/vetoed by the candidate at a checkpoint.

## Round 0 — Candidate decisions

| # | Question | Decision |
|---|----------|----------|
| 0.1 | Stack | Python |
| 0.2 | Problem | Real-time corporate card authorization engine with spend policies (limits, MCC, velocity, company funds), holds, idempotency, concurrency safety |
| 0.3 | Lifecycle scope | Authorization + reversal + simple clearing. Out: disputes, incremental auth, real FX |
| 0.4 | Who answers later rounds | Senior AI agent; candidate reviews summary before SDD |
| 0.5 | SDD format | `docs/sdd.md` + `docs/adr/`, vertical phases, each phase test-first with green acceptance test |

### Candidate problems considered (Round 0 critique)

- **A. Multi-currency double-entry ledger** — deep but generic; weak demo.
- **B. Card authorization engine** — chosen: core of Jeeves' business, visual demo (approve/decline with reason, concurrent burst does not overspend, retries are idempotent).
- **C. Settlement reconciliation** — real ops pain; demo depends on fake files, hard to impress in 5 min.
- **D. LLM receipt matching** — mostly an LLM wrapper; little backend depth for a senior backend role.

## Round 1 — Answered by senior agent

| # | Question | Decision | Rejected | Defend |
|---|----------|----------|----------|--------|
| Q1 | Funding model | Credit line per company: `available = credit_limit - posted - held`. Repayments out | Prefunded (weaker Jeeves fit, needs top-up flow); both behind abstraction (speculative) | Jeeves sells both; formula is the only difference and lives in one function |
| Q2 | Policy rules v1 | 5 rules: card active, MCC blocklist, per-txn max, per-card monthly limit (holds + cleared, UTC month), company available credit. Evaluate all, return `reasons[]` + one primary `decline_code` by fixed precedence (status > MCC > per-txn > card limit > funds) | Velocity (same signal as period limit), merchant country (same shape as MCC, first "next"), short-circuit (loses explainability) | UTC month vs company timezone — known simplification |
| Q3 | Money & currency | Integer minor units (BIGINT); single currency per company; auth carries billing `amount/currency` (network-converted) + `merchant_amount/currency` (display). Billing currency != account currency → decline `currency_mismatch` | Decimal (no benefit, inputs already minor units); decline foreign spend (kills cross-border); FX in engine (network's job) | — |
| Q4 | External interface | `POST /v1/authorizations` (decline = HTTP 200; unknown card = 200 decline; malformed = 422); `POST .../{ntid}/reversals` (full or partial); `POST .../{ntid}/clearings` (single, final, may differ from auth); ops: seed + GET company/card/auths + `PATCH /v1/cards/{id}` (live freeze) | Single typed webhook (dispatcher, no signal); full admin CRUD; multi-clearing/force-post | HMAC webhook signature named as production must, out for local |
| Q5 | Stack | FastAPI sync, psycopg3 raw SQL + psycopg_pool, Postgres 16, `schema.sql` via initdb, pytest on compose Postgres, `docker compose up` + `make demo` / `make test` | SQLite (no row locks — concurrency claim would be fake), Django, SQLAlchemy ORM (hides locking), Alembic, testcontainers, async | Sync over async: bottleneck is the row lock, not the event loop |
| Q6 | Idempotency | Key = `network_transaction_id` UNIQUE (and `event_id` for reversal/clearing); store `request_hash` + response. Same id+hash → stored response, never re-evaluate. Same id, different hash → 409 `idempotency_mismatch`. Declines persisted too | Header-only (processors don't send it); both keys; re-evaluate on replay (can flip decision) | 409 vs original response: payload change is a processor contract violation; fail loud |
| Q7 | Architecture | 4 modules, no interfaces: `policy.py` (pure rules), `service.py` (transaction scripts), `db.py` (SQL), `api.py` (routes). Lock company account row `SELECT ... FOR UPDATE`; totals by SUM under lock, no counters | Hexagonal/repository ABCs, rule registry/strategy, SERIALIZABLE+retry, optimistic version, single conditional UPDATE | Per-company lock ceiling (upgrade: per-card locks + reservation counter / sharded balances); no double-entry ledger in v1 |
| Q8 | Demo | `make demo` runs `demo.py` (httpx, narrated, balances per step) + `requests.http`. Steps: approve → multi-reason decline → replay + tampered 409 → 20-way burst (exactly 8 approve, invariant holds) → partial reversal → clearing with tip → live freeze → `pytest -q` | Dashboard UI; `--no-lock` flag; Postman | — |
| Q9 | Latency & failure | `SET LOCAL lock_timeout='200ms'`, `statement_timeout='1s'`; timeout/DB error → 200 decline `system_error` (fail closed); timing middleware + `X-Process-Time`. Stand-in = processor config, documented only | Stand-in in engine; circuit breakers; no timeouts | Fail closed vs open is a business decision configured at the processor |

## Round 2 — Answered by senior agent

| # | Question | Decision | Rejected | Defend |
|---|----------|----------|----------|--------|
| Q10 | Schema | 4 tables: `companies` (account + lock target), `cards` (status, per_txn_limit, monthly_limit, blocked_mccs[]), `authorizations` (UNIQUE ntid, status declined/pending/reversed/cleared, held/cleared amounts with CHECK invariants, policy_snapshot, request_hash, response), `authorization_events` (append-only, UNIQUE event_id, reversal/clearing). Prefixed text IDs (`card_…`), fixed seed IDs | Bare UUIDs, bigserial, state from amounts only, cardholder entity, separate reversal/clearing tables | — |
| Q11 | Lifecycle edges | Over-clear posts full amount (available may go negative). Under-clear releases remainder. Reversal/clearing on non-pending → 409 `authorization_not_pending`; reversal > hold → 409 `amount_exceeds_hold`; unknown auth → 404. Hold expiry out. Previous-month auth clearing counts in its own month | Cap at auth amount; tolerance at clearing; `expires_at` | Policy enforced at authorization, clearing records reality. Late clearing after reversal rejected in v1 (prod: force-post + alert) |
| Q12 | Monthly window | Card spend = SUM(held + cleared) of auths with `created_at` in current UTC month | Clearing date for posted (spend hops months) | Issuers differ |
| Q13 | Lock ordering | BEGIN + timeouts → read card.company_id (unlocked, immutable) → `FOR UPDATE` company → idempotency lookup → re-read card → SUMs → evaluate → INSERT → COMMIT. Every money/policy write (PATCH card, reversal, clearing) locks company first. Single lock target ⇒ no deadlocks | Idempotency fast path before lock; card row lock; single pre-lock card read (TOCTOU) | — |
| Q14 | Tests | Concurrency: `service.authorize` via ThreadPoolExecutor + Barrier. HTTP: TestClient for mapping/422/409. Isolation: TRUNCATE on separate `authz_test` DB. One Hypothesis property test (stretch). Coverage reported, no gate | Real HTTP concurrency in tests, rollback-per-test, schema-per-test, coverage gate | "Concurrency test is the money test" |
| Q15 | Tooling | Python 3.12, uv, ruff, mypy --strict on src, single-stage Dockerfile, Makefile via `docker compose run`. Layout `src/authz/{api,service,policy,db,logging}.py`, `scripts/{seed,demo}.py`, `db/schema.sql` | poetry, pip, pyright, multi-stage, pydantic-settings, CI | — |
| Q16 | Observability | Stdlib JSON formatter, one line per business event (`authorization.decided`, `reversal.applied`, `clearing.posted`, `idempotency.mismatch`, `authorization.system_error`). DB rows = audit trail. No metrics | structlog, Prometheus, separate audit table | — |
| Q17 | Security | Strict pydantic at boundary (StrictInt amount, regex currency/mcc/country, length caps), `extra="ignore"` on processor payloads, `extra="forbid"` on PATCH, parameterized SQL, no PAN/CVV, holder_name not logged. Out (named): HMAC, ops auth, TLS, rate limiting, secrets | Static API key | Unauthenticated PATCH: first thing to lock down |
| Q18 | Phases | P0 skeleton 1.5h · P1 approve path 2h · P2 policy 2.5h · P3 idempotency 1.5h · P4 concurrency 2h · P5 reversal+clearing 3h · **cut line** · P6 ops surface 1.5h · P7 polish 2h. Day 3 = video + write-up | — | — |
| Q19 | Docs | 6 ADRs (funding, row lock, idempotency, derived balances/no ledger, decline semantics, money). 1-page WRITEUP.md; README = how to run only | ADR per library | — |

## Round 3 — Candidate checkpoint (2026-11-20)

- Q20: Candidate approved all Round 1–2 decisions, including the 9 [DEFEND] points, plus two orchestrator additions:
  1. Cross-company `network_transaction_id` race: catch `UniqueViolation`, re-run idempotency lookup, replay or 409 (own test).
  2. Red/green commit per phase as TDD evidence.
- Q21: Local `git init` now; candidate pushes to a public GitHub repo at the end.

Frontier empty. Output: [`sdd.md`](sdd.md) and [`adr/`](adr/).
