-- read_only_shared: reference data. Everyone logged in reads it, admins change
-- it. Readme.md section 9.3.
--
-- The write policies still exist and still name both app roles. Leaving INSERT,
-- UPDATE and DELETE without a policy would also deny them, but silently: there
-- would be nothing for the structural check "policies for all four commands" to
-- find, and nothing to read back when explaining the rules to the builder.

DROP POLICY IF EXISTS vd_sel ON {schema}.{table};
DROP POLICY IF EXISTS vd_ins ON {schema}.{table};
DROP POLICY IF EXISTS vd_upd ON {schema}.{table};
DROP POLICY IF EXISTS vd_del ON {schema}.{table};

CREATE POLICY vd_sel ON {schema}.{table} FOR SELECT TO {runtime}, {agent}
    USING ({schema}.vd_user_id() IS NOT NULL);

CREATE POLICY vd_ins ON {schema}.{table} FOR INSERT TO {runtime}, {agent}
    WITH CHECK ({schema}.vd_role() = 'admin');

CREATE POLICY vd_upd ON {schema}.{table} FOR UPDATE TO {runtime}, {agent}
    USING ({schema}.vd_role() = 'admin')
    WITH CHECK ({schema}.vd_role() = 'admin');

CREATE POLICY vd_del ON {schema}.{table} FOR DELETE TO {runtime}, {agent}
    USING ({schema}.vd_role() = 'admin');
