# vibedeploy V0.5

**Build with AI. Deploy with proof. Let AI run it safely.**

> A deploy platform on AWS that proves, with automated attacks on every push, that no user can see another user's data. When you add an AI agent to production, it goes through our gateway and gets exactly the access of the person it works for, never more. Every agent action is checked, approved if risky, reversible where possible, and logged.

Read this whole file before writing code. It is the source of truth.
If something here conflicts with what seems easier, follow this file and flag the conflict. Do not silently change it.

---

## Table of contents

1. What we are building
2. The two flows
3. Rules for whoever writes the code
4. Scope
5. Tech stack
6. Repo layout
7. Contracts
8. Build order
9. SQL kernel
10. The questions
11. Deriving the rules
12. Fake data
13. The attack
14. The identity library (shim)
15. Detection and GitHub
16. Login gate (sidecar)
17. AWS base infrastructure
18. Per-app AWS resources and IAM
19. The agent gateway
20. Gateway connectors
21. MCP server
22. Control plane data model
23. Dashboard
24. Testing rules
25. Edge cases and loopholes
26. Demo script
27. Cost
28. What we promise, and what we don't
29. Working with an AI assistant on this repo

---

## 1. What we are building

Three steps for the user:

1. **Vibecode** an app with any AI tool.
2. **Vibedeploy** it through us. We work out who should see what, prove it with attacks, and put it live on AWS.
3. **Add an AI agent** to production. It connects to our gateway, not to your systems. It gets only the access of the person it works for.

### The claims

- Even if the app's code runs `SELECT * FROM invoices` with no filter, a logged-in user only gets their own rows. The database enforces it. We prove it before every deploy by attacking it.
- An AI agent working for Alice can never see or change more data than Alice can. Same rules, same enforcement, same proof.
- Every agent action has an identity, a policy decision with a reason, a dry run, an approval if risky, a check after it runs, an undo where possible, and an audit record that can't be quietly edited.

### Threat model

We protect:
- Users of an app from each other, even when the app's code is sloppy.
- Apps from other apps on the platform.
- Production from AI agents doing more than they're allowed to.
- Customer secrets from AI agents and from our own platform code.

We do NOT protect:
- Users from the app's own builder (the builder owns the data).
- Against a compromised AWS account or a Postgres bug.
- Against a person approving a bad action. Approvals move responsibility to a human, they don't make a bad action good.

---

## 2. The two flows

### Regular flow (always on)

1. Builder connects GitHub, picks a repo, pastes secrets into the **secrets vault**.
2. We read the app's tables by running its migrations in a throwaway database.
3. We ask **4 simple questions** plus follow-ups for unclear tables.
4. We show the rules in plain English. Builder confirms.
5. Fake users **attack** the rules. Report: "212/212 passed". **Any failure blocks the deploy.**
6. App goes live on `<app>.apps.<domain>` on AWS Fargate, isolated from other apps.
7. Failed deploy: **automatic rollback** to the last good version.
8. Builder adds teammates. They log in through our **login gate**.
9. Each person sees and changes only what the rules allow, **enforced in the database**.
10. Every push: everything is **re-attacked before going live**.

### Gateway mode (when an AI agent is added)

11. Builder clicks **Add agent** and links it to a person: "this agent works for Alice".
12. Agent gets its own **identity** and **exactly Alice's access, never more**.
13. Builder sets a **policy**: automatic / needs approval / forbidden, per tool, plus **limits**.
14. Agent connects to our **gateway** (MCP or REST). It never gets passwords, database URLs or AWS keys. It gets **short-lived, task-scoped sessions**.
15. **Reads**: allowed, but only Alice's data comes back.
16. **Writes and ops actions**: **dry run**, **before/after diff**, then run automatically or wait for a **human approval** click.
17. After running: **verify** it worked, **undo** if not.
18. Always on: **rate limits**, **fail closed**, **reason for every decision**.
19. Builder sees the **agent dashboard**, **tamper-evident audit log**, and **evidence report**.
20. Emergency: **kill switch** and **one-click revoke**.

---

## 3. Rules for whoever writes the code (human or AI)

Each rule exists because breaking it creates a silent security hole.

1. **Fail closed.** No known identity = zero rows. Gateway unsure, policy missing, audit write failing = action denied.
2. **The database decides who sees what.** Postgres RLS policies, not app code, not gateway code.
3. **Roles per app:** owner (owns tables, cannot log in), migrator (runs migrations, never given to the app or gateway), runtime (the app), agent (the gateway acting for users). Runtime and agent are NOBYPASSRLS and not owners. `FORCE ROW LEVEL SECURITY` on every table.
4. **Set identity only with `set_config('app.user_id', $1, true)` inside a transaction.** Never plain `SET`. Never string-built SQL.
5. **Never guess the rules silently.** Unclear: ask. No answer: block.
6. **Policies come from templates.** No freestyle SQL.
7. **Deterministic detection and decisions.** No LLM decides anything security-related. The gateway's allow/deny is code and config, never a model's opinion.
8. **Save the access model JSON on every run**, with verification results.
9. **Every policy covers SELECT, INSERT, UPDATE, DELETE, with USING and WITH CHECK.**
10. **Test answers come from the seeding plan, not from reading policies.**
11. **Untrusted code never runs on platform machines.** Migrations only run in an isolated build job or a one-off app task.
12. **Agents get tools, never credentials and never raw SQL or shell.** SQL filters get bypassed (see Postgres MCP Pro restricted-mode CVE, 2026). Narrow typed tools don't.
13. **Forbidden tools are not shown to the agent at all.** Not listed, not callable.
14. **Audit before action.** The audit record for "about to execute" must be written before execution. If it can't be written, don't execute.
15. **Approval is bound to the exact dry-run result.** If what would happen changes, the approval is void.
16. **Local first.** Everything works locally before AWS.

---

## 4. Scope

### In V0.5

**Deploy**
- GitHub App connect, any stack (buildpacks)
- Secrets vault (env vars pasted once, stored in Secrets Manager)
- Live HTTPS subdomain on AWS Fargate
- Per-app IAM roles with a permissions boundary
- Login gate, builder adds users (email + password)
- Rollback on failed deploy
- Re-check on every push

**Prove**
- Schema read by running migrations in a throwaway database
- 4 questions + follow-ups, plain-English confirmation
- RLS from templates, role setup, FORCE
- Fake users attack, report, deploy blocked on failure
- Protection guarantee for Python + SQLAlchemy apps; other stacks deploy marked **Unprotected**

