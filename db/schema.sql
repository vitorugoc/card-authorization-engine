CREATE TABLE companies (
  id            text PRIMARY KEY,
  name          text NOT NULL,
  currency      char(3) NOT NULL,
  credit_limit  bigint NOT NULL CHECK (credit_limit >= 0),
  -- Running total of settled spend, written only by clearings under this row's lock
  -- (ADR 0004). Summing history instead costs O(all clearings ever) per authorization.
  posted        bigint NOT NULL DEFAULT 0 CHECK (posted >= 0),
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
-- Holds are the small, live working set: the only thing still summed per decision.
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
