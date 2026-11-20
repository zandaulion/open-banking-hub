-- Amounts are stored as signed integers in minor units (cents/bani):
-- debits negative, credits positive.

CREATE TABLE consents (
    session_id   TEXT PRIMARY KEY,
    institution  TEXT NOT NULL,
    country      TEXT NOT NULL,
    valid_until  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active'   -- active | expired | revoked
);

CREATE TABLE accounts (
    id                INTEGER PRIMARY KEY,
    uid               TEXT NOT NULL UNIQUE,         -- Enable Banking uid; changes on re-consent
    session_id        TEXT NOT NULL REFERENCES consents(session_id),
    institution       TEXT NOT NULL,
    alias             TEXT NOT NULL UNIQUE,         -- e.g. revolut_ron, revolut_ron_savings
    currency          TEXT NOT NULL,
    cash_account_type TEXT,
    identity_hash     TEXT UNIQUE,                  -- stable across re-consents
    own_ibans         TEXT,                         -- JSON list, raw layer only
    backfilled_at     TEXT,
    active            INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE sync_runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    kind        TEXT NOT NULL,                      -- backfill | incremental
    status      TEXT NOT NULL DEFAULT 'running',    -- running | ok | error
    requests    INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

-- One row per data request to a bank; feeds the PSD2 unattended budget.
CREATE TABLE api_requests (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    account_id  INTEGER NOT NULL,
    kind        TEXT NOT NULL                       -- balances | transactions
);
CREATE INDEX api_requests_account_ts ON api_requests(account_id, ts);

CREATE TABLE balance_snapshots (
    id             INTEGER PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    fetched_at     TEXT NOT NULL,
    balance_type   TEXT NOT NULL,
    amount_minor   INTEGER NOT NULL,
    currency       TEXT NOT NULL,
    reference_date TEXT
);

CREATE TABLE transactions_raw (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id),
    dedup_key         TEXT NOT NULL,
    entry_reference   TEXT,
    status            TEXT NOT NULL,                -- BOOK | PDNG
    booking_date      TEXT,
    value_date        TEXT,
    amount_minor      INTEGER NOT NULL,
    currency          TEXT NOT NULL,
    counterparty_name TEXT,
    counterparty_iban TEXT,
    counterparty_has_account INTEGER NOT NULL DEFAULT 0,
    remittance        TEXT,
    payload           TEXT NOT NULL,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    UNIQUE (account_id, dedup_key)
);
CREATE INDEX transactions_raw_date ON transactions_raw(booking_date);

CREATE TABLE fx_rates (
    date     TEXT NOT NULL,
    currency TEXT NOT NULL,
    rate_ron REAL NOT NULL,                         -- RON per 1 unit
    PRIMARY KEY (date, currency)
);

CREATE TABLE transaction_overrides (
    raw_id               INTEGER PRIMARY KEY REFERENCES transactions_raw(id),
    merchant             TEXT,
    category             TEXT,
    subcategory          TEXT,
    is_internal_transfer INTEGER,
    updated_at           TEXT NOT NULL
);

CREATE TABLE transfer_pairs (
    debit_id   INTEGER NOT NULL REFERENCES transactions_raw(id),
    credit_id  INTEGER NOT NULL REFERENCES transactions_raw(id),
    method     TEXT NOT NULL,                       -- same_reference | amount_match | manual
    confidence REAL NOT NULL,
    confirmed  INTEGER NOT NULL DEFAULT 0,          -- 1 confirmed, -1 rejected
    PRIMARY KEY (debit_id, credit_id)
);

-- Everything below is derived and rebuilt by `bankhub rebuild`.
-- transactions_clean is the ONLY transaction source for reports and MCP.
CREATE TABLE transactions_clean (
    id                   INTEGER PRIMARY KEY,       -- = transactions_raw.id
    account_alias        TEXT NOT NULL,
    booking_date         TEXT NOT NULL,
    value_date           TEXT,
    amount               REAL NOT NULL,             -- signed, original currency
    currency             TEXT NOT NULL,
    amount_ron           REAL,                      -- signed, BNR rate of booking date
    direction            TEXT NOT NULL,             -- debit | credit
    status               TEXT NOT NULL,             -- booked | pending
    merchant             TEXT NOT NULL,             -- known merchant or generic label
    counterparty_type    TEXT NOT NULL,             -- merchant | own_account | individual | unknown_merchant | bank
    category             TEXT NOT NULL,
    subcategory          TEXT,
    is_internal_transfer INTEGER NOT NULL DEFAULT 0,
    is_recurring         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX transactions_clean_date ON transactions_clean(booking_date);

CREATE TABLE recurring_series (
    id                INTEGER PRIMARY KEY,
    merchant          TEXT NOT NULL,
    category          TEXT NOT NULL,
    cadence           TEXT NOT NULL,                -- weekly | monthly | annual
    currency          TEXT NOT NULL,
    occurrences       INTEGER NOT NULL,
    first_date        TEXT NOT NULL,
    last_date         TEXT NOT NULL,
    next_expected     TEXT NOT NULL,
    last_amount       REAL NOT NULL,
    previous_amount   REAL,
    price_change_pct  REAL,
    status            TEXT NOT NULL,                -- active | missed | ended
    monthly_ron       REAL,
    annual_ron        REAL
);