**Agent gateway**
- Agent identity, linked to a person (acts-for)
- API key → short-lived, task-scoped session
- Policy per agent: auto / approve / forbidden, limits
- Risk score per action
- Dry run + before/after diff
- Human approval in dashboard, bound to the diff
- Transactional execution, verify after, undo where possible
- Rate limits, fail closed, reason for every decision
- Tamper-evident audit log (hash chain)
- Agent dashboard + evidence report
- Kill switch (agent, app, global) + one-click revoke
- Connectors: **Postgres** (the USP) and **AWS** (ops actions)
- **MCP server** so Claude Code / Cursor plug in directly, plus a REST API

### Not in V0.5 (README "next" list)
Node/Prisma protection. GitHub, Stripe, internal API connectors (interface + stubs only). Shadow mode. Loop detection. Anomaly detection. Canary execution. Approval escalation (two approvers). Spend limits. Policy-as-code in Git. SSO. Email invites. Access expiry for humans. Backups/restore UI. Export. Custom domains. BYOC. Keep-warm. Websockets through the gate.

### Unsupported (say so in the UI)
- Browser-direct database apps (Supabase anon key, frontend-only Lovable/Bolt apps)
- Non-Postgres databases
- Apps with no users table
- Membership-based sharing (`project_members` deciding access)
- Migration tools other than Alembic and plain SQL folders

---

## 5. Tech stack

| Part | Choice | Why |
|---|---|---|
| Control plane | Python 3.12, FastAPI, Jinja2 + HTMX, Pydantic v2 | One codebase, no JS build |
| Control plane DB | Postgres 16, SQLAlchemy 2, Alembic | Standard |
| Job queue | `jobs` table, `SELECT ... FOR UPDATE SKIP LOCKED` | No Redis/SQS |
| Kernel, verifier, provisioner | asyncpg + raw SQL | Exact control near roles/policies |
| Fake data | Faker | |
| Tests | pytest, pytest-asyncio | |
| Local builds | `pack` + Docker | Any stack |
| Cloud builds + scratch verification | AWS CodeBuild (privileged, no VPC) | Isolates untrusted code |
| Registry | ECR, one repo per app | Per-app IAM scoping |
| Runtime | ECS Fargate | One small VM per task |
| Login gate | Starlette + httpx sidecar per app | App only reachable through it |
| Routing + TLS | Local: Caddy `*.localhost`. AWS: ALB + ACM wildcard + Route 53 | |
| Customer DB | RDS Postgres 16, schema per app | |
| Base AWS | CDK (Python) | One-time setup |
| Per-app AWS | boto3 | Created at deploy |
| Gateway | FastAPI service + `mcp` Python SDK (Streamable HTTP) | Same language as everything else |
| Agent AWS access | STS AssumeRole with session policy, 15 min | Short-lived, scoped per call |

---

## 6. Repo layout

```
vibedeploy/
  README.md
  control_plane/
    app/
      models.py
      api/                     public API, internal API (sidecar login, build callback)
      web/                     Jinja + HTMX dashboard
    migrations/
  worker/                      orchestrates, provisions. Never runs app code.
    pipeline/  clone.py detect.py provision.py deploy.py
    runtimes/  local.py aws.py
  buildjob/                    runs INSIDE the isolated build
    run.py migrate_scratch.py introspect.py derive.py policies.py
    seed.py attack.py inject_shim.py
  kernel/
    sql/  roles.sql helpers.sql templates/*.sql
    messages.py                fixed plain-English messages
  sidecar/                     login gate
  shims/python/
  gateway/
    app.py                     REST + MCP entry
    auth.py                    agent keys, sessions
    pipeline.py                identity -> policy -> risk -> dry run -> approval -> execute -> verify -> undo -> audit
    policy.py                  policy loading + evaluation
    risk.py
    audit.py                   hash-chained log
    ratelimit.py
    killswitch.py
    connectors/
      base.py                  Connector interface
      postgres.py              USP
      aws.py
      github.py                stub
      stripe.py                stub
      http_api.py              stub
    mcp_server.py
  infra/
    local/  docker-compose.yml Caddyfile
    aws/    CDK app
  tests/
    kernel/ concurrency/ derive/ attack/ shims/
    gateway/                   policy, approvals, undo, audit chain, kill switch, acts-for escape attempts
  fixtures/
    schemas/  flat_todo.sql crm_chain.sql messy.sql
    repos/    fastapi_sqlalchemy_crm/  planted_bugs/
    agents/   sample policies, a scripted "malicious agent" test client
```

---

## 7. Contracts (freeze on day one)

### 7.1 Identity header (sidecar → app)

`X-VD-Identity: base64url(payload) + "." + base64url(HMAC_SHA256(payload, IDENTITY_KEY))`

```json
{"v":1,"app":"app_123","sub":"<principal key>","role":"member","iat":1726900000,"exp":1726900060}
```
- Unknown `v` rejected. `exp` = 60s. Per-app key. Sidecar strips any incoming copy. Shim verifies with constant-time compare. Missing or bad = no identity = zero rows.

### 7.2 Access model JSON

```json
{
  "version": 1,
  "app_id": "app_123",
  "principal": {"table": "users", "key": "id", "key_type": "uuid"},
  "answers": {"audience": "team", "size": "small", "visibility": "own_data", "sensitive": true},
  "tables": {
    "users":     {"template": "owner_column", "column": "id"},
    "customers": {"template": "owner_column", "column": "rep_id"},
    "orders":    {"template": "fk_chain", "path": [
                   {"from":"orders","column":"customer_id","to":"customers","to_column":"id"},
                   {"from":"customers","column":"rep_id","to":"users","to_column":"id"}]},
    "plans":     {"template": "read_only_shared"},
    "audit_log": {"template": "admin_only"}
  },
  "admin_enabled": true,
  "explanation": ["A rep can see a customer if they are that customer's rep.", "..."],
  "confirmed_by": "builder@example.com",
  "confirmed_at": "2026-09-21T10:00:00Z",
  "schema_hash": "sha256:..."
}
```

### 7.3 Templates
`owner_column`, `fk_chain`, `shared`, `read_only_shared`, `admin_only`. SQL in 9.3.

### 7.4 Build job result
```json
{"deployment_id":"...","status":"passed|blocked|error","schema_hash":"sha256:...",
 "access_model":{...},"verification":{"total":212,"passed":212,"failures":[]},
 "image_digest":"sha256:...","shim":{"language":"python","db_lib":"sqlalchemy","active":true}}
```
Authenticated with a one-time job token.

### 7.5 Agent policy (stored versioned per agent)

```yaml
agent: support-bot
acts_for: alice@acme.com
session_ttl_minutes: 30
rate_limit_per_minute: 60
postgres:
  default: {read: auto, create: forbid, update: forbid, delete: forbid}
  tables:
    invoices: {read: auto, create: approve, update: auto, delete: approve}
    notes:    {read: auto, create: auto,    update: auto, delete: approve}
  limits: {max_rows_read: 200, max_rows_changed: 50}
aws:
  app_status: auto
  read_logs: auto
  restart_app: {mode: auto, max_per_hour: 3}
  rollback_deploy: approve
```

