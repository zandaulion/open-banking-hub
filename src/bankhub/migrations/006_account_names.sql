-- The owner's own name for an account, shown in their app only (agents keep the neutral alias).
ALTER TABLE accounts ADD COLUMN display_name TEXT;
