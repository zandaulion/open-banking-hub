-- Credit cards that banks do not share over open banking are added from the monthly statement (PDF).
-- 'bank' accounts sync from the bank; 'statement' accounts only change when a statement is added.
ALTER TABLE accounts ADD COLUMN source TEXT NOT NULL DEFAULT 'bank';

-- One row per statement added: what the bank says is owed, the limit, and when payment is due.
CREATE TABLE card_statements (
    id                  INTEGER PRIMARY KEY,
    account_id          INTEGER NOT NULL REFERENCES accounts(id),
    period_start        TEXT NOT NULL,
    period_end          TEXT NOT NULL,
    owed_minor          INTEGER,
    limit_minor         INTEGER,
    available_minor     INTEGER,
    total_due_minor     INTEGER,
    min_due_minor       INTEGER,
    due_date            TEXT,
    installments_due_minor    INTEGER,   -- instalments falling due with this statement
    installments_future_minor INTEGER,   -- instalments still to come after it
    imported_at         TEXT NOT NULL,
    UNIQUE (account_id, period_end)
);

-- Interest-free instalment plans listed on a statement (owner-only: merchant names).
CREATE TABLE card_plans (
    id              INTEGER PRIMARY KEY,
    statement_id    INTEGER NOT NULL REFERENCES card_statements(id) ON DELETE CASCADE,
    merchant        TEXT,
    started         TEXT,
    initial_minor   INTEGER,
    installment_minor INTEGER,
    number          TEXT,             -- "5/6"
    remaining_minor INTEGER
);