Rules:
- A policy can only **narrow** access. It can never grant beyond what the acts-for person has. The database enforces this anyway.
- Anything not listed is `forbid`.
- `forbid` tools are not exposed to the agent.
- AWS tools are only allowed if the acts-for person is an **app admin**.
- Every edit creates a new version with author and timestamp. Actions record which version decided them.

### 7.6 Action record (gateway)

```json
{
  "action_id": "act_01J...",
  "agent_id": "agt_...", "acts_for": "usr_...", "app_id": "app_123",
  "connector": "postgres", "tool": "db_update",
  "args": {"table":"invoices","filters":{"id":"..."},"values":{"status":"paid"}},
  "policy_version": 7, "decision": "approve_required",
  "reason": "update on invoices changes 3 rows; policy: update=auto but limit check passed; risk=medium",
  "risk": "medium",
  "dry_run": {"affected": 3, "diff_hash": "sha256:...", "diff": [...]},
  "status": "pending_approval",
  "approved_by": null, "approved_at": null,
  "result": null, "undo": {"possible": true, "expires_at": "..."}
}
```

Status machine:
`received → denied`
`received → dry_run_done → (auto) executing | pending_approval`
`pending_approval → approved → executing | rejected | expired`
`executing → verified | failed → rolled_back | undo_failed`
`verified → undone` (only via explicit undo)

No other transitions. An action executes at most once.

### 7.7 Connector interface

```python
class Connector(Protocol):
    name: str
    def tools(self, policy) -> list[ToolSpec]: ...             # only non-forbidden tools
    def risk(self, tool, args) -> Risk: ...
    async def scoped_credentials(self, session) -> Creds: ...   # short-lived, this task only
    async def dry_run(self, creds, tool, args) -> DryRun: ...   # affected, diff, diff_hash
    async def execute(self, creds, tool, args, expected: DryRun) -> Result: ...
    async def verify(self, creds, tool, args, result) -> bool: ...
    async def undo(self, creds, action) -> UndoResult: ...      # or UndoResult.impossible
```

---

## 8. Build order (~12 days)

Each milestone ends with a check. Don't start the next until it passes.

| Days | Milestone |
|---|---|
| 1 | M1 kernel |
| 2 | M2 shim + pooling test |
| 3 | M3 schema read, M4 derive |
| 4 | M5 seed + attack + report |
| 5 | M6 local build/run + login gate |
| 6 | M7 local end to end |
| 7 | M8 AWS base, M9 per-app AWS + rollback + push re-check |
| 8 | M10 gateway core + audit + kill switch |
| 9 | M11 Postgres connector (the USP) |
| 10 | M12 approvals, verify, undo, evidence report |
| 11 | M13 AWS connector + MCP server |
| 12 | M14 demo, README polish, blog post |

If short on time, cut in this order: AWS connector scale/rollback tools (keep status + logs), undo for deletes, evidence report styling. **Never cut** the attack tests, the pooling test, or the gateway acts-for escape tests.

### M1. Enforcement kernel
Build `infra/local/docker-compose.yml` (Postgres 16, PgBouncer transaction mode, Caddy), `kernel/sql/*`, `tests/kernel/`.

Done when:
- Runtime with A set sees only A's rows. No user set: zero rows.
- Runtime can't insert as B (error), can't reassign its row to B (error), update/delete of B's rows affects 0.
- `SET ROLE` owner with FORCE: filtered. FORCE off: sees all (proves FORCE matters).
- Throwaway BYPASSRLS role sees all (proves NOBYPASSRLS matters).
- TRUNCATE denied.
- After a transaction that set the user ends, the next one on the same connection sees zero rows.

### M2. Python shim + pooling test
500 concurrent requests, 5 users, through PgBouncer transaction mode. Each response only its own user's rows.
Done when: 10 runs in a row pass. Negative test with session-level `set_config(..., false)` catches leaks.

### M3. Schema read
Scratch Postgres inside the build job, migrations as migrator (Alembic / SQL folder), introspect `pg_catalog`: tables, columns, types, nullability, defaults, PKs, unique/check, enums, FKs, views + `security_invoker`, functions + `prosecdef`, existing policies. Output graph + `schema_hash`.
Done when: fixtures match hand-written expected graphs.

### M4. Questions + derivation
Done when: `flat_todo` and `crm_chain` match expected models; `messy` stops and asks the right questions; every table has a plain-English line.

### M5. Seed + attack
Done when: clean fixtures pass all checks, and each planted bug is caught: `USING (true)`, missing WITH CHECK on INSERT, missing WITH CHECK on UPDATE, SELECT-only policy, FORCE off, runtime owns a table, runtime has BYPASSRLS, view without `security_invoker`, SECURITY DEFINER function, new table with RLS off, TRUNCATE granted.

### M6. Local build/run + login gate
Build job in a container with its own Postgres. `pack build` + shim layer. Sidecar and app share a network namespace. Caddy routes `<app>.localhost`.
Done when: Alice and Bob in two browsers each see only their own invoices from an unfiltered endpoint. Forged `X-VD-Identity` from outside does nothing.

### M7. Local end to end
Worker pipeline with pause for questions. Done when: each planted-bug branch blocked with a plain reason, clean branch live.

### M8. AWS base (CDK)
Done when: hello-world task reachable at `https://hello.apps.<domain>`.

### M9. Per-app AWS + rollback + push re-check
Done when: the fixture repo behaves on AWS as it did locally; a broken deploy rolls back automatically; pushing an unprotected table blocks and the old version keeps running.

### M10. Gateway core
Agent create/revoke, keys (hashed), sessions (30 min), policy load + versioning, risk scoring, rate limit, kill switch, hash-chained audit, fail-closed checks.
Done when:
- Disabled agent, expired session, missing policy, audit DB down: all denied.
- Audit chain verifier detects an edited row.
- Kill switch takes effect on the next call (under 1 second).

### M11. Postgres connector (the USP)
Done when these **acts-for escape tests** pass (agent acting for A):
- `db_query invoices` with no filters returns only A's invoices.
- `db_query` with a filter for B's invoice id returns nothing.
- `db_update` / `db_delete` targeting B's rows: 0 affected, action reported as "no rows you can access".
- `db_create` a row owned by B: denied by the database, reported with reason.
- `db_update` reassigning A's row to B: denied.
- Unknown table or column name: denied before touching the database.
- Table/column names with quotes, semicolons, SQL keywords: denied by the allowlist.
- `max_rows_changed` exceeded: denied at dry run.

