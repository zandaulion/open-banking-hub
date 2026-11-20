-- The bank's own transaction type (e.g. ING: "POS purchase", "Credit Repayment"), raw layer only.
ALTER TABLE transactions_raw ADD COLUMN bank_code TEXT;
