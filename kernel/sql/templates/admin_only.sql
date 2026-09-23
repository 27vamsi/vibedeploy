-- admin_only: nobody but an admin, in any direction. Readme.md section 9.3.
--
-- This is what an unclassified table becomes when the app holds sensitive data,
-- and what an audit table gets. `vd_role()` is NULL when no identity was set,
-- and NULL = 'admin' is NULL, not true, so a request with no identity is denied
-- by the same expression. Fail closed.

DROP POLICY IF EXISTS vd_sel ON {schema}.{table};
DROP POLICY IF EXISTS vd_ins ON {schema}.{table};
DROP POLICY IF EXISTS vd_upd ON {schema}.{table};
DROP POLICY IF EXISTS vd_del ON {schema}.{table};

CREATE POLICY vd_sel ON {schema}.{table} FOR SELECT TO {runtime}, {agent}
    USING ({schema}.vd_role() = 'admin');

CREATE POLICY vd_ins ON {schema}.{table} FOR INSERT TO {runtime}, {agent}
    WITH CHECK ({schema}.vd_role() = 'admin');

CREATE POLICY vd_upd ON {schema}.{table} FOR UPDATE TO {runtime}, {agent}
    USING ({schema}.vd_role() = 'admin')
    WITH CHECK ({schema}.vd_role() = 'admin');

CREATE POLICY vd_del ON {schema}.{table} FOR DELETE TO {runtime}, {agent}
    USING ({schema}.vd_role() = 'admin');