### M12. Approvals, verify, undo, evidence
Done when:
- Approve-required action waits; approving runs it exactly once; approving twice does nothing.
- If data changed between dry run and approval (diff hash differs), execution stops and asks for re-approval.
- Approval expires after 15 minutes.
- Verify catches a mismatch and triggers undo.
- Undo restores before-values, only if rows still match the after-values.
- Evidence report numbers match the audit log exactly.

### M13. AWS connector + MCP
Done when:
- Agent can read status/logs of its app only; asking for another app's logs is denied by AWS itself (session policy), not just by our code.
- `restart_app` respects `max_per_hour`.
- `rollback_deploy` waits for approval, then verifies health.
- Claude Code connects via MCP config, sees only allowed tools, and completes the demo script.

### M14. Demo
Section 26.

---

## 9. SQL kernel

All identifiers via `format('%I', ...)` or safe quoting. Never string formatting.

### 9.1 Roles per app

```sql
-- once per database, as platform admin
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- per app
CREATE ROLE app_123_owner    NOLOGIN NOBYPASSRLS;
CREATE ROLE app_123_migrator LOGIN PASSWORD '<random>' BYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT app_123_owner TO app_123_migrator;
CREATE ROLE app_123_runtime  LOGIN PASSWORD '<random>' NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE app_123_agent    LOGIN PASSWORD '<random>' NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

CREATE SCHEMA app_123 AUTHORIZATION app_123_owner;
REVOKE ALL ON SCHEMA app_123 FROM PUBLIC;
GRANT USAGE ON SCHEMA app_123 TO app_123_runtime, app_123_agent;

ALTER ROLE app_123_migrator SET search_path = app_123;
ALTER ROLE app_123_migrator SET role = app_123_owner;
ALTER ROLE app_123_runtime  SET search_path = app_123;
ALTER ROLE app_123_agent    SET search_path = app_123;
ALTER ROLE app_123_agent    SET statement_timeout = '5s';
```

Why a separate agent role: agent traffic is separable in Postgres logs, can be cut off without touching the app, and gets its own timeouts.

Why the migrator has BYPASSRLS: with FORCE on, the owner is filtered too, so data backfills in migrations would touch zero rows. Safe because the app and gateway never get migrator credentials.

**RDS gotcha:** confirm the RDS admin can create a BYPASSRLS role. If not: `NO FORCE` all tables before migrations, migrate, `FORCE` again. Pick one in M1.

After every migration, in this order:
```sql
-- 1. every table
ALTER TABLE app_123.<t> ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_123.<t> FORCE  ROW LEVEL SECURITY;
-- 2. drop old vd_ policies, create new ones from templates
-- 3. grants last
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app_123 TO app_123_runtime, app_123_agent;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app_123 TO app_123_runtime, app_123_agent;
GRANT EXECUTE ON FUNCTION app_123.vd_user_id(), app_123.vd_role() TO app_123_runtime, app_123_agent;
```
Never grant TRUNCATE, REFERENCES, TRIGGER. No `ALTER DEFAULT PRIVILEGES`.

### 9.2 Helpers

```sql
CREATE FUNCTION app_123.vd_user_id() RETURNS uuid
LANGUAGE sql STABLE SECURITY INVOKER
AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;

CREATE FUNCTION app_123.vd_role() RETURNS text
LANGUAGE sql STABLE SECURITY INVOKER
AS $$ SELECT NULLIF(current_setting('app.role', true), '') $$;
```

**Empty-string gotcha:** after a transaction-local setting ends, `current_setting(..., true)` returns `''`, not NULL, on that connection. `NULLIF` makes it NULL, NULL never matches an owner. Fail closed.

### 9.3 Templates

`{admin}` = `OR vd_role() = 'admin'` when admin enabled.

**owner_column**
```sql
CREATE POLICY vd_sel ON {t} FOR SELECT TO {runtime},{agent} USING ({c} = vd_user_id() {admin});
CREATE POLICY vd_ins ON {t} FOR INSERT TO {runtime},{agent} WITH CHECK ({c} = vd_user_id() {admin});
CREATE POLICY vd_upd ON {t} FOR UPDATE TO {runtime},{agent}
  USING ({c} = vd_user_id() {admin}) WITH CHECK ({c} = vd_user_id() {admin});
CREATE POLICY vd_del ON {t} FOR DELETE TO {runtime},{agent} USING ({c} = vd_user_id() {admin});
```

**fk_chain** (e.g. invoices → orders → customers → users), same expression for all commands, USING and WITH CHECK:
```sql
EXISTS (SELECT 1 FROM orders o JOIN customers c ON c.id = o.customer_id
        WHERE o.id = invoices.order_id AND c.rep_id = vd_user_id()) {admin}
```
Max path length 4. Index every path column.

**shared**: all commands `vd_user_id() IS NOT NULL`
**read_only_shared**: SELECT `vd_user_id() IS NOT NULL`, writes `vd_role() = 'admin'`
**admin_only**: all commands `vd_role() = 'admin'`

No literal `true` anywhere. Unclassified table = RLS on, no policies = nothing visible, reported.

---

## 10. The questions

Always:
1. **Who will use this app?** Just me / My team / My customers
2. **About how many people?** 1-20 / 20-200 / 200+ (stored only in V0.5)
3. **Same data for everyone, or each person only their own?** Everyone / Own only / Choose per table
4. **Is any of this data private or sensitive?** Yes / No

Follow-ups only when needed:
- "Which table holds the people who log in?"
- "Who owns a row in `projects`: `owner_id` or `created_by`?"
- "`settings` isn't linked to any user. Everyone logged in, everyone reads but admins change, or admins only?"
- "Rows in `tasks` with no `assignee_id` will be admin-only. OK?"
- "Can people see other people's rows in `users` (like names)?"

If Q4 is Yes, unclear tables default to `admin_only`. Answers are reused; only new or changed tables trigger new questions.

---

## 11. Deriving the rules

1. **Principal table:** `users`, `user`, `accounts`, `profiles`, `members` with a single-column PK. One: propose. Zero or several: ask. None: refuse.
2. **Graph:** child → parent along FKs. Ignore self-references.
3. **"Everyone sees everything":** all tables `shared`.
4. **Per table:**
   - Principal table: `owner_column` on its own PK.
   - Find simple paths to the principal table, max length 4.
   - 0 paths: ask. 1 path length 1: `owner_column`. Length 2+: `fk_chain`. 2+ paths: ask.
   - Junction table deciding access: refuse ("membership access not supported yet").
   - Nullable FK on the path: those rows admin-only, stated in confirmation.
   - Composite FKs: join on all columns.
