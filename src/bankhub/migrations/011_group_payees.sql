-- Payee names the owner set for a group: payments to `payees` always belong to it, payments to `ignored`
-- never do (when automatic matching got it wrong). JSON lists of names, as written.
ALTER TABLE account_groups ADD COLUMN payees TEXT NOT NULL DEFAULT '[]';
ALTER TABLE account_groups ADD COLUMN ignored TEXT NOT NULL DEFAULT '[]';
