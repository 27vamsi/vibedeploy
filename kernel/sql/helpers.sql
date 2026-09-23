-- Identity helpers used by every policy. Readme.md section 9.2.
--
-- The empty-string gotcha: once a transaction-local setting ends,
-- current_setting('app.user_id', true) returns '' on that connection, not NULL,
-- and casting '' to uuid raises. NULLIF turns it back into NULL, and NULL never
-- equals an owner id, so the query returns nothing. Fail closed.
--
-- SECURITY INVOKER, so these carry no privilege of their own.

CREATE FUNCTION {schema}.vd_user_id() RETURNS {key_type}
    LANGUAGE sql STABLE SECURITY INVOKER
    AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::{key_type} $$;

CREATE FUNCTION {schema}.vd_role() RETURNS text
    LANGUAGE sql STABLE SECURITY INVOKER
    AS $$ SELECT NULLIF(current_setting('app.role', true), '') $$;

ALTER FUNCTION {schema}.vd_user_id() OWNER TO {owner};
ALTER FUNCTION {schema}.vd_role()    OWNER TO {owner};

REVOKE ALL ON FUNCTION {schema}.vd_user_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION {schema}.vd_role()    FROM PUBLIC;

GRANT EXECUTE ON FUNCTION {schema}.vd_user_id(), {schema}.vd_role()
    TO {runtime}, {agent};