5. **Explain** each table in one sentence.
6. **Confirm**, store with answers, schema hash, timestamp.

---

## 12. Fake data

In the scratch DB as migrator.

1. Personas A (member), B (member), ADMIN, and NONE (no identity, attacks only).
2. Insert in FK order. Nullable cycles: insert NULL then update. Non-nullable cycles: deferrable constraints or report and block.
3. For every owned table: at least 2 rows reachable from A and 2 from B, each with its own full parent chain.
4. Values respect type, length, enums, arrays, json, defaults, unique (counter), check constraints (retry 20x then report the column).
5. Record ownership of every row **from the insert plan**. That's the answer key.

---

## 13. The attack

As runtime, each check in a rolled-back transaction. For every protected table and persona:

| Check | Pass means |
|---|---|
| read_all | Exactly the rows P owns (ADMIN all if enabled, NONE empty) |
| read_other | Empty |
| update_other | 0 rows |
| delete_other | 0 rows |
| insert_as_other | Error |
| reassign | Error |
| no_identity | Empty / 0 / error |

Structural checks (counted):
- RLS enabled + FORCE on every table
- Policies for all four commands
- No literal `true` expression
- Runtime and agent roles: not owner, not member of owner, not BYPASSRLS, not superuser
- No TRUNCATE / REFERENCES / TRIGGER for runtime or agent
- Every view has `security_invoker = true`
- No SECURITY DEFINER functions except ours
- No unclassified tables

Each failure maps to a fixed message in `kernel/messages.py`, e.g. "Anyone logged in can read every row in `invoices`."

The same attack suite runs as the **agent** role too. It must produce identical results. This is what proves agents can't exceed their person.

---

## 14. The identity library (Python shim)

1. Read and verify `X-VD-Identity` per request, store in a `ContextVar`, reset after.
2. At every transaction start: `SELECT set_config('app.user_id', $1, true), set_config('app.role', $2, true)`. No identity: both `''`.
3. Log once at startup: `vibedeploy-shim active lang=python db=sqlalchemy framework=fastapi`.

- Middleware auto-install: patch `Starlette.__init__` (FastAPI/Starlette) or `Flask.__init__`.
- DB hook: `sqlalchemy.event.listen(Engine, "begin", on_begin)` at class level (covers async engines too).
- Delivery: extra image layer with the package + `sitecustomize.py` on `PYTHONPATH`.
- Worker checks the startup line. Missing = Unprotected = deploy blocked if protection was expected.

Known gaps: autocommit apps see no data (fail closed, warned). Raw psycopg/asyncpg = Unprotected. Background jobs have no identity = see nothing (warned). Apps with their own login = double login.

---

## 15. Detection and GitHub

| Signal | Means |
|---|---|
| `requirements.txt` / `pyproject.toml` / `Pipfile` | Python |
| `package.json` | Node (deploys, Unprotected) |
| `sqlalchemy` in deps | SQLAlchemy |
| `fastapi` / `flask` / `starlette` | framework |
| `alembic.ini` + `alembic/` | Alembic |
| `migrations/*.sql` / `db/migrations/*.sql` | SQL folder |
| `DATABASE_URL` read in code | needs Postgres |
| `.python-version` | version |

GitHub App: Contents read, Metadata read, push webhook. Short-lived installation token per clone. Clone in the build job only.

---

## 16. Login gate (sidecar)

- Listens on 8080; app on `127.0.0.1:3000` (`PORT=3000`). Security group only lets the ALB reach 8080.
- Owns `/__vd/login`, `/__vd/logout`, `/__vd/health`.
- Login: POST to control plane `/internal/apps/{id}/login` with `SIDECAR_API_KEY`. argon2 check. Returns `sub`, `role`, `session_version`.
- Cookie: host-only, HttpOnly, Secure, SameSite=Lax, signed with per-app `SESSION_KEY`, 8h.
- Each request: verify cookie, strip incoming `X-VD-Identity`, strip our cookie from `Cookie`, add signed header, stream-proxy to the app.
- Removed users: `session_version` bump, checked with 60s cache.
- Login rate limit per IP and per email.
- Adding a user inserts a row into the app's users table (as migrator) and stores `principal_key`.

---

## 17. AWS base infrastructure (CDK, one time)

One AWS account in V0.5. Code treats control and workload as separate so splitting into two accounts later is config.

- **VPC**, 2 AZs. Public subnets: ALB, app tasks, control plane, worker, gateway. Isolated subnets: RDS. **No NAT gateway** (tasks get public IPs for outbound; inbound blocked by security groups).
- **Security groups:** `alb-sg` 443 in. `apps-sg` 8080 from ALB. `gateway-sg` 443 from ALB. `rds-sg` 5432 from `apps-sg`, `worker-sg`, `gateway-sg`.
- **ALB:** HTTPS with ACM wildcard `*.apps.<domain>`, HTTP→HTTPS, default 404.
- **Route 53:** `*.apps.<domain>` → ALB.
- **RDS Postgres 16** `db.t4g.micro`, isolated, encrypted, admin secret `vd/platform/rds-admin` (worker only).
- **ECS cluster:** control plane (`console.apps.<domain>`), worker, gateway (`gateway.apps.<domain>`).
- **CodeBuild:** privileged, **no VPC**, own throwaway Postgres, reports via one-time token.
- **Platform roles:** control plane, worker (deployer), gateway, CodeBuild, `vd-agent-ops` (assumed by gateway), and the `vd-app-boundary` managed policy.
- **Budget alerts** at $50 and $100.

---

## 18. Per-app AWS resources and IAM

### 18.1 Resources per app

1. ECR repo `vd/apps/app_123`
2. Secrets:
   - `vd/apps/app_123/runtime` (app container): runtime `DATABASE_URL`, `VD_IDENTITY_KEY`, builder env vars
   - `vd/apps/app_123/sidecar`: `SESSION_KEY`, `VD_IDENTITY_KEY`, `SIDECAR_API_KEY`
   - `vd/apps/app_123/migrator`: migration task only
   - `vd/apps/app_123/agent`: agent role `DATABASE_URL`, gateway only
3. Log group `/vd/apps/app_123`, 14 days
4. IAM roles under `/vd/apps/`, all with `vd-app-boundary`:
   - `vd-app-app_123-exec`: pull own repo + sidecar repo, write own logs, read own `runtime` + `sidecar` secrets
   - `vd-app-app_123-task`: **no permissions**
   - `vd-app-app_123-migrate-exec`: like exec but reads `migrator` instead of `runtime`
