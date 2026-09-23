-- owner_column: the row carries the owner's id directly. Readme.md section 9.3.
--
-- Four policies, one per command. UPDATE needs both USING (which rows may be
-- touched) and WITH CHECK (what they may become); without the latter a user
-- could hand their row to someone else. INSERT is WITH CHECK only, which is
-- what stops a row being created already owned by another user.
--
-- Both the runtime and the agent role are named on every policy, so an agent
-- acting for a person is filtered by exactly the same expression as the app.

DROP POLICY IF EXISTS vd_sel ON {schema}.{table};
DROP POLICY IF EXISTS vd_ins ON {schema}.{table};
DROP POLICY IF EXISTS vd_upd ON {schema}.{table};
DROP POLICY IF EXISTS vd_del ON {schema}.{table};

CREATE POLICY vd_sel ON {schema}.{table} FOR SELECT TO {runtime}, {agent}
    USING ({column} = {schema}.vd_user_id(){admin});

CREATE POLICY vd_ins ON {schema}.{table} FOR INSERT TO {runtime}, {agent}
    WITH CHECK ({column} = {schema}.vd_user_id(){admin});

CREATE POLICY vd_upd ON {schema}.{table} FOR UPDATE TO {runtime}, {agent}
    USING ({column} = {schema}.vd_user_id(){admin})
    WITH CHECK ({column} = {schema}.vd_user_id(){admin});

CREATE POLICY vd_del ON {schema}.{table} FOR DELETE TO {runtime}, {agent}
    USING ({column} = {schema}.vd_user_id(){admin});
