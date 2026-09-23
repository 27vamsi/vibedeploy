# vibedeploy

`Readme.md` at the repo root is the source of truth for V0.5. Read it before writing code.
If something here or in the code conflicts with `Readme.md`, follow `Readme.md` and flag the conflict.

The product is two halves that share one enforcement mechanism:
1. **Deploy + prove** — derive who may see what, attack it, block the deploy on any failure.
2. **Agent gateway** — an AI agent gets exactly the access of the person it works for, never more.

Both are enforced by the same Postgres RLS policies. That is the whole point: the agent is not
a second security model, it is the same one under a different role.

## Non-negotiable rules (Readme.md section 3)

### Database
- Fail closed: no identity => zero rows, never "everything". Unsure, missing policy, audit write failing => deny.
- Postgres RLS decides access, not app code and not gateway code. `FORCE ROW LEVEL SECURITY` on every table.
- Four roles per app: `owner` (NOLOGIN), `migrator` (BYPASSRLS, never given to the app or the gateway),
  `runtime` (the app), `agent` (the gateway acting for a user). Runtime and agent are both
  NOBYPASSRLS, NOINHERIT and not owners.
- Identity is set only via `set_config('app.user_id', $1, true)` inside a transaction with bind
  params. Never plain `SET`, never string-formatted SQL.
- Policies come from the 5 templates only: `owner_column`, `fk_chain`, `shared`,
  `read_only_shared`, `admin_only`. No `USING (true)`, no freestyle SQL.
- Every policy covers SELECT, INSERT, UPDATE and DELETE, with both USING and WITH CHECK.
- Never grant TRUNCATE, REFERENCES or TRIGGER. No `ALTER DEFAULT PRIVILEGES`. Grants go last.

### Deriving and proving
- Never guess the access model. Ask the builder; no answer blocks launch.
- Detection and derivation are deterministic. No LLM decides anything security-related.
- Test expectations come from the seeding plan, never from reading the policies back.
- The attack suite runs as the runtime role **and** as the agent role, and must produce
  identical results. That is what proves an agent cannot exceed its person.
- Save the access model JSON on every run, with its verification results.

### Gateway
- Agents get narrow typed tools. Never credentials, never raw SQL, never shell.
- Forbidden tools are not exposed to the agent at all: not listed, not callable.
- Audit before action. The "about to execute" record is written before execution; if it cannot
  be written, do not execute.
- Approval is bound to the exact dry-run `diff_hash`. If what would happen changes, the
  approval is void and goes back to pending.
- An action executes at most once. Only the transitions in contract 7.6 are legal.

### Platform
- Untrusted app code (migrations) runs only in CodeBuild or a one-off ECS task, never in the
  worker or the control plane.
- The gateway may read only `vd/apps/*/agent-*` secrets. Never runtime, migrator or sidecar.
- Local first. Everything works locally before AWS.

## Working style

- One milestone at a time (M1..M14 in Readme.md section 8). Use that milestone's "Done when"
  as the acceptance criteria, and do not start the next one until it passes.
- For M1, M2, M5 and M11: write the failing test first.
- Never delete or skip a test to make CI green. Permanently protected: the pooling test, the
  planted-bug tests, the acts-for escape tests, the audit chain tamper test, the fail-closed tests.
- Contracts in Readme.md section 7 are frozen: identity header (7.1), access model JSON (7.2),
  policy templates (7.3), build-job callback (7.4), agent policy YAML (7.5), action record and
  its status machine (7.6), connector interface (7.7). Do not change them without asking.

## Stop and push back if asked to

An LLM making an allow/deny decision; a raw SQL or shell tool for agents; a plain `SET`;
`ALTER DEFAULT PRIVILEGES`; running migrations in the worker; giving the gateway runtime or
migrator secrets; UPDATE or DELETE on `audit_log`; deleting a test to make CI green.

## Local dev

- Run `.venv/Scripts/python.exe -m pytest -q` from the repo root (the venv is not auto-activated).
- Needs Docker Desktop: `docker compose -f infra/local/docker-compose.yml up -d postgres pgbouncer`.
  Postgres on `127.0.0.1:55432`, PgBouncer (transaction mode) on `127.0.0.1:56432`.
