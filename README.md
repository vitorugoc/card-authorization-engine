# Card Authorization Engine

This engine makes the real-time approve/decline decision for corporate card swipes and enforces spend policies.
It guarantees two things: **no overspend under concurrent authorizations**, and **exactly-once effects under processor retries**.

Read [`WRITEUP.md`](WRITEUP.md) for the problem and the trade-offs. Read [`docs/`](docs/) for the design.

## Run it

You only need Docker.

```bash
make up      # build + start Postgres 16 and the API on :8000
make demo    # reset demo data and run the narrated end-to-end demo
make test    # 59 tests against a real Postgres (separate test database)
make lint    # ruff + mypy --strict
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
| POST | `/v1/authorizations` | Processor webhook. Always HTTP 200 with `decision` approved/declined; 409 `idempotency_mismatch`; 422 invalid |
| POST | `/v1/authorizations/{ntid}/reversals` | `{event_id, amount?}` (no amount = full). 404 / 409 `authorization_not_pending` / 409 `amount_exceeds_hold` |
| POST | `/v1/authorizations/{ntid}/clearings` | `{event_id, amount}`. Final; may exceed the hold (tips) |
| GET | `/v1/companies/{id}` | `credit_limit`, `held`, `posted`, `available` |
| GET | `/v1/cards/{id}` | Card policy plus `monthly_spend` |
| GET | `/v1/cards/{id}/authorizations` | Newest first |
| PATCH | `/v1/cards/{id}` | `status`, `per_txn_limit`, `monthly_limit`, `blocked_mccs` |

- Amounts are integers in minor units (`12000` = 120.00).
- Example payloads are in [`requests.http`](requests.http). OpenAPI docs are at `http://localhost:8000/docs`.

Decline codes, in precedence order: `card_not_found`, `card_inactive`, `currency_mismatch`, `mcc_blocked`,
`exceeds_per_transaction_limit`, `exceeds_card_monthly_limit`, `insufficient_credit`, `system_error`.

## Layout

```
src/authz/
  policy.py    pure spend rules: no IO, unit-tested at every boundary
  service.py   transaction scripts: lock company → idempotency → evaluate → persist
  models.py    pydantic request models (trust boundary)
  api.py       FastAPI routes + status mapping
  db.py        connection pool
  logging.py   JSON log formatter
db/schema.sql  tables + CHECK constraints for the hold invariants
scripts/       seed.py, demo.py
tests/         policy (unit), authorize, idempotency, concurrency, lifecycle, api, ops, properties
docs/          sdd.md (spec), adr/ (6 decisions), grill-log.md (how the design was produced)
```

## Tests

- `test_concurrency.py` fires bursts released by a barrier and asserts exact approval counts. It fails if `FOR UPDATE` is removed (verified).
- `test_idempotency.py` includes a real cross-company race on the same transaction id, which is resolved by the UNIQUE constraint.
- `test_properties.py` runs Hypothesis model-based tests: random auth/reverse/clear sequences must match a reference model.

Stack: Python 3.12, FastAPI (sync), psycopg 3 with raw SQL, Postgres 16, uv, pytest, Hypothesis, ruff, mypy.
