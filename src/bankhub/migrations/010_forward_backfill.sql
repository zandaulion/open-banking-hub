-- Banks fetched in date windows fill in older history going forward, in requests whose progress is kept:
-- everything from backfill_from up to (not including) backfill_until has been fetched.
-- history_from..recent_until is the continuous recent part; the backfill is done when it reaches history_from.
ALTER TABLE accounts ADD COLUMN backfill_from TEXT;
ALTER TABLE accounts ADD COLUMN backfill_until TEXT;
ALTER TABLE accounts ADD COLUMN recent_until TEXT;
ALTER TABLE accounts ADD COLUMN recent_cursor TEXT;   -- progress of a recent part not finished yet
