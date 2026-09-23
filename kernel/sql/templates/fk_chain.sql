-- fk_chain: the row's owner is reached by following foreign keys. Section 9.3.
--
-- The same EXISTS expression guards all four commands, as USING and as WITH
-- CHECK, so a row can never be created or moved into a chain that does not lead
-- back to the person doing it. The expression itself is built by
-- `kernel.render.fk_chain_expression` from the derived path, with every
-- identifier quoted and every column of a composite key joined on.

DROP POLICY IF EXISTS vd_sel ON {schema}.{table};
DROP POLICY IF EXISTS vd_ins ON {schema}.{table};
DROP POLICY IF EXISTS vd_upd ON {schema}.{table};
DROP POLICY IF EXISTS vd_del ON {schema}.{table};

CREATE POLICY vd_sel ON {schema}.{table} FOR SELECT TO {runtime}, {agent}
    USING ({expression});

CREATE POLICY vd_ins ON {schema}.{table} FOR INSERT TO {runtime}, {agent}
    WITH CHECK ({expression});

CREATE POLICY vd_upd ON {schema}.{table} FOR UPDATE TO {runtime}, {agent}
    USING ({expression})
    WITH CHECK ({expression});

CREATE POLICY vd_del ON {schema}.{table} FOR DELETE TO {runtime}, {agent}
    USING ({expression});
