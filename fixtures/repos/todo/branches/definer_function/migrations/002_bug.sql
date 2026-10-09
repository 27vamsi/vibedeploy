-- Planted bug: a SECURITY DEFINER function.
--
-- It runs as its owner, so row level security is evaluated for the owner and
-- not for the caller. Anyone allowed to call it reads the whole table through
-- it, and no policy on `todos` is broken in the process.

CREATE FUNCTION all_todos() RETURNS SETOF todos
    LANGUAGE sql STABLE SECURITY DEFINER
    AS $$ SELECT * FROM todos $$;
