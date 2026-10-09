-- Planted bug: TRUNCATE, to PUBLIC.
--
-- TRUNCATE ignores row level security entirely. A policy that carefully lets
-- somebody delete only their own rows is worth nothing next to a grant that
-- lets them empty the table.

GRANT TRUNCATE ON todos TO PUBLIC;
