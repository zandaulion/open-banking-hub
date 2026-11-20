-- What a transaction means for the owner's money, independent of its category:
-- spending | refund | income | other_in | one_off | internal
ALTER TABLE transactions_clean ADD COLUMN kind TEXT NOT NULL DEFAULT 'spending';
ALTER TABLE transactions_clean ADD COLUMN is_one_off INTEGER NOT NULL DEFAULT 0;
-- NULL: automatic (above the threshold), 1: always one-off, 0: never.
ALTER TABLE transaction_overrides ADD COLUMN one_off INTEGER;
-- Recurring series of money coming in (salary) as well as going out.
ALTER TABLE recurring_series ADD COLUMN direction TEXT NOT NULL DEFAULT 'out';
