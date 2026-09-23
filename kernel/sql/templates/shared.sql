-- shared: everyone logged in sees and changes everything. Section 9.3.
--
-- `vd_user_id() IS NOT NULL` rather than `true`: a request that arrived without
-- a verified identity still gets nothing. That is the difference between "open
-- to the team" and "open to the internet", and it is why no template anywhere
-- in the kernel contains a literal true.
--
-- No {admin} suffix: a member already sees everything, so there is nothing for
-- an admin bypass to add.

DROP POLICY IF EXISTS vd_sel ON {schema}.{table};
DROP POLICY IF EXISTS vd_ins ON {schema}.{table};
DROP POLICY IF EXISTS vd_upd ON {schema}.{table};
DROP POLICY IF EXISTS vd_del ON {schema}.{table};

CREATE POLICY vd_sel ON {schema}.{table} FOR SELECT TO {runtime}, {agent}
    USING ({schema}.vd_user_id() IS NOT NULL);

CREATE POLICY vd_ins ON {schema}.{table} FOR INSERT TO {runtime}, {agent}
    WITH CHECK ({schema}.vd_user_id() IS NOT NULL);

CREATE POLICY vd_upd ON {schema}.{table} FOR UPDATE TO {runtime}, {agent}
    USING ({schema}.vd_user_id() IS NOT NULL)
    WITH CHECK ({schema}.vd_user_id() IS NOT NULL);

CREATE POLICY vd_del ON {schema}.{table} FOR DELETE TO {runtime}, {agent}
    USING ({schema}.vd_user_id() IS NOT NULL);
