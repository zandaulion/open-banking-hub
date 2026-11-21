-- The column mapping the owner confirmed for an account's CSV statements, so the next file needs no setup.
CREATE TABLE statement_formats (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    mapping    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
