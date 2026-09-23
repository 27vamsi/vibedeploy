-- The schema that must make derivation stop and ask rather than guess.
-- Readme.md section 3 rule 5, section 11.
--
-- Every table here is a different reason to refuse:
--   accounts / profiles  two principal table candidates, so ask which
--   projects             two columns could be the owner, so ask which
--   project_members      membership deciding access: refuse, not supported
--   tasks                nullable FK on the path, so those rows are admin only
--   settings             reachable from nothing, so ask what it is
--   legacy_notes         composite FK, supported but must join on both columns
--   reports              a view, and one without security_invoker

CREATE TABLE accounts (
    id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email text NOT NULL UNIQUE
);

CREATE TABLE profiles (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name text NOT NULL
);

CREATE TABLE projects (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id   uuid NOT NULL REFERENCES accounts(id),
    created_by uuid NOT NULL REFERENCES accounts(id),
    name       text NOT NULL
);

CREATE TABLE project_members (
    project_id uuid NOT NULL REFERENCES projects(id),
    account_id uuid NOT NULL REFERENCES accounts(id),
    role       text NOT NULL DEFAULT 'member',
    PRIMARY KEY (project_id, account_id)
);

CREATE TABLE tasks (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  uuid NOT NULL REFERENCES projects(id),
    assignee_id uuid REFERENCES accounts(id),
    title       text NOT NULL
);

-- Linked to nobody.
CREATE TABLE settings (
    key   text PRIMARY KEY,
    value text NOT NULL
);

CREATE TABLE legacy_docs (
    tenant_id uuid NOT NULL,
    doc_no    integer NOT NULL,
    title     text NOT NULL,
    PRIMARY KEY (tenant_id, doc_no)
);

CREATE TABLE legacy_notes (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    doc_no    integer NOT NULL,
    body      text NOT NULL,
    FOREIGN KEY (tenant_id, doc_no) REFERENCES legacy_docs (tenant_id, doc_no)
);

-- A view with no security_invoker runs as its owner and skips the caller's
-- policies entirely. Readme.md section 25.
CREATE VIEW reports AS
    SELECT p.id AS project_id, p.name, count(t.id) AS task_count
    FROM projects p LEFT JOIN tasks t ON t.project_id = p.id
    GROUP BY p.id, p.name;

CREATE VIEW safe_reports WITH (security_invoker = true) AS
    SELECT id, name FROM projects;

-- SECURITY DEFINER runs as the definer, so it is a hole unless it is ours.
CREATE FUNCTION bump_task_count(p uuid) RETURNS integer
    LANGUAGE sql SECURITY DEFINER
    AS $$ SELECT count(*)::integer FROM tasks WHERE project_id = p $$;
