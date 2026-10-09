-- Planted bug: a view without `security_invoker`.
--
-- By default a view reads its tables as the view's owner, so the policies on
-- `todos` are checked against the owner rather than the person asking. The
-- table is protected; the view beside it is not.

CREATE VIEW everyones_todos AS SELECT id, owner_id, title, done FROM todos;
