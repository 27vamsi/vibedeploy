-- Per-app roles and schema. Readme.md section 9.1.
-- Run as platform admin. Every placeholder is pre-quoted by kernel/render.py.

-- Database-level and idempotent: nothing ends up in public by accident.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Owns the tables. Cannot log in, so no credential for it exists anywhere.
CREATE ROLE {owner} NOLOGIN NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- Runs migrations, and only migrations. FORCE filters the owner, so a data
-- backfill would otherwise touch zero rows; BYPASSRLS is why it works. Safe
-- only because these credentials never reach the app or the gateway.
CREATE ROLE {migrator} LOGIN PASSWORD {migrator_password} BYPASSRLS
    NOSUPERUSER NOCREATEDB NOCREATEROLE;

GRANT {owner} TO {migrator};

-- What the app connects as. The only credential the app ever sees.
CREATE ROLE {runtime} LOGIN PASSWORD {runtime_password} NOBYPASSRLS
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

-- What the gateway connects as when acting for a person. Separate from the
-- runtime so agent traffic is distinguishable in Postgres logs, can be cut off
-- without touching the app, and carries its own timeout.
CREATE ROLE {agent} LOGIN PASSWORD {agent_password} NOBYPASSRLS
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

CREATE SCHEMA {schema} AUTHORIZATION {owner};
REVOKE ALL ON SCHEMA {schema} FROM PUBLIC;

-- USAGE only, never CREATE: neither the app nor the gateway can add a table
-- that has no policies on it.
GRANT USAGE ON SCHEMA {schema} TO {runtime}, {agent};

-- Readme.md section 9.1 also has `ALTER ROLE {migrator} SET role = {owner}` so
-- that migrated objects come out owned by the owner. That cannot be combined
-- with the BYPASSRLS above: once the session has SET ROLE to the owner, the
-- effective role is NOBYPASSRLS, FORCE applies to it, and the data backfills
-- that BYPASSRLS exists for touch zero rows.
--
-- So the migrator stays itself while migrating, and ownership is handed to the
-- owner afterwards with REASSIGN OWNED (see reassign_objects_to_owner). Same
-- end state, and backfills actually work. Membership of the owner role is what
-- lets it ALTER owner-owned tables on later deploys.
ALTER ROLE {migrator} SET search_path = {schema};

-- Neither of these ever sees public, or needs to qualify a table name.
ALTER ROLE {runtime} SET search_path = {schema};
ALTER ROLE {agent}   SET search_path = {schema};

-- An agent is driven by a model and will happily ask for something enormous.
ALTER ROLE {agent} SET statement_timeout = '5s';