5. Target group (`ip`, 8080, `/__vd/health`, 30s deregistration)
6. Listener rule for `app_123.apps.<domain>`
7. Task definition: sidecar + app (pinned by digest), 0.25 vCPU / 0.5 GB, read-only root FS + `/tmp` volume, init process, non-root if possible
8. Migration task definition
9. ECS service: 1 task, min 100% / max 200%, **deployment circuit breaker with rollback on** (this is the automatic rollback)

### 18.2 Deploy order

1. CodeBuild passes, image pushed, digest + schema hash returned
2. Worker creates schema + roles if new
3. Worker runs the one-off migration task, waits
4. Worker enables RLS + FORCE, writes policies, grants
5. Worker introspects real schema; hash must match verified hash, else block
6. Structural checks on the real schema
7. New task def revision, update service, wait for steady state
8. Mark live (or circuit breaker rolls back → mark failed)

### 18.3 IAM policies

**`vd-app-boundary`** (max any app role can ever have):
```json
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["ecr:GetAuthorizationToken"],"Resource":"*"},
 {"Effect":"Allow","Action":["ecr:BatchGetImage","ecr:GetDownloadUrlForLayer","ecr:BatchCheckLayerAvailability"],
  "Resource":["arn:aws:ecr:REGION:ACCT:repository/vd/apps/*","arn:aws:ecr:REGION:ACCT:repository/vd/platform/sidecar"]},
 {"Effect":"Allow","Action":["logs:CreateLogStream","logs:PutLogEvents"],"Resource":"arn:aws:logs:REGION:ACCT:log-group:/vd/apps/*"},
 {"Effect":"Allow","Action":["secretsmanager:GetSecretValue"],"Resource":"arn:aws:secretsmanager:REGION:ACCT:secret:vd/apps/*"}
]}
```

**Per-app exec role** narrows the above to `app_123` only (own repo, own log group, own `runtime-*` and `sidecar-*` secrets).

**Worker (deployer), key statements:**
```json
[
 {"Sid":"CreateAppRolesOnlyWithBoundary","Effect":"Allow",
  "Action":["iam:CreateRole","iam:PutRolePolicy","iam:AttachRolePolicy"],
  "Resource":"arn:aws:iam::ACCT:role/vd/apps/*",
  "Condition":{"StringEquals":{"iam:PermissionsBoundary":"arn:aws:iam::ACCT:policy/vd-app-boundary"}}},
 {"Sid":"PassAppRolesToEcsOnly","Effect":"Allow","Action":"iam:PassRole",
  "Resource":"arn:aws:iam::ACCT:role/vd/apps/*",
  "Condition":{"StringEquals":{"iam:PassedToService":"ecs-tasks.amazonaws.com"}}},
 {"Sid":"WriteAppSecretsButNotReadRuntime","Effect":"Allow",
  "Action":["secretsmanager:CreateSecret","secretsmanager:PutSecretValue","secretsmanager:TagResource"],
  "Resource":"arn:aws:secretsmanager:REGION:ACCT:secret:vd/apps/*"},
 {"Sid":"ReadOnlyWhatWorkerNeeds","Effect":"Allow","Action":"secretsmanager:GetSecretValue",
  "Resource":["arn:aws:secretsmanager:REGION:ACCT:secret:vd/apps/*/migrator-*",
              "arn:aws:secretsmanager:REGION:ACCT:secret:vd/platform/rds-admin-*"]}
]
```
Plus scoped ECS, ELB, ECR create, Logs create.

**Gateway role:**
- `secretsmanager:GetSecretValue` on `vd/apps/*/agent-*` **only**. Never runtime, migrator or sidecar secrets.
- `sts:AssumeRole` on `vd-agent-ops` only.

**`vd-agent-ops` role** (assumed by gateway per call):
- Base permissions: `ecs:DescribeServices`, `ecs:UpdateService`, `ecs:DescribeTaskDefinition`, `ecs:ListTaskDefinitions`, `logs:FilterLogEvents`, `elasticloadbalancing:DescribeTargetHealth` on `vd` resources.
- The gateway always assumes it with a **session policy** narrowing to one app, e.g. only `service/<cluster>/vd-app-app_123` and log group `/vd/apps/app_123`. Duration 15 minutes. Session name = `action_id` so CloudTrail links AWS calls to our audit log.
- Effective permission = base ∩ session policy. So even a bug in the gateway can't touch another app: AWS refuses.

What this buys:
- Worker can create app roles, but never more powerful than the boundary.
- Worker writes secrets it can't read back.
- Gateway can read only agent DB credentials, and AWS access is per-call, per-app, 15 minutes.
- CloudTrail records everything, tagged with our action IDs.

---

## 19. The agent gateway

### 19.1 Identity and sessions
- **Agent** = id, name, app, acts-for user, policy version, status (active/disabled), created_by.
- **Agent key** `vd_agent_...`, shown once, stored as argon2 hash. Revoke = delete hash + kill sessions.
- **Session:** created when the agent connects. 30 min TTL (policy), bound to one app and one acts-for user. Every call checks: key valid, agent active, app agents enabled, global switch on, session not expired, acts-for user still active.
- Acts-for user removed or demoted → agent sessions die on next call.

### 19.2 The pipeline (every call)

```
1. authenticate        key/session valid, agent active, kill switches off
2. rate limit          per agent, per minute (in-process token bucket; single gateway instance in V0.5)
3. validate            tool exists and is not forbidden; args match schema; names in allowlist
4. policy              auto | approve | forbid, with a reason string
5. risk                low | medium | high (table below)
6. audit (intent)      write "received" record BEFORE doing anything. Fail = deny.
7. scoped creds        short-lived, this call only
8. dry run             what would happen: affected count, diff, diff_hash
9. limits              max rows, max per hour; exceed = deny with reason
10. approval           if needed: status pending_approval, return action_id to agent
11. execute            only if (auto) or (approved and diff_hash still matches)
12. verify             re-read state, compare with expected
13. undo               if verify fails and undo possible
14. audit (result)     final record with outcome
```

Any exception at any step = deny or stop, recorded. **Fail closed.**

### 19.3 Risk scoring (fixed table, not a model)

| Action | Risk |
|---|---|
| read, status, logs | low |
| create 1 row, update ≤ 5 rows | medium |
| update > 5 rows, any delete | high |
| restart app | medium |
| rollback deploy | high |

Policy decides auto/approve; risk is shown to humans and used in the evidence report. A policy can't set `auto` for a `high` action unless the builder ticks an explicit "I understand" box, recorded in the policy version.

### 19.4 Approvals
- Shown in dashboard: agent, acts-for, tool, plain-English summary, diff, risk, reason.
- Approver must be an app admin, and cannot be an agent.
- Approval is bound to `diff_hash`. At execution, the dry run is redone. Different hash = approval void, back to pending.
- Expires after 15 minutes.
- One click approves one action. No bulk approve in V0.5.

