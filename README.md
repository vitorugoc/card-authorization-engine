# Card Authorization Engine

This engine makes the real-time approve/decline decision for corporate card swipes and enforces spend policies.
It guarantees two things: **no overspend under concurrent authorizations**, and **exactly-once effects under processor retries**.

Read [`WRITEUP.md`](WRITEUP.md) for the problem and the trade-offs. Read [`docs/`](docs/) for the design.

## Run it

You only need Docker.

```bash
make up      # build + start Postgres 16 and the API on :8000
make demo    # reset demo data and run the narrated end-to-end demo
make test    # 92 tests against a real Postgres (separate test database)
make lint    # ruff + mypy --strict (src, tests, scripts)
make loadtest  # throughput: one hot company, 16 companies, hot + 500k history (wipes demo data)
```

- If port 8000 is taken, run `API_PORT=8080 make up`. The demo runs inside the compose network, so it is unaffected.
- `make demo-live` runs the same demo but waits for Enter between steps (useful for presenting).
- `make reset` recreates the database from `db/schema.sql`.

## What the demo shows

| Step | What happens | Why it matters |
|------|--------------|----------------|
| 1 | A 120.00 MXN lunch is approved, billed by the network for a 7.00 USD merchant charge | Cross-border: policy runs on the billing amount |
| 2 | A 600.00 casino charge is declined with `mcc_blocked` + `exceeds_per_transaction_limit` | Every failing reason is returned; one primary network code |
| 3 | The same `network_transaction_id` is retried → identical response; a tampered retry → 409 | Idempotency |
| 4 | 20 concurrent 100.00 auths with 880.00 left on the card → exactly 8 approved | Row lock: no overspend |
| 5 | A partial reversal of 20.00 | The hold shrinks |
| 6 | A clearing at 138.00 (tip added) | Posted in full, remaining hold released |
| 7 | The card is frozen via the API, and the next swipe declines `card_inactive` | Live policy change under the same lock |

## API

| Method | Path | Notes |
|--------|------|-------|
| POST | `/v1/authorizations` | Processor webhook (HMAC-signed). Always HTTP 200 with `decision` approved/declined; 409 `idempotency_mismatch`; 422 invalid |
| POST | `/v1/authorizations/{ntid}/reversals` | `{event_id, amount?}` (no amount = full). 404 / 409 `authorization_not_pending` / 409 `amount_exceeds_hold` |
| POST | `/v1/authorizations/{ntid}/clearings` | `{event_id, amount}`. Final; may exceed the hold (tips) |
| GET | `/v1/companies/{id}` | `credit_limit`, `held`, `posted`, `available` (ops) |
| GET | `/v1/cards/{id}` | Card policy plus `monthly_spend` (ops) |
| GET | `/v1/cards/{id}/authorizations` | Newest first (ops) |
| PATCH | `/v1/cards/{id}` | `status`, `per_txn_limit`, `monthly_limit`, `blocked_mccs` (ops) |
| GET | `/health` | `SELECT 1` through the pool; 503 if the database is unreachable |

- Amounts are integers in minor units (`12000` = 120.00).
- **The three processor routes require an `X-Signature: t=<unix>,v1=<hmac-sha256 hex>` header.** The HMAC is computed over `"<t>." + raw body` using `WEBHOOK_SECRET`, with a 5-minute tolerance. A missing or invalid signature gets 401, before the body is validated.
- **Ops routes require `Authorization: Bearer <OPS_TOKEN>`** (`ops_local_demo_only` in `docker-compose.yml`), otherwise they return 401 `unauthorized`.
- The app refuses to boot if `WEBHOOK_SECRET`, `OPS_TOKEN` or `DATABASE_URL` is missing.
- `requests.http` is unsigned, so use it for the ops routes (it carries the bearer token); `scripts/demo.py` shows how to sign.
- Example payloads are in [`requests.http`](requests.http). OpenAPI docs, with typed response and error schemas, are at `http://localhost:8000/docs`.

Decline codes, in precedence order: `card_not_found`, `card_inactive`, `currency_mismatch`, `mcc_blocked`,
`exceeds_per_transaction_limit`, `exceeds_card_monthly_limit`, `insufficient_credit`, `system_error`.

## Performance

`make loadtest` sends 2,000 signed authorizations per scenario. It was run on an Apple M3 Pro with Docker, a single uvicorn process, and client and server on the same machine.

| Scenario | Concurrent clients | Throughput | Server p99 | `system_error` |
|----------|-------------------:|-----------:|-----------:|---------------:|
| hot: 1 company | 16 | ~300 req/s | ~45 ms | 0 |
| hot: 1 company | 64 | ~380 req/s | ~170 ms | 0 |
| spread: 16 companies | 64 | ~750 req/s | ~80 ms | 0 |
| history: hot + 500k settled clearings | 16 | ~310 req/s | ~45 ms | 0 |
| history, **before** the posted counter | 16 | 59 req/s | ~280 ms | **909 / 2000** |

- **One company is serialized by its row lock** ([ADR 0002](docs/adr/0002-per-company-row-lock.md)). Each authorization holds the lock for about 3 ms, so more clients mostly add queueing time. Spread across companies, the locks rarely collide and throughput grows with concurrency.
- **The history scenario changed a design decision.** At first, balances were derived by summing every past clearing under the lock. With a year of history, that sum took about 27 ms, and authorizations started failing closed on the 200 ms lock timeout. Posted spend is now a running total, updated in the same transaction and under the same lock, and reconciled by the property test ([ADR 0004](docs/adr/0004-derived-balances-no-ledger.md)).

## Layout

```
src/authz/
  domain.py      shared vocabulary: StrEnums (statuses, decline codes) + business errors. No IO, no HTTP
  policy.py      pure spend rules: no IO, unit-tested at every boundary
  service.py     use cases as transaction scripts: lock company → idempotency → evaluate → persist
  idempotency.py exactly-once mechanism: request hash, replay matchers, UNIQUE-race resolution
  models.py      pydantic request models (trust boundary) and typed response models (use-case output)
  api.py         FastAPI: lifespan, routers + auth dependencies, the only DomainError → HTTP mapping
  config.py      Settings, validated at boot and injected as a dependency
  db.py          connection pool, opened/closed by the lifespan
  signature.py   HMAC webhook signing/verification
  logging.py     JSON log formatter
db/schema.sql  tables + CHECK constraints for the hold invariants
scripts/       seed.py, demo.py, loadtest.py
tests/         policy + signature (unit), authorize, idempotency, concurrency, lifecycle, api, ops, properties
docs/          sdd.md (spec), adr/ (6 decisions), grill-log.md (how the design was produced)
```

## Tests

- `test_concurrency.py` fires bursts released by a barrier and asserts exact approval counts. It also races clearings against each other and against a reversal, and commits a card freeze while an authorization waits on the lock. These tests fail if `FOR UPDATE` is removed or the card is read before the lock (verified).
- `test_idempotency.py` includes a real cross-company race on the same transaction id, which is resolved by the UNIQUE constraint.
- `test_properties.py` runs Hypothesis model-based tests: random auth/reverse/clear sequences must match a reference model.

**Dependencies point inwards:** `api` → `service` → `idempotency`/`db` → `policy`/`domain`. The domain and policy layers import nothing from HTTP or the database. There are no repository or DI-container layers on purpose. The SQL sits next to the transaction it protects, so the `FOR UPDATE` that carries the correctness guarantee is visible in the code under review.

Stack: Python 3.12, FastAPI (sync), psycopg 3 with raw SQL, Postgres 16, uv, pytest, Hypothesis, ruff, mypy --strict (src, tests and scripts).
