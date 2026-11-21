-- Accounts can belong to a group (a person or a purpose) with a behaviour:
--   supported: someone I support (a child, a parent). Their own payments are not counted; money I send
--              is spending in the group's category; money back is money in, or reduces that category.
--   separate:  kept apart (a business, an account managed for someone). Their payments are not counted;
--              money I send is a transfer (not counted) or spending.
-- Accounts without a group are mine.
CREATE TABLE account_groups (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    behaviour   TEXT NOT NULL CHECK (behaviour IN ('supported', 'separate')),
    category    TEXT NOT NULL DEFAULT 'family',
    subcategory TEXT DEFAULT 'support',
    money_back  TEXT NOT NULL DEFAULT 'money_in' CHECK (money_back IN ('money_in', 'reduce')),
    sent_as     TEXT NOT NULL DEFAULT 'spending' CHECK (sent_as IN ('spending', 'transfer'))
);
ALTER TABLE accounts ADD COLUMN group_id INTEGER REFERENCES account_groups(id) ON DELETE SET NULL;

-- Accounts marked as a child's before groups existed become one group each.
INSERT INTO account_groups (name, behaviour, category, subcategory)
    SELECT coalesce(display_name, alias), 'supported', 'kids', 'allowance' FROM accounts WHERE role = 'dependent';
UPDATE accounts SET group_id = (SELECT g.id FROM account_groups g WHERE g.name = coalesce(accounts.display_name, accounts.alias))
    WHERE role = 'dependent';

-- Which group a payment between my accounts and a group's accounts belongs to (owner-side detail).
CREATE TABLE transaction_groups (
    raw_id   INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL
);