### 19.5 Kill switch and revoke
- Per agent, per app, global. Stored in the control plane DB, cached in the gateway for at most 1 second.
- One-click revoke: disables the agent, deletes key hash, kills sessions, cancels pending actions.

### 19.6 Audit log (tamper-evident)
- Table `audit_log`: `id`, `ts`, `action_id`, `event`, `payload_json`, `prev_hash`, `hash`.
- `hash = sha256(prev_hash || canonical_json(row without hash))`.
- Gateway DB role has **INSERT and SELECT only**. No UPDATE, no DELETE.
- `/audit/verify` recomputes the chain and reports the first broken link.
- Payload stores primary keys, changed column names and `diff_hash`. Full diffs live in `actions` for 30 days, then are deleted (the hash stays).

### 19.7 Evidence report
Per app, per date range, computed from `audit_log`:
- total actions, auto-allowed, approved, rejected, expired, denied (by reason), failed, undone
- denials broken down: forbidden tool, limit exceeded, rate limited, database refused (outside acts-for access), kill switch
- **"actions that reached data outside the acts-for person's access: 0"** (backed by the agent-role attack run and the database refusals)
- audit chain status: intact / broken at id N

### 19.8 Prompt injection
Anything the agent reads (rows, logs) may contain instructions. We don't care: decisions are made by the gateway's code and the database, never by what text says.

---

## 20. Gateway connectors

### 20.1 Postgres (the USP)

Connects with the app's **agent** role, sets the acts-for identity per transaction, so the same RLS applies.

Tools (no raw SQL):
- `db_query(table, filters, columns?, order_by?, limit?)`
- `db_create(table, values)`
- `db_update(table, filters, values)`
- `db_delete(table, filters)`

Filters: list of `{column, op, value}` with `op` in `=, !=, <, <=, >, >=, in, is_null`. Tables and columns must exist in the latest introspected schema (allowlist). SQL is built with identifier quoting and bound values only.

Execution:
- **Dry run:** `BEGIN` → `set_config` as acts-for → `SELECT ... FOR UPDATE` the target rows (before-images) → run the change with `RETURNING *` (after-images) → `ROLLBACK`. Diff = before vs after. `diff_hash` over sorted PKs + values.
- **Limits:** affected > `max_rows_changed` → deny.
- **Execute:** same transaction steps, but check affected count and diff hash match the approved dry run, then `COMMIT`. Mismatch → `ROLLBACK`, back to approval.
- **Verify:** new transaction as acts-for, re-read affected PKs, compare with after-images.
- **Undo:** within 24h, run compensating writes **through the same pipeline as the acts-for user** (so undo obeys the same rules), only if current rows equal the stored after-images. Otherwise "can't undo safely, data changed since". Deletes undo by re-insert (may fail on unique/FK, reported).
- Reads: max `max_rows_read`, `statement_timeout` 5s.

Rows the acts-for person can't see simply don't appear. An update aimed at them affects 0 rows and is reported as "no rows you can access matched".

### 20.2 AWS (ops actions)

Only if the acts-for person is an app admin. Scoped creds = `vd-agent-ops` with a one-app session policy (18.3).

| Tool | Dry run | Execute | Verify | Undo |
|---|---|---|---|---|
| `aws_app_status` | n/a | describe service + target health | n/a | n/a |
| `aws_read_logs(since, filter, limit)` | n/a | FilterLogEvents on own group, max 500 lines | n/a | n/a |
| `aws_restart_app` | show current task count and version | UpdateService force new deployment | steady state + healthy targets within 5 min | impossible (marked) |
| `aws_rollback_deploy` | show current and previous version | UpdateService to previous task def | steady state + healthy | redeploy the version it replaced (approval) |

Secrets are never returned. Log lines are returned as-is (they're the app's own logs; builders are warned not to log secrets).

### 20.3 Stubs (design only, V0.5)
Each implements the connector interface with `NotImplemented` and a README section:
- **GitHub:** read files, open PR, comment. One-repo installation token. Never merge, never push to default branch. Undo = close PR.
- **Stripe:** look up customer, refund. Restricted key, max amount. Refund can't be undone → always approval. Stripe idempotency keys = our `action_id`.
- **Internal HTTP APIs:** tools generated from an OpenAPI file. Signed acts-for header (same format as 7.1). Policy per endpoint.

---

## 21. MCP server

- Endpoint: `https://gateway.apps.<domain>/mcp`, Streamable HTTP transport, `Authorization: Bearer vd_agent_...`.
- Session created on MCP initialize, bound to agent + app + acts-for.
- `tools/list` returns only non-forbidden tools for that agent's policy.
- Write tools return either the result, or `{"status":"pending_approval","action_id":"act_..."}`.
- Extra tool: `get_action_status(action_id)` so the agent can wait for approval.
- Same pipeline as REST (`POST /v1/actions`). MCP is just another front door.

Example Claude Code config:
```json
{"mcpServers":{"vibedeploy":{"type":"http","url":"https://gateway.apps.<domain>/mcp",
  "headers":{"Authorization":"Bearer vd_agent_xxx"}}}}
```

---

## 22. Control plane data model

- `builders` (id, email, password_hash)
- `apps` (id, builder_id, name, subdomain, repo, installation_id, stack, protection, status, agents_enabled)
- `deployments` (id, app_id, commit_sha, status, image_digest, schema_hash, blocked_reason, timestamps)
- `access_models` (id, app_id, deployment_id, model_json, confirmed_by, confirmed_at)
- `verification_runs` (id, deployment_id, role [runtime|agent], total, passed, failed, report_json)
- `app_users` (id, app_id, email, password_hash, role, principal_key, session_version, disabled)
- `agents` (id, app_id, name, acts_for_user_id, status, created_by)
- `agent_keys` (id, agent_id, key_hash, created_at, revoked_at)
- `agent_sessions` (id, agent_id, expires_at, revoked)
- `agent_policies` (id, agent_id, version, policy_yaml, author, created_at, high_risk_ack)
- `actions` (per 7.6, full diff kept 30 days)
- `audit_log` (19.6, insert/select only for the gateway role)
- `global_settings` (global_agent_kill_switch)
- `jobs`, `build_tokens`, `listener_rules`

No secret values in the control plane DB.

---

## 23. Dashboard (HTMX, plain)

- **Apps:** list, status, protection badge, live link
- **Deployment:** questions, plain-English rules, verification report (runtime + agent), blocked reason
- **Users:** add, remove, role
- **Agents:** create, acts-for, policy editor with version history, show key once, revoke, kill switch
- **Approvals:** pending actions with diff, approve / reject
- **Activity:** agent actions with status and reason, undo button where possible
- **Audit:** chain status, search
- **Evidence:** report per date range, downloadable JSON

