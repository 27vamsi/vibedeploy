-- Per-app roles and schema. Readme.md section 9.1.
-- Run as platform admin. Every placeholder below is pre-quoted by kernel/render.py.

-- Database-level and idempotent: nothing ends up in public by accident.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Owns the tables. Cannot log in, so no credential for it exists anywhere.
CREATE ROLE {owner} NOLOGIN NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- Runs migrations. Never handed to the app.
CREATE ROLE {migrator} LOGIN PASSWORD {migrator_password} BYPASSRLS
    NOSUPERUSER NOCREATEDB NOCREATEROLE;

GRANT {owner} TO {migrator};

-- What the app connects as. The only credential the app ever sees.
CREATE ROLE {runtime} LOGIN PASSWORD {runtime_password} NOBYPASSRLS
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

CREATE SCHEMA {schema} AUTHORIZATION {owner};
REVOKE ALL ON SCHEMA {schema} FROM PUBLIC;
GRANT USAGE ON SCHEMA {schema} TO {runtime};

-- The migrator acts as the owner, so migrated objects are owned by the owner
-- role rather than by a login role.
ALTER ROLE {migrator} SET search_path = {schema};
ALTER ROLE {migrator} SET role = {owner};

-- Runtime never sees public, and never needs to qualify table names.
ALTER ROLE {runtime} SET search_path = {schema};
