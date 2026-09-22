-- The row belongs to a user directly, through one column. Readme.md section 9.3.
--
-- All four commands are covered. WITH CHECK on INSERT stops a user creating a
-- row owned by someone else; WITH CHECK on UPDATE stops them handing one over.
-- There is no USING (true) here: even with admin enabled, an unidentified
-- session matches nothing.

CREATE POLICY vd_sel ON {schema}.{table} FOR SELECT TO {runtime}
    USING ({column} = {schema}.vd_user_id(){admin});

CREATE POLICY vd_ins ON {schema}.{table} FOR INSERT TO {runtime}
    WITH CHECK ({column} = {schema}.vd_user_id(){admin});

CREATE POLICY vd_upd ON {schema}.{table} FOR UPDATE TO {runtime}
    USING ({column} = {schema}.vd_user_id(){admin})
    WITH CHECK ({column} = {schema}.vd_user_id(){admin});

CREATE POLICY vd_del ON {schema}.{table} FOR DELETE TO {runtime}
    USING ({column} = {schema}.vd_user_id(){admin});
