-- For banks fetched in date windows: how far back history has been fetched so far.
ALTER TABLE accounts ADD COLUMN history_from TEXT;
