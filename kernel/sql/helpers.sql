-- Identity helpers used by every policy. Readme.md section 9.2.
--
-- The empty-string gotcha: once a transaction-local setting ends,
-- current_setting('app.user_id', true) returns '' on that connection, not NULL,
-- and casting '' to uuid raises. NULLIF turns it back into NULL, and NULL never
-- equals an owner id, so the query returns nothing. Fail closed.

CREATE FUNCTION {schema}.vd_user_id() RETURNS {key_type}
    LANGUAGE sql STABLE SECURITY INVOKER
    AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::{key_type} $$;

CREATE FUNCTION {schema}.vd_role() RETURNS text
    LANGUAGE sql STABLE SECURITY INVOKER
    AS $$ SELECT NULLIF(current_setting('app.role', true), '') $$;

REVOKE ALL ON FUNCTION {schema}.vd_user_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION {schema}.vd_role() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {schema}.vd_user_id(), {schema}.vd_role() TO {runtime};
