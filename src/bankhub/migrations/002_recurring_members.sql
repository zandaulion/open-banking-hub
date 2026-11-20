-- Which transactions belong to which recurring series (derived, rebuilt with the ledger).
CREATE TABLE recurring_members (
    series_id INTEGER NOT NULL REFERENCES recurring_series(id) ON DELETE CASCADE,
    clean_id  INTEGER NOT NULL,
    PRIMARY KEY (series_id, clean_id)
);