---

## 24. Testing rules

- CI runs kernel, concurrency, derive, attack, shims, gateway tests on every commit.
- Permanent, never skipped: pooling test, planted-bug tests, acts-for escape tests, audit chain tamper test, fail-closed tests.
- Write the failing test first for M1, M2, M5, M11.
- `fixtures/agents/malicious_client.py`: scripted agent that tries every escape (other users' rows, other apps, forbidden tools, injected names, replaying approvals, calling after revoke). Must be fully blocked.

---

## 25. Edge cases and loopholes

### Database
- [ ] Owner bypass: FORCE, no one but the worker has owner/migrator creds
- [ ] BYPASSRLS / superuser never on runtime or agent roles
- [ ] TRUNCATE ignores RLS: never granted
- [ ] Views skip RLS: `security_invoker = true` required
- [ ] SECURITY DEFINER functions: fail unless ours
- [ ] `''` after local setting: `NULLIF`
- [ ] Session-level settings + pooling: always transaction-local
- [ ] String-built SQL: never
- [ ] Missing WITH CHECK: templates always include it
- [ ] Owner reassignment: WITH CHECK on UPDATE
- [ ] Data migrations under FORCE: migrator BYPASSRLS (or NO FORCE window)
- [ ] Grants before rules: grants last
- [ ] New tables: re-check on push
- [ ] Scratch vs real schema: hash compare
- [ ] Slow policies: index path columns
- [ ] NULL owners: admin-only
- [ ] Composite keys: supported
- [ ] Hardcoded `public.`: refuse
- [ ] Known and accepted: error-based side channels (unique/FK violations can reveal a row exists)

### App and runtime
- [ ] Autocommit: no data, warned
- [ ] Unsupported libraries: Unprotected
- [ ] Background jobs: no identity, warned
- [ ] Own login: double login
- [ ] Forged identity header: stripped + HMAC + expiry
- [ ] Session cookie to app: stripped, host-only
- [ ] Shim not active: blocked

### Gateway
- [ ] Agent asks for rows outside acts-for: database returns nothing
- [ ] Agent writes as someone else: database refuses
- [ ] Injected table/column names: allowlist from introspected schema
- [ ] Raw SQL: no tool exists for it
- [ ] Forbidden tool called directly: not listed, and denied if called anyway
- [ ] Approval replay / double execute: status machine, one execution per action
- [ ] Data changed between approval and execution: diff hash mismatch, re-approve
- [ ] Revoked agent mid-session: checked on every call
- [ ] Acts-for user removed: sessions die
- [ ] Gateway can't write audit: deny
- [ ] Gateway reaches another app's AWS resources: AWS refuses via session policy
- [ ] Agent reads secrets: no tool returns them; gateway can't read runtime secrets
- [ ] Prompt injection in data/logs: irrelevant to decisions
- [ ] Undo after data changed: refused with reason
- [ ] Long queries: 5s statement timeout on agent role
- [ ] Gateway holds agent creds for all apps: concentration risk, accepted in V0.5 (next: per-app gateway or per-call DB credentials)
- [ ] Single gateway instance: rate limits in memory, accepted in V0.5

### Platform
- [ ] Untrusted migration code: only CodeBuild or one-off task
- [ ] Privilege escalation via role creation: boundary + path condition
- [ ] PassRole abuse: path + service condition
- [ ] Worker reading runtime secrets: write-only
- [ ] Mutable tags: deploy by digest
- [ ] App grabbing its task credentials: task role empty
- [ ] Build token reuse: one-time, hashed, expiring

---

## 26. Demo script (3 minutes)

1. **The problem:** fixture CRM repo, endpoint does `SELECT * FROM invoices` with no filter.
2. **Deploy:** paste repo, answer questions, see "A rep sees an invoice if they own its customer."
3. **Prove:** "212/212 passed". Push the planted-bug branch (`USING (true)`): deploy blocked with a plain reason. Presence-only scanners pass this.
4. **Use:** two browsers, Alice and Bob, same unfiltered endpoint, each sees only their own invoices.
5. **Add an agent:** "support-bot acts for Alice". Connect Claude Code via MCP.
6. **Agent reads:** "show all invoices" → only Alice's.
7. **Agent escapes:** "mark Bob's invoice paid" → "no rows you can access matched". Logged.
8. **Agent writes:** "delete my draft invoice" → pending approval with diff → approve → done → undo button available.
9. **Agent ops:** "why is the app slow, check logs" → logs of this app only. "roll back the deploy" → approval → rolled back → health verified.
10. **Kill switch:** flip it, agent's next call denied.
11. **Evidence:** "38 actions, 31 auto, 4 approved, 3 blocked, 0 beyond Alice's access. Audit chain intact."
12. **IAM:** show the boundary, the session policy, and CloudTrail entries named with our action IDs.

---

## 27. Cost (approx, us-east-1)

| Item | Monthly |
|---|---|
| ALB | ~$18-22 |
| RDS db.t4g.micro + 20GB | ~$15 |
| Control plane + worker + gateway (Fargate, small) | ~$27 |
| Each app task (0.25 vCPU, 0.5 GB) | ~$9 |
| Public IPv4 per task ($0.005/hr) | ~$3.60 each |
| Secrets Manager | $0.40 per secret (4 per app) |
| Route 53 zone | $0.50 |
| CodeBuild, ECR, logs | small |

About **$65/month base + ~$15 per app**. Apply for AWS Activate credits. Budget alerts on.

---

## 28. What we promise, and what we don't

**We say:**
- "Every deploy is attack-tested. If one user can reach another's data, it doesn't ship."
- "Your AI agent never gets unrestricted production access. It gets the access of the person it works for, never more. Every action is scoped, checked, limited, logged, and reversible where possible."

**We never say:**
- "AI can never break production."
- "100% secure."
- "Works for any app" (protection is Python + SQLAlchemy + Postgres in V0.5).

**Where the proof ends:** per-user data access is proven for Postgres. AWS and future connectors are policy + scoped credentials, not proven the same way.

---

## 29. Working with an AI assistant on this repo

- Give it this file first, every session.
- One milestone at a time. Paste its "Done when" as acceptance criteria.
- It must not change section 3 (rules) or section 7 (contracts) without asking.
- For M1, M2, M5, M11: failing test first.
- Stop it and point here if it proposes: an LLM making allow/deny decisions, raw SQL or shell tools for agents, a plain `SET`, `ALTER DEFAULT PRIVILEGES`, running migrations in the worker, giving the gateway runtime or migrator secrets, UPDATE/DELETE on `audit_log`, or deleting a test to make CI green.