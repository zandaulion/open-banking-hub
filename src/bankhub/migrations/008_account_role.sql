-- 'own': counted in totals. 'dependent': a child's account (or one you watch for someone else): its
-- own payments are not in your totals, money you send it is spending, money from it is money in.
ALTER TABLE accounts ADD COLUMN role TEXT NOT NULL DEFAULT 'own';
