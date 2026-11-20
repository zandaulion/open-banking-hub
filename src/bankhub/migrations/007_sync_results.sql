-- What each sync did per account, for the health view and alerts.
CREATE TABLE sync_results (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES sync_runs(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    at         TEXT NOT NULL,
    fetched    INTEGER NOT NULL DEFAULT 0,
    new        INTEGER NOT NULL DEFAULT 0,
    skipped    TEXT
);
CREATE INDEX sync_results_account ON sync_results(account_id, at);
