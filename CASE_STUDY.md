# vibedeploy — Engineering Case Study

A long-form, deliberately unflattering record of how this system was designed, built, broken
and fixed. Written to be read by someone who wants to interrogate the engineering, not be
sold on it.

---

## How to read this document, and what it is allowed to claim

This project's git history is four squashed commits. That is not enough to reconstruct a
timeline, so this document does not pretend to have one. What it has instead is two
engineering journals kept during the build — a progress log and a gotchas log — plus the
code, the test suite, and the recorded output of specific runs. Every factual claim below is
tagged against that evidence.

| Tag | Meaning |
|---|---|
| **[verified]** | Read directly out of code, a test, a recorded run, or a journal entry written at the time. |
| **[inferred]** | A reasonable conclusion from the artifacts, labelled as such. Not an observation. |
| **[unknown]** | Genuinely unreconstructable. Stated as unknown rather than filled in. |

Three rules this document follows, because a case study that breaks them is worthless as
evidence:

1. **No invented numbers.** Every benchmark, test count and failure count here was either
   printed by a run or recorded in the journal at the time. Where a number is stale or two
   sources disagree, that is said out loud (see [§12, Known inconsistencies](#12-known-inconsistencies-in-this-document-and-in-the-project)).
2. **No invented bugs.** Every bug in [§5](#5-technical-challenges-the-bugs-that-actually-happened)
   happened. None were composed for narrative effect. Several are embarrassing.
3. **No invented history.** Where the order of events cannot be recovered, this document says
   so instead of inventing a plausible sequence.

### What the commit history cannot tell you **[verified]**

```
50b3916  enforcements and gates
65d85b9  intial local tests
bfe65ec  Snapshot pre-reset work against the superseded V0.5 spec
```

Four commits (one earlier root), tens of thousands of lines, no intermediate states. So:

- **[unknown]** The order in which most modules were written.
- **[unknown]** How long any single bug took to find.
- **[unknown]** How many approaches were tried and discarded without leaving a file behind.
- **[unknown]** Wall-clock project duration.

The real record is `memory/progress.md` (443 lines, milestone-by-milestone, with the
non-vacuity proof for each) and `memory/gotchas.md` (319 lines, one entry per non-obvious
failure, written when it was still fresh). This case study is largely a structured reading of
those two files against the code. Where this document is confident, it is because the journal
recorded a number at the time, not because the author remembers.

---

## Table of contents

1. [Project overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Engineering decisions](#3-engineering-decisions)
4. [Implementation journey](#4-implementation-journey)
5. [Technical challenges: the bugs that actually happened](#5-technical-challenges-the-bugs-that-actually-happened)
6. [Performance](#6-performance)
7. [Reliability and security](#7-reliability-and-security)
8. [Testing](#8-testing)
9. [Deployment](#9-deployment)
10. [Lessons learned](#10-lessons-learned)
11. [Interview preparation](#11-interview-preparation)
12. [Known inconsistencies](#12-known-inconsistencies-in-this-document-and-in-the-project)

---

# 1. Project overview

## 1.1 The problem

A developer writes a multi-tenant web app. Somewhere in it is a query like:

```python
rows = await db.fetch("SELECT * FROM invoices")
```

The intent was `WHERE user_id = $1`. The `WHERE` is missing — forgotten, refactored away, or
never there because the handler was written when the app had one user. The app now serves
every tenant's invoices to whoever asks.

This class of bug has three properties that make it worth building a product around:

1. **It is invisible in review.** The query is syntactically fine and semantically plausible.
   There is nothing to catch unless you know the row's owner column by heart.
2. **It is invisible in tests.** The app's own test suite almost certainly has one user. With
   one user, the unfiltered query returns exactly the right answer.
3. **It is catastrophic on discovery.** Cross-tenant data exposure is not a bug report, it is
   a disclosure.

And the usual mitigations are all in the wrong place. A linter sees text. An ORM scope is
app code — the same layer that had the bug. A code-review checklist is a human promise. A
WAF sees HTTP. All of them sit *above* the database, which means all of them can be bypassed
by the next handler someone writes.

## 1.2 The motivation, stated plainly

The thing I wanted to be able to say, and then prove, is:

> Even if the app runs `SELECT * FROM invoices` with no `WHERE` clause at all, a logged-in
> user gets only their own rows.

That is a claim about the database, not about the app. If it holds, the app's SQL stops being
security-critical. That is a large enough change in the shape of the problem to be worth the
machinery.

A second motivation, which turned out to dominate the back half of the project: **a security
claim nobody can re-run is marketing.** So the product does not emit a certificate or a
score. It emits a test run. Anyone can execute it and watch it pass, or plant a bug and watch
it fail.

## 1.3 Goals

The three pillars, in the order they constrain each other:

**Secure.** Postgres row-level security decides every access. Not app code, not the gateway,
not a middleware. Policies are generated from a small fixed set of templates, never
freehand. Then the system *attacks itself* — a suite that runs as a real low-privilege role
and tries to read and write other users' rows — and a single failure blocks the release.

**Reliable.** A deploy is a multi-step, partially-external process (containers, proxy
routes, database roles). It will be interrupted. So: leases and fencing tokens on the job
queue so an interrupted worker cannot come back and stomp its successor; a generation fence
that extends the same guarantee to resources Postgres does not own; a health-gated
blue-green cutover; and a reconciliation loop that converges the world back to what the
database says.

**Reproducible.** Every claim in the documentation names the test that proves it. Every
verification run saves its own evidence JSON. Negative controls are part of the suite: a
check that cannot fail does not count, so the journal records, for each feature, exactly how
many tests break when the feature is deliberately sabotaged.

## 1.4 Explicit non-goals **[verified — `Readme.md` §28, and the cut list in the plan]**

Stated up front because a case study that lists only wins is a brochure:

- **Not a sandbox.** The worker holds the host's Docker socket. Anything that can reach that
  socket can ask for a privileged container. So per-app isolation is defence in depth, and
  the thing the product actually rests on is Postgres. This is written into
  `infra/prod/docker-compose.yml` as a comment at the top of the file, not buried in a doc.
- **Not AWS.** Two milestones of AWS work (CodeBuild, ECS) were specced and then **cut
  without being started**. Local-first, one Linux box.
- **Not Kubernetes, Redis, canary traffic shifting, OTel/Prometheus/SLOs, per-tenant quotas,
  multi-cloud, billing, or custom domains.** All explicitly cut.
- **WebSockets are out of scope for the zero-downtime claim.** The drain measurement is for
  request/response traffic. A long-lived socket is not covered, and the claim says so.
- **The app's own logic above the database is not verified.** If the app decides to show you
  a page you shouldn't see, and fetches no rows to do it, RLS has no opinion.

## 1.5 The solution, in one pass

```
 git push
    │
    ▼
 /hooks/github ──── HMAC-SHA256 verify ──── delivery-id idempotency
    │                                            (duplicate → no second job)
    ▼
 deployment row + job row              apps.desired_generation += 1
    │
    ▼
 worker claims job (FOR UPDATE SKIP LOCKED + lease + fence)
    │
    ├─ clone the builder's repo at a resolved commit
    ├─ provision: schema app_<id>, ledger schema app_<id>_vd, four roles
    ├─ run the app's migrations ── in a one-off CONTAINER, against a THROWAWAY database
    ├─ introspect the real schema  → tables, columns, FKs, schema_hash
    ├─ derive the access model     → which template fits which table
    ├─ generate policies           → 5 templates only, never freehand SQL
    ├─ ATTACK  ─── as `runtime` ───┐
    │              as `agent`  ────┴─ results must be IDENTICAL
    │              any failure → deploy BLOCKED, old version still serving
    ├─ start the new generation (two containers: login gate + app)
    ├─ health gate: N CONSECUTIVE passes
    ├─ publish (the only write that grants traffic; fenced on desired_generation)
    ├─ re-assert the whole proxy route table from the database
    └─ retire the old generation: drain → SIGTERM → verify the port went quiet
    │
    ▼
 live at https://<subdomain>.<app-domain>
```

And running continuously beside the queue, in the same process, a second loop: adopt orphans,
reap stale generations, restate the route table, unstick stranded deployments.

---

# 2. Architecture

## 2.1 Processes

Five long-lived containers plus two kinds of short-lived ones.

```
                    internet
                       │  :80 :443  ← the ONLY published ports on the host
                       ▼
                 ┌───────────┐
                 │   Caddy   │  route table written over its admin API
                 │           │  from the database. Never from the Caddyfile.
                 └─────┬─────┘
          ┌────────────┼──────────────────────┐
          ▼            ▼                      ▼
   console.<dom>   <sub>.apps.<dom>      (admin :2019 — compose network only)
          │            │
          ▼            ▼
  ┌───────────────┐   ┌──────────────────────────────┐
  │ control-plane │   │  per-app, per-generation:     │
  │  :8100        │   │  ┌────────┐   ┌───────────┐   │
  │  FastAPI +    │   │  │ login  │◄──│    app    │   │
  │  Jinja2/HTMX  │   │  │  gate  │   │  uvicorn  │   │
  └───────┬───────┘   │  └────────┘   └───────────┘   │
          │           │   one shared network namespace │
          │           └──────────────────────────────┘
          │                      │
          │                      │ "who owns this cookie?"
          │◄─────────────────────┘   (VD_SIDECAR_API_KEY)
          │
          │  ┌──────────┐        ┌──────────────────────────┐
          ├─►│ gateway  │        │         worker           │
          │  │  :8200   │        │  loop 1: job queue       │
          │  │ MCP/HTTP │        │  loop 2: reconciler      │
          │  └────┬─────┘        └───────┬──────────────────┘
          │       │                      │  /var/run/docker.sock
          │       │                      │  ← the trust boundary.
          │       │                      │    NOTHING else gets this.
          ▼       ▼                      ▼
  ┌──────────────────────┐      ┌────────────────────────┐
  │   postgres :5432     │      │  buildpg :5432         │
  │  vibedeploy_control  │      │  vibedeploy_build      │
  │  vibedeploy (apps)   │      │  THROWAWAY. no volume. │
  │  app_<id> × N        │      │  untrusted migrations  │
  │  app_<id>_vd × N     │      │  run here and only here│
  └──────────────────────┘      └────────────────────────┘
```

**control-plane** — FastAPI. The console (server-rendered Jinja2 + HTMX, no SPA), the REST
API, the GitHub webhook endpoint, operator auth, and the Alembic migration runner. It holds
**no app database credentials** it doesn't need and **no agent secrets at all**.

**worker** — one process, two `asyncio` loops. Loop one polls the `jobs` table and runs
deployment pipelines. Loop two is the reconciler, every `VD_RECONCILE_INTERVAL` (10s). This
is the only process with the Docker socket, and therefore the only process that can start a
container.

**gateway** — the agent half. FastAPI plus the `mcp` SDK over Streamable HTTP. A separate
process from the console specifically because of *what each is allowed to read*: the gateway
may read `vd/apps/*/agent-*` secrets; the console may read none of them. Two processes is how
that is enforced rather than asserted.

**caddy** — the only thing with a public address. Both halves of the product are reached
through it. Its configuration is **not** in its config file; it is POSTed over the admin API
from what the control plane's database says is live, which is what makes the reconciler able
to *re-assert* rather than *trust* it.

**postgres** — the customer database (every app gets two schemas and four roles) and, as a
separate database on the same instance, the control plane's own tables.

**buildpg** — a second Postgres instance whose only job is to be destroyed. See
[§3.6](#36-why-the-build-database-is-a-separate-instance-not-a-separate-schema).

Short-lived: the **build job** container (runs the app's own migrations — untrusted code) and
the **app containers** (two per generation).

## 2.2 Data flow for one request to a deployed app

```
browser
  │ GET https://acme.apps.example.com/invoices   (cookie: vd_app_session=…)
  ▼
Caddy          — TLS terminate, match host → upstream = gate container name
  ▼
login gate     — no cookie?    → 302 to the gate's own login page
               — cookie?       → POST control-plane /internal/whoami
                                   header: VD_SIDECAR_API_KEY
                                 ← {"user_id": "u_17", "is_admin": false}
               — attach identity header, proxy to 127.0.0.1:<app port>
                 (same netns, so loopback is the app)
  ▼
app (uvicorn)  — shim middleware reads the identity header
               — opens a transaction
               — set_config('app.user_id', $1, true)   ← bind param, tx-local
               — runs the handler's SQL as role `runtime`
  ▼
Postgres       — every table: FORCE ROW LEVEL SECURITY
               — policy USING (user_id = vd_user_id())
               — vd_user_id() = NULLIF(current_setting('app.user_id', true), '')::type
               — no identity → NULL → zero rows. Never "everything".
```

The load-bearing line is the third from the bottom. `SELECT * FROM invoices` and
`SELECT * FROM invoices WHERE user_id = $1` return the same rows, because the policy is
applied by the database, after the planner has the query.

## 2.3 Data flow for one agent action

The agent half reuses the same enforcement under a different role:

```
AI agent (MCP client)
  │ tool call: update_invoice_status(id=812, status="paid")
  ▼
gateway        — is this tool exposed to this agent at all?
                 forbidden tools are NOT LISTED and NOT CALLABLE
               — DRY RUN as role `agent`, identity = the person it acts for
                 → diff, diff_hash, risk score
               — write the audit record BEFORE executing. If the write fails,
                 do not execute.
               — risky? → status=pending, return. Nothing has happened.
  ▼
console        — operator approves. Approval is bound to the exact diff_hash.
  ▼
gateway        — re-run the dry run. diff_hash changed? approval is VOID,
                 back to pending.
               — claim: UPDATE actions SET status='executing'
                        WHERE id=$1 AND status='approved'   ← 0 rows = someone
                        else got it. At most once.
               — execute as role `agent`, same RLS, same person.
  ▼
Postgres       — identical policies. The agent is not a second security model.
```

That last line is the design claim the whole gateway exists to make, and it is tested by
running the attack suite twice — once as `runtime`, once as `agent` — and requiring byte-identical
results.

## 2.4 The four roles **[verified — `kernel/sql/roles.sql`, `CLAUDE.md`]**

Per app, four roles. The count is the design.

| Role | Attributes | Who gets it | Why it exists |
|---|---|---|---|
| `owner` | `NOLOGIN` | nobody | Owns the schema objects. Cannot log in, so owning is not a capability anyone holds. |
| `migrator` | `BYPASSRLS` | the build job only | Must see all rows to run a migration. Never handed to the app or the gateway. |
| `runtime` | `NOBYPASSRLS`, `NOINHERIT`, not owner | the app | The normal case. |
| `agent` | same, plus `statement_timeout = '5s'` | the gateway | A separate role so that "the agent did this" is a database-level fact, and so the parity test has two subjects. |

Two notes that matter more than they look:

- **`NOINHERIT` is not cosmetic.** Without it a role that is a member of another role picks up
  its privileges implicitly, and the careful separation above becomes one role with four
  names.
- **Not an owner.** In Postgres, the owner of a table is exempt from its policies unless you
  go out of your way. `runtime` and `agent` own nothing, which is why `FORCE ROW LEVEL
  SECURITY` plus non-ownership is the actual guarantee and either one alone is not.

## 2.5 Two schemas per app, and why **[verified]**

Every app gets:

- `app_<id>` — the real schema. Introspected, policied, attacked.
- `app_<id>_vd` — the ledger. Holds `vd_applied_migrations`.

Neither `runtime` nor `agent` has `USAGE` on the ledger.

The ledger could have been a table in the app schema. It is not, for a specific reason: every
table in `app_<id>` is introspected, assigned a policy template, and attacked. An exception
carved out by name — "ignore the table called `vd_applied_migrations`" — is a table an app
could create in order to hide in. A second schema has no such hole. **[verified — this
decision is recorded in the journal and was forced by a real bug; see
[§5.14](#514-no-app-with-plain-sql-migrations-could-be-deployed-twice).]**

## 2.6 Technology choices

| Layer | Choice | Reasoning |
|---|---|---|
| Control plane | Python 3.12, FastAPI, Pydantic v2 | Async all the way down, and the type-validated request layer is free. |
| Console UI | Jinja2 + HTMX, server-rendered | **No SPA on purpose.** The console's job is forms, tables and a live-ish status. A build step, a client router and a JSON API duplicating every template would be strictly more to maintain and strictly more to secure. HTMX covers partial updates. |
| Control-plane DB access | SQLAlchemy 2 + Alembic | The control plane's own schema changes over time and wants migrations. ORM is appropriate here. |
| Kernel / verifier / provisioner / gateway DB access | **asyncpg + raw SQL, no ORM** | These components write DDL, `CREATE ROLE`, `GRANT`, `CREATE POLICY`, and `set_config`. An ORM is a liability in that work: it abstracts exactly the details that are the product. Raw SQL with bind parameters is both clearer and safer here. |
| Job queue | the `jobs` table, `FOR UPDATE SKIP LOCKED` | **No Redis, no SQS.** Postgres is already a hard dependency and already the thing we trust most. Adding a broker adds an operational component and a second source of truth about what work exists. `SKIP LOCKED` gives competing consumers without either. |
| Proxy | Caddy 2 | Automatic ACME, and — the deciding feature — a **JSON admin API**, which is what makes "the route table is derived from the database" implementable. |
| Untrusted execution | `docker run --rm` | Covered in [§3.5](#35-untrusted-code-runs-in-a-container-or-the-deploy-blocks). |
| Agent protocol | `mcp` SDK, Streamable HTTP | The standard surface for tool-using agents. |
| Identity shim | Python only (SQLAlchemy) | A Node/Prisma shim existed and was **deleted**; see [§4.5](#45-the-scope-reset-deleting-working-code). |

---

# 3. Engineering decisions

Each decision below is stated as: what was chosen, what the alternatives were, what it costs,
and why it won. Where a decision was forced by a bug, the bug is cross-referenced.

## 3.1 Enforcement lives in Postgres, not in app code

**Chosen:** row-level security policies on every table, `FORCE ROW LEVEL SECURITY`, identity
supplied per transaction via `set_config`.

**Alternatives considered:**

- *A query-rewriting library or ORM scope.* This is the common answer. It fails on the
  premise: the layer doing the rewriting is the same layer that wrote the bug. A handler that
  bypasses the scope — or uses a raw connection, or a `text()` escape hatch — is unprotected,
  and nothing tells you.
- *A database proxy that parses and rewrites SQL.* Now you own a SQL parser. Every feature of
  Postgres you fail to parse is either an outage or a hole, and CTEs, lateral joins and
  `INSERT ... ON CONFLICT ... RETURNING` are not optional in real apps.
- *Views instead of policies.* Works for reads. For writes you need `INSTEAD OF` triggers,
  and now authorization is spread across triggers and views with no single place to audit.

**Cost:** policies are DDL, so they must be generated and migrated; Postgres RLS has real
semantic corners that bit this project hard (see [§5.10](#510-postgres-applies-the-select-policy-to-update-with-a-where-clause)
and [§5.11](#511-a-missing-with-check-is-behaviourally-invisible)); and the whole thing is
Postgres-only.

**Why it won:** it is the only option where the app's SQL is *allowed* to be wrong. Every
other option requires the app to cooperate. The claim "even an unfiltered `SELECT *` is safe"
is only sayable from inside the database.

## 3.2 Five policy templates, no freehand SQL

**Chosen:** policies may only be generated from `owner_column`, `fk_chain`, `shared`,
`read_only_shared`, `admin_only`. No `USING (true)`. Every policy covers SELECT, INSERT,
UPDATE and DELETE, with both `USING` and `WITH CHECK`.

**Alternative:** let the builder write the predicate, or let an LLM write it.

**Why not:** a free-form predicate is a free-form security hole, and it defeats verification.
With five templates, "is this policy correct?" is a decidable structural question — the
verifier can assert that each table has four policies, that each has both clauses, that the
predicate is literally one of the five renderings. With arbitrary SQL, the only check
available is behavioural, and behavioural checks miss exactly the invisible failures
([§5.11](#511-a-missing-with-check-is-behaviourally-invisible)).

**Cost, stated honestly:** schemas that don't fit a template are not supported.
`Readme.md` contract 7.2 cannot even *spell* a composite foreign key **[verified — recorded
as a known limitation in the gotchas journal]**, so an `fk_chain` through a composite key
cannot be expressed. That is a real product gap, written down rather than papered over.

## 3.3 No LLM makes any security decision

**Chosen:** detection and derivation are deterministic code. The LLM in this product talks to
humans and writes nothing that ends up in a policy.

**Reasoning:** an LLM-authored policy has no stable failure mode you can test against. The
attack suite can't distinguish "the model was right" from "the model was lucky this run," and
a security control you cannot regression-test is not a control. The asymmetry is stark:
deterministic derivation is wrong in the same way every time, which means a test catches it
once and forever.

**Where the LLM is allowed:** asking the builder questions about their access model in
natural language, and summarising a report. **Never guessing the access model** — if the
builder doesn't answer, the launch is blocked. A blocked launch is a bad day; a guessed
access model is a breach.

## 3.4 The system attacks itself, twice, and the results must match

**Chosen:** after policies are applied, a suite connects **as the real `runtime` role** and
**as the real `agent` role** and tries to read and write rows belonging to other users. Any
failure blocks the deploy. The two runs must produce identical results.

**Alternative:** assert the policies are correct by reading them back out of `pg_policy`.

**Why not:** that is marking your own homework. If derivation has a bug, the policy it wrote
is exactly the policy it expected to read back. This is why the project's hardest testing
rule is: **test expectations come from the seeding plan, never from reading the policies
back.** The suite knows that user A owns rows 1–5 because it inserted them; it then checks
what user B can see, against the plan, not against the schema.

**Why run it twice:** the parity run is the entire justification for the agent half. The
claim "an agent gets exactly the access of the person it works for" is otherwise an
architecture diagram. Running the same adversarial suite under both roles and requiring the
same answer is the only form of that claim that can be checked. The suite carries a dedicated
`agent_differs` check for it.

## 3.5 Untrusted code runs in a container, or the deploy blocks

**Chosen:** an app's own migrations run in a one-off `docker run --rm`. If Docker is
unavailable, the deploy **blocks**. There is no subprocess fallback.

**Reasoning:** the fallback is the bug. A fallback path means that on the day the socket is
missing, the system silently executes a stranger's migration code in the worker — the process
holding the Docker socket and every platform credential. The failure mode of blocking is a
stuck deploy and an angry user; the failure mode of falling back is total compromise. A
fallback would also be *untested in CI* (because CI has Docker), which is the worst possible
property for a security-critical branch.

**Cost:** Docker is a hard dependency for development, not just for the stack. That is stated
in `CLAUDE.md` so nobody discovers it at the wrong moment.

## 3.6 Why the build database is a separate instance, not a separate schema

**Chosen:** a second Postgres container, `buildpg`, with its own credentials and **no
volume**, which the build job is handed admin of.

**Reasoning:** the build job runs the app's own migrations, which means it must be given
admin rights over whatever it is pointed at — a migration legitimately creates tables, types
and indexes. Point it at the customer instance and you have handed code you have never read
admin over every other app's data. A separate schema on the same instance does not help; the
grant is instance-level in practice once you have a superuser-ish role.

The no-volume part is deliberate too: a restart is a clean slate, which is what "throwaway"
means, and nothing in there is ever read back.

**[verified]** The env template goes further and requires the build database's password to be
*different* from the other two, with the reason inline: "this is the one credential that is
handed to a container running code we have never read."

## 3.7 Fail closed, as a rule with no exceptions

Everywhere a decision could default, it defaults to deny:

| Situation | Behaviour | Where |
|---|---|---|
| No identity set | `vd_user_id()` is NULL → zero rows | `NULLIF(current_setting('app.user_id', true), '')` |
| Webhook secret unset | **reject every delivery** | the webhook verifier |
| `global_settings` row missing | kill switch reads as **ON** | control plane settings read |
| Console/gateway API key unset | admits nobody | auth layer |
| Audit write fails | **do not execute the action** | gateway, audit-before-action |
| Policy missing for a table | deploy blocked | structural checks |
| Proxy unreachable | deploy blocked, **no raw-port fallback** | `proxy.require` |

The `current_setting('app.user_id', true)` detail is small and important. The second argument
is `missing_ok`. Without it, an unset setting raises; with it, you get `NULL`, and the
`NULLIF` converts the empty string to `NULL` too. Both roads lead to zero rows. An
implementation that returned `''` and compared it to a text column would also fail closed by
accident — but accidentally is not good enough, so the function is explicit.

## 3.8 Identity is set exactly one way

**Chosen:** `set_config('app.user_id', $1, true)` — bind parameter, `is_local = true`, inside
a transaction.

**Rejected, explicitly and permanently:** plain `SET app.user_id = ...`.

**Reasoning:** three distinct failure modes are closed by this one line.

1. `SET` cannot take a bind parameter, so using it forces string interpolation into SQL. That
   is SQL injection in the authorization layer, which is the worst place to have it.
2. `SET` without `LOCAL` is session-scoped. On a pooled connection the identity outlives the
   request and the next request inherits it — a user silently acting as the previous user.
   This is not hypothetical: the project has a **permanently protected** test that drives
   500 concurrent requests across 5 users through PgBouncer in transaction mode, ten times
   over, to prove it cannot happen.
3. `is_local = true` means the value dies with the transaction, with no cleanup code to
   forget to write.

## 3.9 Leases and fencing on the job queue

**Chosen:** `FOR UPDATE SKIP LOCKED` to claim, plus `lease_expires_at` (60s lease, ~20s
heartbeat), plus a monotonically increasing `fence` column bumped on **every** claim and
named in the `WHERE` clause of **every** settling write. `MAX_ATTEMPTS = 3`.

**Alternative:** `SKIP LOCKED` alone.

**Why that is not enough:** `SKIP LOCKED` holds a row lock for the life of the transaction.
A deployment pipeline is minutes long and makes external calls, so holding a transaction open
across it is wrong (it pins a connection and blocks vacuum). Once you claim-and-commit, the
lock is gone and the only thing preventing a second worker from taking the job is a timeout.
And a timeout alone gives you the classic distributed-lock failure: the first worker was not
dead, only paused — a GC pause, a host suspend, a slow `docker` call — and when it resumes it
believes it still owns the job.

**The fix is Kleppmann's fencing token.** Each claim increments `fence`. Every write that
settles the job (`finish`, `park`, `heartbeat`) carries the fence the worker was given:

```sql
UPDATE jobs SET status = 'done'
 WHERE id = :id AND fence = :fence
```

The resumed zombie's fence is stale, so its update matches zero rows. It cannot finish, park
or extend a job that has moved on.

**Two implementation decisions inside this one:**

- **`finish` / `park` / `heartbeat` return a boolean, and ignoring it is the bug.** A fenced
  write that matches zero rows is not an error — it is the expected outcome for a zombie. So
  the functions report it and the caller must act on it. Silently discarding the return value
  recreates the problem the fence was added to solve.
- **Expired leases are reclaimed *inside* `claim()`**, not by a sweeper. A sweeper is a second
  actor with its own schedule, which means there is a window in which a job is owned by
  nobody and a window in which it is owned by two. Folding reclamation into the claim makes
  "take this job" a single atomic decision.

## 3.10 The fence has to extend past the database

**Chosen:** `apps.desired_generation`, bumped when work is queued. Three labels on every
Docker resource — `vd.app`, `vd.generation`, `vd.deployment`. And one rule: **traffic is only
ever granted by a fence-protected database write.**

```sql
-- generations.publish(), the only write that grants traffic
UPDATE apps SET live_generation = :generation
 WHERE id = :app AND desired_generation = :generation
```

**Reasoning:** the job fence protects rows. A deploy's effects are not rows — they are
containers on a host and routes in a proxy. A stale worker that cannot write to `jobs` can
still call `docker run`, and in the naive design it can still POST a route to Caddy. The
fence has to cover the external world or it covers nothing that matters.

**The claim is deliberately narrow**, and this is the part I would defend in an interview:

> A stale worker cannot route traffic, and its leftovers are reaped within one reconcile
> interval.

Not "a stale worker cannot do anything." It can start a container — nothing can stop that
once it holds the socket. What it cannot do is make that container *reachable*, because
reachability requires a publish that its generation no longer matches. The labels then let
the reconciler find and remove the orphan. Claiming more than this would be a lie; claiming
less would make the feature pointless.

## 3.11 Blue-green with a health gate, in a fixed order

**Chosen order**, and it is the order that makes the guarantee:

```
start new → health gate (N CONSECUTIVE passes) → publish (fenced)
         → re-assert proxy → wait drain → SIGTERM old → verify exit
```

Config: `health_passes=3`, `health_interval=0.5s`, `health_timeout=30s`, `drain_seconds=2.0`.

**Decisions inside it:**

- **Consecutive, not cumulative.** A process that is flapping will accumulate N passes
  eventually. Three *in a row* is a signal about stability; three *in total* is a signal about
  patience. Any single failure resets the counter.
- **The probe checks two things**: the login gate's health endpoint (`/__vd/health`) **and**
  a raw TCP connect to the app's port. The gate can be up while the app behind it is still
  importing, and an app the gate can't reach is not ready however healthy the gate says it is.
- **Drain before SIGTERM, and the drain must exceed (longest in-flight request + keep-alive
  idle).** Otherwise you cut a connection the proxy was about to reuse.
- **Verified exit is measured by the port going quiet, never by a pid.** This is a bug fix
  masquerading as a design decision — see [§5.17](#517-oskillpid-0-terminates-the-process-on-windows).
- **A failed gate leaves the old generation serving.** The new one is simply never published.
  This is the whole point of doing it in this order: failure is a non-event.

## 3.12 The cutover is measured, not asserted

**Chosen:** a test that drives steady RPS through a real flip and counts non-2xx responses
**and connection errors**, and prints the numbers.

**Reasoning:** "zero downtime" is the single easiest claim in infrastructure to assert and
never check. A test that performs a cutover and then asserts the app is up afterwards proves
nothing about the cutover. Counting connection errors separately from HTTP status matters
because the failure mode of a bad drain is a dropped TCP connection, which an HTTP-status-only
harness records as... nothing at all. It never gets a status.

**And the negative control is part of the feature**, not an afterthought: a run with
`drain_seconds=0` must be able to produce failures, and a `SIGKILL` run must produce a lot of
them. If the harness can't see a broken cutover, it can't see a working one either.
Recorded numbers are in [§6.3](#63-the-cutover-measurement).

## 3.13 A reconciler that will never start an app

**Chosen:** `worker/reconcile.py`, a loop returning
`Drift(adopted, reaped, lost, routes, unstuck, proxy_unavailable)`.

The single most important decision here is a restriction: **the reconciler will never start
an application.** If it finds a generation that the database says is live but nothing is
listening for, it reports it as `lost` and leaves it alone.

**Reasoning:** a controller that starts things is a controller that can fight the pipeline.
The deploy path and the reconcile loop would both be trying to converge the same resource
with different information, and the reconciler's information is always a snapshot. The worst
case is a flapping app being endlessly restarted by a loop that cannot see why it died. So
the reconciler's job is strictly: adopt what exists, reap what is stale, restate the route
table, unstick what is stranded, and *report* what it cannot fix. `lost` is a signal to a
human.

**Other decisions inside the loop:**

- **Order is processes → unstick → `proxy.reassert` last.** The route table must be written
  from the final state, not from a state that is about to change.
- **`keep` is a set, not a value.** The naive version keeps "the generation that holds
  traffic." That reaps the brand-new generation that a pipeline is mid-way through starting,
  because it doesn't hold traffic yet. So `keep` = the traffic holder **plus every generation
  held by a live lease**. Planting `one < keep` as a scalar comparison fails exactly 2 tests
  **[verified]**.
- **Orphans are adopted, not duplicated.** Found by label, so a container from a previous
  worker is recognised as the thing it is.
- **The loop is idempotent**, and that is tested by running it twice and requiring the second
  pass to report no drift.

## 3.14 The proxy route table is derived, never trusted

**Chosen:** Caddy's config is written over its admin API from the control plane's database,
every reconcile interval. The `Caddyfile` contains no routes and never did.

**Reasoning:** if the route table is the source of truth, then every partial failure leaves
it authoritative and wrong — a route to a dead generation, or a missing route for a live one,
with nothing to correct it. If the database is the source of truth, the proxy becomes a cache
that is continually refreshed, and "re-assert" becomes a meaningful operation. This is what
lets the reconciler fix a hand-edited or restarted proxy.

**Cost:** an operator cannot usefully edit the Caddyfile; their changes get overwritten
within 10 seconds. That is a documented property, not a bug. **[verified]** It also produced
a real production-class bug about intermediate objects in Caddy's config tree
([§5.18](#518-caddys-admin-api-does-not-create-intermediate-objects)).

## 3.15 Audit before action, and the chain is append-only

**Chosen:** the gateway writes the "about to execute" record **before** executing. If the
write fails, it does not execute. `audit_log` is hash-chained and append-only — `UPDATE` and
`DELETE` on it are things the project refuses to implement.

**Reasoning:** audit-after-action has a window in which an action has happened and no record
of it exists, and that window is exactly the one an incident lands in. Inverting it means the
worst case is a record of something that didn't happen, which is recoverable, instead of
something that happened with no record, which is not.

The hash chain makes tampering detectable rather than prevented — you cannot stop a database
admin from editing a row, but you can make the edit break every subsequent hash. Chain
appends take an advisory transaction lock (`0x76446761`) so concurrent writers cannot
interleave and produce two rows claiming the same predecessor.

## 3.16 Approval is bound to a diff hash, and actions execute at most once

**Chosen:** an approval references the exact `diff_hash` from a dry run (15-minute window).
Before executing, the gateway re-runs the dry run; if the hash differs, the approval is void
and the action returns to `pending`. Execution is claimed with a guarded update:

```sql
UPDATE actions SET status = 'executing'
 WHERE id = :id AND status = :expected
```

Zero rows affected means somebody else claimed it. The status machine is frozen in
`Readme.md` contract 7.6 and only those transitions are legal.

**Reasoning:** an operator approves *a described effect*, not an action id. If the data moved
between the description and the execution, the thing they approved no longer exists, and
executing anyway is a confused-deputy attack with the operator as the deputy. Re-deriving the
hash is the only way to know.

The guarded update is the same exactly-once trick as the job fence, applied to a different
table: make the precondition part of the write, and let the database arbitrate.

## 3.17 Forbidden tools are not exposed at all

**Chosen:** agents get narrow, typed tools. No raw SQL, no shell, no credentials. Tools the
agent may not use are **not listed and not callable** — not listed-and-rejected.

**Reasoning:** a tool that is listed and then refused is an information leak and an invitation;
it tells the agent the capability exists and encourages it to find a path to it. More
practically, "listed but guarded" means the guard is a conditional somewhere, and conditionals
get refactored. Absence from the tool list is enforced by construction.

And the deeper reason: a raw-SQL tool makes every other control in the system cosmetic. All
the policy derivation and attack verification is about constraining what the database will
answer; a tool that lets an agent write arbitrary SQL as a privileged role walks around it.
This is on the project's short list of things to refuse outright even if asked.

## 3.18 Alembic support, and the ledger schema

**Chosen:** apps may ship either plain `.sql` migration files or Alembic. Plain-`.sql` apps
get an applied-migrations ledger in their own `_vd` schema.

**Reasoning:** Alembic apps already track their own revisions in `alembic_version`. Plain
`.sql` apps track nothing, so re-running the directory on the second deploy re-executes
`001_init.sql` and fails on `CREATE TABLE`. This was found by a real deploy, not by
reasoning — see [§5.14](#514-no-app-with-plain-sql-migrations-could-be-deployed-twice).

## 3.19 Builder environment variables, with `.env` excluded

**Chosen:** builders can set env vars for their app. Files named `.env` in the repository are
**excluded** from the deployment.

**Reasoning:** a committed `.env` is a committed secret. Deploying it means the platform
treats a leaked credential as configuration and keeps it alive. Excluding it forces the real
value through the env-var path, where it is encrypted at rest in the state root. The journal
records this as a security fix, not a feature. **[verified]**

## 3.20 Operator auth: argon2, and no accidental open door

**Chosen:** argon2id password hashing, signed session cookies (`VD_SESSION_SECRET`,
`Secure` flag driven by `VD_PUBLIC_SCHEME`), and a one-time setup page for the first
operator — *unless* `VD_BOOTSTRAP_EMAIL`/`VD_BOOTSTRAP_PASSWORD` are set, in which case the
first operator is created on boot.

**The decision worth explaining** is the interaction. A fresh box with no operator serves a
setup page. On a public address, **the first stranger to find it becomes the operator.** So
the env template requires the bootstrap pair in production and says exactly that in a
comment. The pair is ignored once any builder exists, so it is not a standing password reset
either.

There is no "unsigned session" mode. An empty `VD_SESSION_SECRET` would make every session
forgeable, which is the same thing as having no login, so the control plane refuses to start
without it.

## 3.21 `VD_ENV=prod` turns conveniences into refusals

**Chosen:** one variable that converts every development default in `control_plane/config.py`
into a hard startup failure.

**Reasoning:** development defaults exist so that `pytest` works on a laptop without a
twenty-line `.env`. Those same defaults, shipped, mean a stack that comes up on a public
address using a Postgres password that is **in the repository**. The options are: no defaults
at all (and a worse development experience, which costs real velocity), or defaults that are
illegal in production and fail loudly. One variable, checked at startup, is the cheapest
version of the second.

## 3.22 Code is mounted into the production image, not copied

**Chosen:** `infra/prod/Dockerfile` installs dependencies and a user, and nothing else. The
repository is bind-mounted at `${VD_ROOT}:${VD_ROOT}:ro`, at the **same path** it has on the
host.

**This is not a convenience, it is load-bearing.** The worker starts app containers by asking
the *host's* Docker daemon for them, and a `--volume` flag in that request is resolved by the
daemon on the host — not inside the worker. So every path the worker hands over must mean the
same thing on both sides of its own filesystem. It only can if the repository and the state
root are mounted at the paths they already have.

A copied-in `/app` would break app deploys in a way that looks like a missing file: the
worker would construct `/app/.../deployments/<id>` and hand it to a daemon for which that
path does not exist. **[inferred — this is the documented reason in the Dockerfile; the
specific failure has not been observed because the design never allowed it.]**

## 3.23 One image, three commands

**Chosen:** control plane, worker and gateway are the same image with different `command:`
entries.

**Reasoning:** they are the same codebase. A second Dockerfile would exist only to leave the
`docker` CLI out of two of them, and that buys nothing — reaching a daemon needs a socket,
and only the worker is given one. The trust boundary is the socket mount, not the presence of
a client binary.

## 3.24 PgBouncer is in the local stack and deliberately absent in production

**Chosen:** local compose runs PgBouncer in transaction mode on `55433`. The production
compose file does not run it at all, and says why in a comment.

**Reasoning:** nothing in the product dials a pooler. An app's DSN is derived from
`VD_APP_ADMIN_DSN`, and that same DSN is what provisioning runs `CREATE ROLE` and `GRANT`
over — which a transaction pooler is the wrong thing to sit in front of. PgBouncer exists
locally so the concurrency suite can prove the identity shim survives being handed a
connection mid-session. **It is a test fixture, not a component.** Shipping it with the local
stack's `trust` server-side auth would be shipping a hole in order to pool connections nobody
sends.

I like this decision because the reasoning is the opposite of the usual direction: the
component exists in dev and not in prod, because its purpose is to make a test adversarial.

## 3.25 Exact version pins in the image, floors in `pyproject.toml`

**Chosen:** `pyproject.toml` declares floors; the Dockerfile pins 13 exact versions.

**Reasoning:** a floor is right for a developer's virtualenv, which should be free to pick up
a fix. A floor is wrong for an image, where "the same image produces the same behaviour" is
the only property worth having. The pinned set is the set the suite is green on.

**[verified]** The pins: `fastapi==0.141.1`, `uvicorn==0.53.0`, `starlette==1.6.0`,
`httpx==0.28.1`, `python-multipart==0.0.32`, `SQLAlchemy==2.0.54`, `asyncpg==0.31.0`,
`alembic==1.20.0`, `argon2-cffi==25.1.0`, `Jinja2==3.1.6`, `cryptography==50.0.2`,
`PyYAML==6.0.3`, `mcp==2.3.0`.

---

# 4. Implementation journey

**[unknown]** Dates, durations and the fine-grained order of work are not recoverable from
four squashed commits. What follows is the structure recorded in the progress journal, which
is organised by milestone and checkpoint — so the *dependency order* is reliable even where
the calendar is not.

## 4.1 The shape of the plan, and that it changed twice

The project started against a milestone plan, **M1–M14**, with a rule in `CLAUDE.md`: one
milestone at a time, that milestone's "Done when" is the acceptance criteria, and you do not
start the next until it passes. For M1, M2, M5 and M11, the failing test comes first.

That plan was superseded twice:

1. **M8 and M9 — the AWS milestones — were cut without being started.** The product ran
   locally end to end; the AWS work was infrastructure translation, not product. The decision
   was to go deeper on reliability instead of wider on hosting. `infra/aws/` does not exist.
2. **An 8-checkpoint plan replaced the back half** (agreed as a unit, superseding both an
   earlier 6-item list and the remaining M-numbers). Two of those checkpoints, CP2 and CP3,
   were **inserted** mid-plan for a reason worth recording — see
   [§4.6](#46-two-checkpoints-that-had-to-be-inserted).

## 4.2 M1–M7: getting to a working deploy

| Milestone | What it built | Recorded evidence **[verified]** |
|---|---|---|
| **M1** | The kernel: roles, `vd_user_id()`, `set_config` identity, grants. | 40 tests. Non-vacuity: planting `USING (true)` in a policy fails **18** of them. |
| **M2** | The identity shims — middleware that puts the user id into the transaction. | A concurrency test driving **500 concurrent requests × 5 users × 10 runs** through PgBouncer in transaction mode. |
| **M3 / M4** | Introspection (real tables, columns, FKs, a stable `schema_hash`) and derivation (which template fits which table). | — |
| **M5** | The five policy templates and the attack suite. | A clean run is **196 checks** on the `flat_todo` fixture and **520** on `crm_chain`. All **11 planted bugs** are caught. |
| **M6** | The sidecar / login gate. | — |
| **M7** | The control plane, the console, and the deployment pipeline. | — |

The non-vacuity numbers in that table are the thing I would point at first. "40 tests pass"
is not evidence. "Planting `USING (true)` — the single most plausible mistake in the whole
system — makes 18 of those 40 fail" is evidence that the 40 are looking at something.

## 4.3 M10–M14: the agent gateway

Built after the deploy half worked, and built on top of it rather than beside it: the gateway
authenticates as the `agent` role and inherits the same policies. This half includes the MCP
server, the dry-run/risk/approval/execute/undo/audit flow, the console split (console and
gateway as separate processes because of what each may read), and a scripted local demo.

It is **finished and tested** and then **demoted to the last section of the README**.
**[verified — recorded explicitly, along with "do not delete it".]** The reasoning:
the deploy-and-prove half is the stronger, more legible product; the agent half is a
demonstration that the same enforcement generalises. Leading with the agent story would
invite the question "so it's an AI product?" and the answer is no — it's a Postgres product
with an AI-shaped consumer.

## 4.4 M7.5: the slices that made it real

A set of mid-plan slices, each one replacing a convenient fiction with the real thing. These
are where most of the interesting bugs came from.

| Slice | Fiction replaced | Consequence |
|---|---|---|
| **1** | fixtures → **real git repositories** | Clone, resolve a commit, deploy from it. |
| **1b** | → **plain folder deploys** | A deploy source that isn't git at all. |
| **1c** | → **builder environment variables** | Surfaced the `.env` exclusion as a security fix ([§3.19](#319-builder-environment-variables-with-env-excluded)). |
| **2** | subprocess → **the real container** | Fixed a latent and serious bug: `scratch_dsn` was pointing at the **customer database** ([§5.5](#55-the-scratch-dsn-pointed-at-the-customer-database)). |
| **2b** | plain `.sql` only → **Alembic** | 19/19 new tests, full suite green. Also exposed the second-deploy bug ([§5.14](#514-no-app-with-plain-sql-migrations-could-be-deployed-twice)). |

Slice 2 is the one I would highlight. Moving migrations from a subprocess into a real
container was planned as an isolation improvement. It *also* revealed that the DSN handed to
the migration step was the wrong database — a mistake that the subprocess implementation had
been quietly tolerating because the subprocess already had the credentials either way.
Tightening an isolation boundary made a pre-existing misconfiguration fail loudly. That is
the strongest argument I have for doing isolation work even when nothing appears broken.

## 4.5 The scope reset: deleting working code

A **Node/Prisma identity shim existed and was deleted.** Node apps are now deployed and
marked "Unprotected". The journal is blunt about it: *don't rebuild it*, it is explicitly out
of V0.5, and commit `bfe65ec` exists only to snapshot the pre-reset tree.

**Reasoning:** the shim is the component that guarantees identity reaches the transaction. One
language's shim is a deep, adversarially-tested thing (see the 500-concurrent-request test).
Two languages' shims is two of those, and the second one was going to be maintained at half
the attention. A half-attention shim on a security path is worse than no shim, because
"Unprotected" is an honest label and a weak shim is a false one.

Deleting tested, working code because it widened a security surface is the decision in this
project I am most confident was correct and found hardest to make.

## 4.6 Two checkpoints that had to be inserted

The 8-checkpoint plan was supposed to go straight from leases to blue-green. It couldn't, and
the reason is a good example of a plan colliding with the code:

- **Blue-green needs a stable address.** At that point, "live" meant *a uvicorn process on a
  random port*. There is nothing to flip traffic between if the address changes every time
  the process starts. So **CP3 — Caddy in front of apps** had to come first.
- **Blue-green needs the fence to cover external resources.** Flipping traffic is an external
  operation, so the fence protecting it must be too. So **CP2 — fencing past the database**
  had to come first.

And CP4 and CP5 are one feature split in two by *what each proves*: CP4 implements the
health-gated cutover, CP5 measures it. Keeping them apart is specifically what stops "zero
downtime" from being an assertion. If they were one checkpoint, the measurement would have
been written by someone who already believed the answer.

## 4.7 The checkpoint record **[verified]**

Each checkpoint's "done when" was written before the work and each has a recorded
non-vacuity proof — the exact number of tests that fail when the feature is sabotaged.

| # | Checkpoint | Status | Non-vacuity proof |
|---|---|---|---|
| 0 | Alembic slice | done | 19/19 + full suite green |
| 1 | Leases + fencing | done | lease expiry reclaims; a zombie's write is fence-rejected. Deleting the fence check fails **5**. |
| CP1 | HMAC webhook + idempotency | done | `return True` in `verify()` fails exactly **4**; randomising `delivery_id` fails exactly **2** |
| CP2 | Fencing past the DB | done | dropping `desired_generation` fails exactly **4**; making `keep` a scalar (`one < keep`) fails exactly **2** |
| CP3 | Caddy in front of apps | done | re-adding a raw-port fallback fails exactly **3**; making `proxy.require` a no-op fails exactly **1** |
| CP4 | Blue-green behind a health gate | done | 3 planted bugs → exactly **1 / 1 / 1** |
| CP5 | Measure the cutover | done | recorded numbers in [§6.3](#63-the-cutover-measurement) |
| CP6 | Reconciliation controller | done | 18 tests; 3 planted bugs → exactly **1 / 1 / 3** |
| CP7 | Chaos suite → `EVIDENCE.md` | **not done** | — |
| CP8 | README + demo video + doc debts | **not done** | — |

The discipline of writing down *exactly* how many tests a given sabotage breaks is the single
practice from this project I would carry to any other. It turns "we have tests" into a
falsifiable statement, and it catches the case where a test passes for the wrong reason.

## 4.8 The production-hardening phases

A later plan, in five phases:

- **Phase A — operator auth.** Done. argon2, sessions, setup page, bootstrap operator.
- **Phase B — secrets and config hardening.** Done. `VD_ENV=prod`, encrypted secrets in the
  state root, `VD_SECRET_KEY_FILE` as an alternative to an env var so the key need not appear
  in a process listing or a `docker inspect`.
- **Phase C — containerised app runtime.** Done. `worker/runtimes/apprt.py` — two containers
  per generation sharing one network namespace that the login gate owns — **is now the default
  runtime.** The older bare-process runtime (`local.py`) survives only until the Linux run
  passes, then it goes.
- **Phase D — production packaging.** Written. `infra/prod/` (Dockerfile, compose file,
  Caddyfile, env template), `VD_MIGRATE_ON_BOOT` under a `pg_advisory_lock`, `/health` that
  answers for the database rather than for the process, `public_url` and the Caddy listener
  following `VD_PUBLIC_SCHEME`, the console routed through the same Caddy via
  `VD_CONSOLE_DOMAIN`, `VD_BUILD_NETWORK` separated from `VD_APP_NETWORK`, and real
  `dependencies` in `pyproject.toml`.
- **Phase E — chaos (= CP7).** Not started.

The network separation in Phase D is a small decision with a clear rationale:
`VD_APP_NETWORK` decides *who can reach an app*; `VD_BUILD_NETWORK` decides *what an app's own
untrusted migrations can reach*. They happen to be the same network today, only because the
throwaway database has a name on it and nothing else the build job could reach is worth
anything to it. Separate variables mean the day that stops being true is a config change
rather than a refactor.

## 4.9 Current state, honestly **[verified]**

- **Suite: 618 collected, 618 passed, 0 failed.** Split: 604 in 18m59s, plus 14 concurrency
  tests in 1m36s, run separately (see [§5.20](#520-two-pytest-areas-truncate-the-same-database)).
- The production image **builds and has been audited locally** — every entrypoint imports,
  `compileall` is clean, pins match the green virtualenv, compose interpolation is complete,
  only Caddy publishes ports, the socket is on the worker alone.
- **It has never run on real Linux.** `VD_RUNTIME=apprt` is the production default and has
  only ever run under Docker Desktop, which masks exactly the differences that break it
  (`host.docker.internal` existing, loopback reachability from the bridge).
- CP7 and CP8 are outstanding. `EVIDENCE.md` does not exist yet.
- One known bug is open and unfixed: [§5.19](#519-stopping-the-worker-stops-every-app-it-started).
- One spec conflict is open and **flagged rather than hidden**: `Readme.md` §15 says the git
  clone happens in the build job; the clone still happens in the worker. The project rule is
  that `Readme.md` wins and conflicts get flagged, so it is written down in the journal
  instead of being quietly resolved in either direction.

---

# 5. Technical challenges: the bugs that actually happened

Every entry below is a real failure recorded in `memory/gotchas.md` at the time it was found,
or observed directly in a run. None were composed for this document. Each is given as
**symptom → root cause → how it was found → fix → lesson**, because the symptom is almost
never where the bug is and that gap is the interesting part.

**[unknown]** How long each took to diagnose. The journal records causes and fixes, not
stopwatch times.

## 5.1 The SQL renderer substituted variables inside `--` comments

**Symptom.** Generated SQL was malformed or carried values into places they had no business
being, in files that looked correct in the source template.

**Root cause.** `kernel/render.py` substitutes `{word}` placeholders across the whole template
text. It has no concept of SQL lexical structure, so a `{word}` inside a `-- comment` was
substituted exactly like one inside a predicate.

**How it was found.** Inspecting generated SQL. **[inferred — the journal records the cause
and the rule, not the discovery path.]**

**Fix.** The rule, recorded as a standing constraint: do not write anything that looks like a
placeholder inside a comment in a SQL template.

**Lesson.** A templating layer that does not parse its target language is a text processor,
and it is wise to document that out loud rather than let the next person assume it is
smarter. There was a choice here — make the renderer comment-aware, or forbid the pattern —
and for a security-critical code path, *less* machinery with a documented constraint beats
more machinery with a subtle one.

## 5.2 `DROP ROLE` fails until you `DROP OWNED BY`

**Symptom.** Teardown between tests failed: a role could not be dropped.

**Root cause.** Postgres refuses to drop a role that still owns objects or holds privileges
anywhere in the cluster, and the error does not enumerate them.

**Fix.** `DROP OWNED BY <role>` before `DROP ROLE <role>`.

**Lesson.** This is a small thing that matters out of proportion to its size, because it sits
in **teardown**. A teardown that half-fails leaves a dirty cluster, and the *next* test fails
with a confusing error in an unrelated place. Several of the hardest-to-read failures in this
project were second-order effects of incomplete cleanup, which is why fixture teardown
eventually got treated as production code.

## 5.3 `BYPASSRLS` and `SET role = owner` are mutually exclusive — a spec conflict

**Symptom.** The behaviour the spec described for the `migrator` role could not be
implemented as written.

**Root cause.** `Readme.md` §9.1 described both giving `migrator` `BYPASSRLS` *and* having it
`SET role = owner`. Those are two different mechanisms for the same goal and they interact
badly: `SET role` switches the current role, which discards the `BYPASSRLS` attribute of the
role you authenticated as.

**Fix.** `BYPASSRLS` was kept; **`SET role` was deliberately omitted.** The conflict with the
spec was written into the gotchas journal rather than silently resolved.

**Lesson.** The project rule is "`Readme.md` wins; if the code must differ, flag the
conflict." The temptation in the moment is to just implement the thing that works and move
on. That produces a spec that quietly stops describing the system, and six weeks later the
spec is actively misleading. Writing the divergence down costs a paragraph and preserves the
document's value.

## 5.4 `host.docker.internal` is a Docker Desktop property, not a Docker one

**Symptom.** Addresses that resolve fine on the development machine will not resolve on a
Linux host, and loopback-bound ports are unreachable from the bridge network.

**Root cause.** `host.docker.internal` is provided by Docker Desktop's VM networking. On a
plain Linux Docker installation it does not exist, and a port bound to `127.0.0.1` on the host
is not reachable from a container on the bridge regardless.

**How it was found.** Reasoning during the container work, recorded as a standing hazard
before it could cause an outage. **[verified — the gotchas entry predates any Linux run.]**

**Fix.** Every inter-process address in production is a **compose service name**, not a
loopback address: `http://control-plane:8100`, `http://gateway:8200`, `http://caddy:2019`. A
`VD_CONTAINER_HOST` escape hatch exists for an address that genuinely cannot have a name, and
the env template says so and says it should not be needed.

**Lesson.** The development platform's conveniences are the production environment's absences.
This one is still an open risk — see [§4.9](#49-current-state-honestly) — because the apprt
runtime has never actually run on Linux, and this is precisely the class of difference that
will bite.

## 5.5 The scratch DSN pointed at the customer database

**Symptom.** None visible. Everything worked.

**Root cause.** The DSN handed to the migration step (`scratch_dsn`) was pointing at the
**customer** Postgres instance rather than the throwaway one. Since the migration step was
running as a subprocess inside the worker at the time, it had access to the customer
credentials either way, so nothing failed and nothing looked wrong.

**How it was found.** By moving the build job into a real container (M7.5 slice 2). The
container is handed only the credentials it is meant to have, so the misconfiguration stopped
being invisible.

**Fix.** The build job runs against `buildpg` with its own credentials
([§3.6](#36-why-the-build-database-is-a-separate-instance-not-a-separate-schema)).

**Lesson.** This is the most instructive bug in the project. **An isolation boundary is also a
detector.** The subprocess implementation was not just less safe in principle — it was
actively concealing a live misconfiguration, because a process with every credential cannot
tell you it used the wrong one. Tightening the boundary converted a silent security bug into
a loud configuration error. I would now expect every isolation improvement to surface at
least one latent mistake, and treat it as suspicious if it doesn't.

## 5.6 Introspection produced an unstable `schema_hash`

**Symptom.** The `schema_hash` for an unchanged schema differed between runs.

**Root cause.** Introspection queries resolved object names through the session's
`search_path`, which was not pinned. Different connection states produced different
resolutions and therefore different hashes.

**Fix.** Introspection pins `search_path` explicitly.

**Lesson.** A hash used as an identity must be computed from a canonical form, and
"canonical" includes the *resolution context*, not just the bytes. This matters beyond
aesthetics: `schema_hash` is what the evidence report names, so an unstable hash makes every
saved report unverifiable. A fingerprint that changes when nothing changed is worse than no
fingerprint, because people stop trusting the ones that do change.

## 5.7 `Readme.md` §14 said to patch `Starlette.__init__` — it does not reach FastAPI

**Symptom.** The identity middleware, installed exactly as the spec described, was not
applied in a FastAPI application.

**Root cause.** FastAPI subclasses Starlette and does not route its own initialisation
through the path the spec assumed. Patching `Starlette.__init__` therefore misses FastAPI
apps entirely.

**How it was found.** The middleware demonstrably not running.

**Fix.** Patch `build_middleware_stack` **on both** classes.

**Lesson.** Two lessons, and the second is the important one.

First: when you monkey-patch a framework, patch the point where the behaviour is *assembled*,
not where the object is *constructed*. Construction has many entry points; assembly usually
has one.

Second, and more serious: **a security middleware that fails to install is a silent total
failure.** The app works. The requests succeed. Nothing is protected. The only reason this
was caught is that the test suite checks for the *presence of enforcement* rather than for
successful requests. If the shim's tests had been "the app responds 200", this bug ships.

## 5.8 FastAPI runs `yield`-dependency teardown *after* the response reaches the client

**Symptom.** Two separate, apparently unrelated bugs:

1. The build job's callback got an HTTP `204`, and then a read of the deployment showed it
   still in `verifying`.
2. `POST /apps` returned `303`, the browser followed the redirect, and the resulting `GET`
   returned `404` for the app that had just been created.

**Root cause.** One cause for both. The database session was a `yield` dependency and the
commit happened in its teardown. FastAPI runs that teardown **after** the response has been
sent. So the client — the build job in the first case, the browser in the second — issues its
next request in the window between "response sent" and "transaction committed", and reads a
world in which the write has not happened.

**How it was found.** Both symptoms were initially read as application-logic bugs ("the status
isn't being set", "the app isn't being created"). The link was the timing: the data was always
correct a moment later.

**Fix.** Commit explicitly before returning the response, rather than relying on dependency
teardown.

**Lesson.** This is the best bug in the project to think about, because **commit-on-teardown
is a correctness bug that cannot be found by looking at either piece of code.** The route
handler is correct. The dependency is correct. The framework is behaving as documented. The
bug lives in the ordering guarantee between two layers, and it only manifests when a client
is fast enough to come back before the teardown runs — which means it is load-dependent, and
a slower machine hides it.

The generalisation I took from it: **a response is a promise that the work is durable.** If
you send it before the commit, you have lied, and the client is entitled to act on the lie
immediately. Any "write then redirect" or "write then callback" flow should be audited for
this specific pattern.

## 5.9 Three small, expensive harness bugs

Grouped because each is a one-line fix and each cost real time by producing a failure that
pointed somewhere else.

**`pg_policy.polcmd` returns `bytes`.** The structural checks read policy metadata out of the
catalog, and comparing a `bytes` value (`b'r'`) to a `str` (`'r'`) is silently `False` in
Python. A structural check comparing the wrong types doesn't error — it just never matches,
so **every policy looks wrong, or every policy looks absent.** A verifier whose checks
silently never match is the most dangerous possible state for this project, because it fails
*open* in the only place that matters. This is the strongest argument for the planted-bug
discipline: a test that only ever asserts "the verifier passed" cannot distinguish a working
verifier from a blind one.

**pytest-asyncio needs *both* loop-scope options set.** Setting one and not the other
produces event-loop errors in tests that have nothing to do with event loops.

**Parametrising over the runtime/agent role pair requires a plain `@pytest.fixture`.** The
runtime/agent parity suite parametrises over which role it connects as, and the fixture
providing the connection cannot be an async-generator fixture in the way first attempted. The
symptom was failures in the parity tests specifically — i.e. in the one place the project
most needs to be able to trust.

**Lesson for all three.** Time spent making the test harness boringly correct is not overhead;
it is the only thing that makes every later failure legible. Every hour in this project spent
debugging the harness instead of the system was an hour the harness had been allowed to be
clever.

## 5.10 Postgres applies the SELECT policy to UPDATE with a `WHERE` clause

**Symptom.** Two attack probes — `update_other` and `delete_other` — could never fire. They
were dead code that always reported "blocked".

**Root cause.** Real Postgres semantics, not a bug in Postgres. An `UPDATE ... WHERE id = 7`
must first *find* row 7, and finding it goes through the **SELECT** policy. If the SELECT
policy is correct, the row is invisible, the `UPDATE` matches zero rows, and the probe
concludes "the UPDATE was blocked" — when in truth the UPDATE policy was never consulted at
all.

**How it was found.** By trying to plant a bug in the UPDATE policy and watching the
`update_other` probe stay green.

**Fix.** The probe's limitation is documented, and the UPDATE and DELETE policies are verified
by the **structural** checks instead — the behavioural probe cannot reach them while SELECT is
correct.

**Lesson.** This is the single most important testing insight in the project. **A behavioural
test can be shadowed by a different, correct control.** The probe wasn't wrong about what it
observed; it was wrong about what its observation *implied*. And the only way this was
discovered was by deliberately breaking the thing the probe claimed to test.

It also justifies the hybrid verification design. Behavioural probes prove the system does the
right thing for real connections; structural checks prove the configuration is what it should
be even where behaviour cannot distinguish. Either alone has a blind spot, and this bug is the
proof.

## 5.11 A missing `WITH CHECK` on UPDATE is behaviourally invisible

**Symptom.** None. Nothing fails.

**Root cause.** When an UPDATE policy has `USING` but no `WITH CHECK`, Postgres **reuses the
`USING` expression** for the check. So a policy missing its `WITH CHECK` behaves identically to
a correct one for every input you can construct — until the day the two expressions should
have differed, which is exactly the case where `WITH CHECK` matters (preventing a user from
updating *their own* row into *someone else's* ownership).

**How it was found.** Planting it as a deliberate bug and finding that the behavioural suite
did not care. **[verified]**

**Fix.** A structural check asserts that every policy has **both** clauses, explicitly.

**Lesson.** A sensible default in a database engine can erase a class of misconfiguration from
observable behaviour. There is no amount of black-box testing that finds this. The structural
check exists *only* because someone tried to plant a bug and the suite shrugged — which is a
direct argument for making planted-bug testing a routine practice rather than a one-off
validation exercise.

## 5.12 An insert failing for the wrong reason was counted as a pass

**Symptom.** An attack probe reported "blocked" when the insert had actually failed on a
`NOT NULL` violation, a type error, or a missing column.

**Root cause.** The probe treated *any* exception as evidence of enforcement. "The database
said no" is not the same claim as "RLS said no."

**Fix.** Only SQLSTATE **`42501`** (`insufficient_privilege`) counts as a pass. Any other
error is a **failure**, not a pass, because it means the probe never tested what it claimed
to.

**Lesson.** The generalisable rule: **a negative test must assert the reason, not just the
outcome.** A probe that accepts any failure is an unconditional pass waiting for a schema
change — rename a column and the probe keeps reporting "blocked" forever, having tested
nothing. This is the same failure mode as a `try/except: pass` around an assertion, dressed up
as a security control.

## 5.13 The negative control that refused to leak

**Symptom.** The deliberately-unsafe M2 negative control — the version with no identity shim,
which is *supposed* to leak rows across users — did not reliably leak.

**Root cause.** Without a `commit()`, the writes sat in uncommitted transactions and other
sessions couldn't see them. The absence of a leak was an artifact of transaction visibility,
not evidence that the unsafe code was safe.

**Fix.** The negative control commits, so that the leak it is demonstrating can actually be
observed.

**Lesson.** **A negative control that doesn't fail is as broken as a test that doesn't pass,
and much harder to notice**, because the output looks like good news. This one is subtler than
it sounds: the bug was in the *control*, which meant the real test's credibility was resting
on a comparison against a baseline that wasn't measuring anything.

(A related instinct misfired later in this project and is recorded in
[§10.6](#106-read-the-test-before-declaring-it-broken) — I flagged a passing negative control
as broken when it was passing by design.)

## 5.14 No app with plain `.sql` migrations could be deployed twice

**Symptom.** A second deploy of an app with plain `.sql` migrations failed. The first deploy
worked perfectly.

**Root cause.** Nothing recorded which migrations had already run. The deploy re-executed
`001_init.sql`, which does `CREATE TABLE`, against a schema that already had the table.

**How it was found.** **By the CP4 blue-green work.** Blue-green is the first feature that
requires deploying the *same app twice* — that is literally what it does. Until then every
test deployed a fresh app, so the entire product worked only for first deploys and nobody
knew. **[verified — recorded in the progress journal as a bug surfaced by CP4.]**

**Fix.** The per-app ledger schema `app_<id>_vd`, holding `vd_applied_migrations`. Not a table
in the app schema, for the reason in [§2.5](#25-two-schemas-per-app-and-why): everything in
the app schema is introspected, policied and attacked, and a by-name exception is a hiding
place.

**Lesson.** This is my favourite "the tests were green and the product was broken" story,
because the gap was not in the depth of testing but in its *shape*. Every test did one deploy.
The suite was large and thorough and uniformly blind to the second-deploy case. What found it
was a feature whose definition forced a different usage pattern.

The transferable version: **look for the axis your tests are constant along.** Count of
deploys was that axis here. It is worth periodically asking "what value is the same in every
single test?" — because that value is untested, no matter how many tests there are.

## 5.15 PgBouncer needed `auth_query` and an `auth_user`

**Symptom.** Connections through PgBouncer failed to authenticate, with a message that points
at credentials rather than at configuration.

**Root cause.** PgBouncer does not know about roles created after it started. With per-app
roles being created on the fly, a static `userlist.txt` is structurally wrong — the whole
point is that new roles appear constantly.

**Fix.** Configure `auth_query` with an `auth_user` so PgBouncer looks credentials up in
Postgres at connection time.

**Lesson.** A component configured from a static file cannot sit in front of a system whose
principals are dynamic. This is the kind of impedance mismatch that only shows up when you
combine two reasonable things, and it is a decent argument for the choice in
[§3.24](#324-pgbouncer-is-in-the-local-stack-and-deliberately-absent-in-production) — the
pooler earns its place as a test fixture and nowhere else.

## 5.16 The port Hyper-V had already reserved

**Symptom.** PgBouncer could not bind `56432` on the development machine. Intermittent and
confusing, since nothing was listening there.

**Root cause.** Windows/Hyper-V reserves a **dynamic port exclusion range**, and on this
machine it covered **55834–56433**. A port inside that range cannot be bound even though
`netstat` shows nothing using it, because the reservation is not a listener.

**Fix.** PgBouncer moved to **`55433`**, below the range.

**Lesson.** "Nothing is listening on this port" and "this port is available" are different
statements on Windows. More generally: when a bind fails and the port looks free, suspect the
OS before suspecting your code. The whole local port map was then chosen to sit in one
contiguous, documented block — Postgres `55432`, PgBouncer `55433`, buildpg `55434`, Caddy
`55080`, Caddy admin `55019` — so the next collision is obvious.

## 5.17 `os.kill(pid, 0)` terminates the process on Windows

**Symptom.** Checking whether the old generation had exited **killed it**.

**Root cause.** On POSIX, signal `0` is the documented no-op liveness probe: it performs
permission and existence checks and sends nothing. On Windows, Python's `os.kill` has no
real signal layer — it is implemented in terms of `TerminateProcess`, so passing `0`
terminates the target.

**How it was found.** During the CP4 drain work, where the sequence is explicitly *drain →
SIGTERM → verify exit*. The verification step was ending the process it was supposed to be
observing, which corrupts the measurement in the most direct way possible: the thing you are
timing is caused by your timer.

**Fix.** **Verified exit is measured by the port going quiet, never by a pid.** The check
attempts a TCP connect and waits for it to stop succeeding.

**Lesson.** Two things. First, the portability trap: a POSIX idiom that is *so* standard it
reads as a no-op is not one everywhere, and `os.kill(pid, 0)` is the canonical example.

Second, and more interesting: **the fix is better than the thing it replaced, on its own
merits.** A pid being alive does not mean a process is still serving — it could be in
teardown, holding no sockets. A port being quiet is a statement about the property you
actually care about (can a client still connect?), observed from the outside, with no
platform-specific API involved. The bug forced a measurement that is both portable and more
truthful. When a platform quirk pushes you toward observing the externally-visible property
instead of the internal one, that is usually the right direction anyway.

## 5.18 Caddy's admin API does not create intermediate objects

**Symptom.** POSTing the route table to a freshly-started, empty Caddy returned **HTTP 500
`invalid traversal path`**.

**Root cause.** Caddy's admin API traverses its config as a JSON tree. If you POST to a path
under `apps/http/servers/...` and the `apps` key does not exist, it does not create the
intermediate objects — it refuses the traversal. A long-running Caddy that has had a config
written once already has those keys, so everything works.

**How it was found.** **A 533-test suite missed this entirely**, because every test shared one
long-running Caddy container that had already been configured. It only appeared against a
Caddy that had been restarted empty — i.e. the state of every real deployment's first-ever
publish, and of every restart after a host reboot.

**Fix.** Handle the empty-config case when publishing, and pin it with a dedicated test:
`test_a_proxy_that_was_restarted_empty_can_still_be_published_into`. Plus a hard rule: **never
`POST /config/`** (which replaces the entire configuration rather than a subtree).

**Lesson.** The most valuable lesson in the deployment half of this project: **shared, long-lived
test fixtures hide initialisation bugs.** The fixture was efficient and correct-looking, and it
systematically prevented the suite from ever observing a cold start. Since a cold start is the
state of every real first deploy and every post-reboot recovery, the suite was blind to the
most common real-world condition.

The counter-practice: for anything that writes configuration into an external system, at least
one test must run against an instance that has **never** been written to. Not a reset one — a
virgin one.

## 5.19 Stopping the worker stops every app it started — **OPEN, UNFIXED**

**Symptom.** Stopping the worker takes down every running application. Worse, the deployment
state on disk survives, so the route stays published and the app's public address returns
**502** instead of going away cleanly.

**Root cause.** Under the local bare-process runtime, apps are started as **child processes of
the worker**. Killing the parent kills the children.

**Why the suite does not catch it.** The CP6 reconciler tests simulate a worker crash with an
in-process `Killed(BaseException)` — raising an exception inside the worker's own loop. That
simulates the control-flow consequences of a crash faithfully, but the *process* never dies,
so the children survive. The test suite's model of "the worker died" is precisely the model in
which this bug does not exist.

There is a second-order consequence recorded with it:
`test_a_live_app_with_nothing_running_is_not_published` only holds when the deployment's run
record is **absent**. A dead generation that left its record on disk is still considered
publishable, which is how the 502 persists rather than the route being withdrawn.

**Status.** Found, written down, **not fixed.** The containerised runtime
(`worker/runtimes/apprt.py`) does not have the problem — containers are children of the Docker
daemon, not of the worker — and it is now the production default. So the bug's blast radius is
the legacy local runtime, which is scheduled for deletion once the Linux run passes.

**Why it is in this document.** Because the honest version of this project includes the bugs
that are known and open. It is also a good example of a **test-model bug rather than a code
bug**: the suite was thorough, the simulation was reasonable, and the gap was in the
*fidelity* of the failure being simulated. In-process exception injection cannot find
process-tree problems, by construction.

## 5.20 Two pytest areas truncate the same database

**Symptom.** `6 failed, 155 passed, 8 errors` — a cascade of unrelated-looking failures across
the pipeline suite.

**Root cause.** `tests/orchestration/conftest.py` truncates the `vibedeploy_pipeline_test`
database, which is the same database the pipeline tests are using. Running the two areas
concurrently means one suite deletes the other's fixtures mid-test.

**Fix.** A rule rather than a code change: **never run two pytest areas at once.** The
concurrency suite is likewise always run separately — it is slow and heavily contended, and
mixing it in distorts both.

**Lesson.** Tests that share mutable external state are not independent regardless of how
independent their code is. The specific trap: the failures appear in the *victim* suite, which
looks like a regression in the code under test. Every minute spent looking at the pipeline
code was a minute spent looking at innocent code. The giveaway was the *pattern* — errors in
setup, failures scattered without a common theme — which is almost always environmental.

## 5.21 The production image had no `docker` binary — found before the first deploy

**Symptom.** Would have been: **every deploy on the production box fails at the first container
start**, surfacing as "the deployment pipeline is broken" rather than "the image is wrong."

**Root cause.** The Dockerfile installed `docker.io`. On **Debian 13** (the base of
`python:3.12-slim`), the Docker **client was split into a separate `docker-cli` package**.
`docker.io` now installs the daemon-side pieces and `docker-init` — and **not**
`/usr/bin/docker`. `dpkg -l` cheerfully reported `docker.io 26.1.5+dfsg1-9+deb13u1` as
installed; `command -v docker` found nothing; the only thing present was
`/usr/bin/docker-init`, a symlink to tini-static.

`worker/pipeline/container.py:45` is:

```python
DOCKER = os.environ.get("VD_DOCKER", "docker")
```

The worker shells out to that binary for the build job and for every app container. With no
binary, nothing deploys.

**How it was found.** By building and *auditing* the production image locally before provisioning
the server: checking that the CLI existed, that every entrypoint imports, and that
`compileall` is clean. The build itself succeeded — `apt-get install` returned 0, because the
package it was asked for does exist and did install.

**Fix.** `docker.io` → `docker-cli`, with the reasoning written into the Dockerfile as a
comment so nobody "simplifies" it back. Verified afterwards: `Docker version 26.1.5+dfsg1`. A
grep confirmed only `infra/prod/Dockerfile` had the pattern — the local buildjob and apprt
images don't need a client.

**Lesson.** Three.

1. **A green build is not a working image.** `apt-get install` exiting 0 says a package was
   installed, not that a binary you need is on the PATH. The useful verification is to assert
   the *capability* (`command -v docker`), not the installation.
2. **Base image upgrades move packages around.** Debian splitting a client out of a
   metapackage is routine and invisible until something shells out.
3. **Stage 0 of a deployment plan should be "build and audit the artifact locally."** That
   stage cost minutes and caught a bug whose alternative discovery path was a failed deploy on
   a fresh server, where every other variable is also unproven and the error would have been
   attributed to the new environment.

## 5.22 Git Bash rewrote a container path into a Windows path

**Symptom.** `docker run -v ...:/src` produced a mount at `C:/Program Files/Git/src`.

**Root cause.** MSYS/Git Bash path conversion: an argument that looks like a Unix absolute path
gets translated to a Windows path on the way to a native executable. `/src` became
`C:/Program Files/Git/src`.

**Fix.** `MSYS_NO_PATHCONV=1` on `docker` invocations from Git Bash.

**Lesson.** The shell can modify your arguments before the program sees them. When a path is
wrong in a way no code in your repository could have produced — and `C:/Program Files/Git/`
prefixed to a container path is exactly that — the mangling happened outside your program.

## 5.23 Ninety "errors" that were all the same read-only mount

**Symptom.** Running `compileall` inside the production image produced roughly **90
`OSError: [Errno 30] Read-only file system`** entries. At a glance: ninety broken files.

**Root cause.** None of them were compile errors. `compileall` was successfully compiling each
file and then failing to *write* `__pycache__` into the repository mount, which is mounted
`:ro` by design ([§3.22](#322-code-is-mounted-into-the-production-image-not-copied)).

**Fix.** `-e PYTHONPYCACHEPREFIX=/tmp/pyc`. Exit code 0, no errors.

**Lesson.** Read the exception type before counting the occurrences. `OSError` on a write is
not a `SyntaxError`, and ninety identical errors are one error. The deeper point is that a
read-only mount is a deliberate constraint and tooling will protest about it in ways that
mimic real failures — so the tooling needs to be told where to put its scratch output, not
the mount loosened.

## 5.24 The demo kept breaking because truncation deleted the kill switch

**Symptom.** The demo path broke after a database reset.

**Root cause.** `_truncate` empties tables including `global_settings`. The kill switch
**fails closed when its row is missing** — a deliberate design choice
([§3.7](#37-fail-closed-as-a-rule-with-no-exceptions)) — so a truncated database has the kill
switch effectively ON and nothing works.

**Fix.** `_truncate` must **re-seed** `global_settings`.

**Lesson.** A nice illustration that fail-closed has a cost and the cost is paid in developer
experience. The correct response is to pay it: fix the fixture, not the default. Any time a
test helper wipes state, it must restore the rows the system treats as "absent means deny" —
otherwise every later failure is a fail-closed decision working exactly as designed, which is
maximally confusing because nothing is wrong.

## 5.25 A fresh named volume takes ownership from the image's directory

**Symptom.** Permission behaviour changed depending on whether a volume was new.

**Root cause.** Docker initialises a **new, empty** named volume by copying the content *and
ownership* of the image's directory at that mount point. An existing volume is mounted as-is.
So the same compose file behaves differently on a first run than on a subsequent one.

**Lesson.** Relevant to the production design, where all three platform processes run as uid
10000 and all three write into the shared state root — which is why the env template tells the
operator to `chown -R 10000:10000` the state root *before the first `up`*, rather than hoping
the image's ownership is inherited. "Works on the second run" is a category of bug that this
mechanic creates, and it is worth knowing about specifically because the first run is the one
a stranger does.

## 5.26 SQLAlchemy 2.1 changed what a bare `postgresql://` URL means

**Symptom.** A DSN that worked stopped working after a version change.

**Root cause.** SQLAlchemy 2.1 changed the default driver for a bare `postgresql://` URL from
**psycopg2** to **psycopg 3**.

**Lesson.** This is a direct argument for
[§3.25](#325-exact-version-pins-in-the-image-floors-in-pyprojecttoml). A DSN string is
configuration that *looks* inert and is actually a driver selection. Where the driver matters,
name it explicitly (`postgresql+asyncpg://`) — which is what the production env template does
for every DSN it contains.

## 5.27 Keeping secrets out of `argv`

Not a bug — a hazard noticed and designed around, recorded alongside the bugs because it
belongs to the same work.

Passing a secret as `docker run --env NAME=value` puts the value in the command line, where it
is visible to `ps` for anyone on the host and gets captured by anything that logs commands.

**The technique:** `docker run --env NAME` with **no `=`** tells Docker to forward the variable
from the *parent's own environment*. The value never appears in `argv`.

**Lesson.** Worth knowing because the safe form is less obvious than the unsafe one, and
because "how does this value travel" is a question worth asking separately from "is this value
encrypted." A secret that is encrypted at rest and then shouted into a process listing is not
protected.

## 5.28 The worker logs nowhere — **OPEN**

**Status.** Recorded as a known deficiency, not fixed.

Everything the worker does — claiming jobs, starting containers, health gating, publishing
routes, reconciling drift — produces no durable log. Diagnosis depends on database state and
on re-running things.

**Why it matters more than it sounds.** This is the one deficiency that makes every *other*
bug in this document more expensive. Several entries above were found by reasoning backwards
from a wrong database row, which is exactly the work a log would have done. It is the highest
value-per-effort item outstanding, and the reason it is still open is simply that it was never
a checkpoint's "done when" — a small, instructive failure of plan-driven development: **work
that isn't in a checkpoint doesn't happen, so operability has to be somebody's checkpoint.**

## 5.29 Laptop sleep produces garbage timings

**Symptom.** A `docker run` measured at **156 minutes** of wall time with **0.2 seconds of
user time**.

**Root cause.** The host slept mid-operation. Wall-clock measurement includes the sleep;
nothing was running for almost all of it.

**Lesson.** Recorded as a standing rule for this project: a slow run is not a regression until
you have ruled out host contention and suspend. Full-suite wall time legitimately varies
between roughly **4 and 28 minutes** depending on what else the machine is doing, and the
first diagnostic is to check for leaked processes and containers rather than to go looking for
a performance bug. Any benchmark taken on a laptop needs a user-time sanity check beside its
wall-clock number — a 156-minute wall time next to 0.2 seconds of CPU is not a slow program.

## 5.30 A long pipeline run saturates PyPI

**Symptom.** Pipeline runs slow down or fail on dependency installation.

**Root cause.** The pipeline suite performs many real deploys, and each deploy installs an
app's dependencies. Enough of them in sequence is enough traffic to hit rate limiting.

**Lesson.** A test suite that does real work is also a real client of other people's
infrastructure, and at some scale that becomes a correctness problem rather than a politeness
one. This is part of what motivated the dependency-volume work in
[§6.1](#61-the-dependency-install-6m12s--2m01s-against-a-15-minute-timeout) — caching
dependencies was a performance fix *and* a way to stop being a bad citizen.

---

# 6. Performance

A short section, deliberately. This project optimised exactly two things, because those were
the only two where a number was blocking something. Everything else was left alone, and that
is a decision rather than an omission.

**[verified]** All numbers below were printed by a run and recorded at the time. There are no
estimates in this section.

## 6.1 The dependency install: 6m12s → ~2m01s, against a 15-minute timeout

**The problem.** An app's dependencies are installed during its build. On Windows, into a
**writable bind mount**, `pip install --target` took **6 minutes 12 seconds**.

**Why that was blocking, not merely slow.** The container has a **15-minute timeout**. One
operation consuming 6m12s of a 15-minute budget leaves no room for the rest of the build —
clone, migrations, introspection, policy generation, the attack suite — on a machine under any
contention at all. And the pipeline suite performs many real deploys in sequence, so the
figure multiplies.

**Root cause.** Bind mounts on Docker Desktop for Windows cross a filesystem translation
layer. `pip install --target` is a pathological workload for it: thousands of small file
creations, each paying the crossing cost.

**The fix.** Install into a **named Docker volume** instead of a bind mount. Native
filesystem, no translation.

**Measured result: ~2 minutes 1 second.** Roughly a **3x improvement**, and more importantly it
turned a 41%-of-budget operation into a ~13% one.

**What makes this interesting as an engineering decision**, rather than just a tuning win: the
bind mount existed because the *result* needs to be visible on the host (an app's deployment
checkout gets bind-mounted into the app container, so the host must be able to see it). Moving
to a volume meant separating "where dependencies are installed" from "what the host must
see" — a small design change forced by a performance measurement. The 15-minute timeout is
itself a value chosen *because* of this measurement: it is set where it is to accommodate a
realistic worst case, not picked as a round number.

**[verified]** The 6m12s / ~2m01s pair and the 15-minute timeout are all recorded in the
gotchas journal, with the volume decision recorded in the progress journal.

## 6.2 Test suite runtime

| Area | Tests | Time |
|---|---|---|
| Everything except concurrency | 604 | **18m59s** |
| Concurrency (run separately) | 14 | **1m36s** |
| **Total** | **618** | ~20m35s |

Breakdown by directory **[verified by collection]**:

| Directory | Tests |
|---|---|
| `tests/pipeline` | 174 |
| `tests/orchestration` | 135 |
| `tests/gateway` | 131 |
| `tests/kernel` | 40 |
| `tests/sidecar` | 33 |
| `tests/attack` | 26 |
| `tests/derive` | 26 |
| `tests/shims` | 22 |
| `tests/e2e` | 17 |
| `tests/concurrency` | 14 |

**Where the time goes, and why it was not optimised.** `tests/pipeline` owns the real deploys —
real containers, real `pip install`, real health gates with real sleeps, real drains. An
earlier recorded figure has that directory at **~940 seconds for 165 tests**, i.e. the
overwhelming majority of total suite time. **[verified; the count has since grown to 174 and
the directory has not been re-timed in isolation — so the 940s figure is correct for the run
it came from and is now slightly stale.]**

The decision was to leave it slow. The alternative is mocking the container runtime, the
proxy and the health gate — and those are precisely the components whose real behaviour the
suite exists to verify. [§5.21](#521-the-production-image-had-no-docker-binary--found-before-the-first-deploy),
[§5.18](#518-caddys-admin-api-does-not-create-intermediate-objects) and
[§5.5](#55-the-scratch-dsn-pointed-at-the-customer-database) are all bugs that a mocked suite
would have passed cleanly. A twenty-minute suite that finds those is better than a
ninety-second suite that doesn't.

**Variance.** Total wall time legitimately varies between roughly **4 and 28 minutes** with
host contention. This is documented so that a slow run is not mistaken for a regression; see
[§5.29](#529-laptop-sleep-produces-garbage-timings).

## 6.3 The cutover measurement

The CP5 harness drives steady RPS through a real blue-green flip and records both HTTP status
codes and connection errors. **[verified — both tables are recorded runs, not estimates.]**

**Recorded at the time of CP5:**

| Scenario | Result |
|---|---|
| Normal cutover (`drain_seconds=2`) | **307 requests, all 200** |
| `drain_seconds=0` (negative control) | `{200: 256, 502: 1}` |
| `SIGKILL` instead of drain+SIGTERM | `{200: 5, 502: 19}` |

**Re-run later (this session):**

| Scenario | Result |
|---|---|
| Normal cutover | **278 requests, all 200.** p50 **0.44s**, p99 **0.68s**, max **0.79s** |
| `drain_seconds=0` | 234 requests, **all 200** |
| `SIGKILL` | `{200: 9, 502: 22}` |

**Three honest observations about these numbers.**

1. **The normal cutover is clean in both runs**, on status codes and on connection errors.
   That is the claim, and it is measured rather than asserted.
2. **`drain_seconds=0` produced zero failures on the second run.** That is consistent with the
   recorded rate — 1 failure in 257 requests is a ~0.4% event, and a 234-request run missing
   it entirely is unremarkable. This is exactly why
   `test_what_the_drain_is_actually_worth` asserts **only** that enough requests were made
   (`> 50`) and that no wrong rows came back — **and deliberately asserts nothing about the
   failure count.** A test that required the rare event would be flaky; the docstring records
   the observed rate instead.
3. **The real negative control is the `SIGKILL` run**, and it fires hard and reliably: 22
   failures out of 31. That is the test that would break if the harness stopped being able to
   see breakage. Which matters, because a measurement harness that cannot detect a broken
   cutover cannot certify a working one.

I initially mis-read point 2 as a broken negative control and said so. Reading the test showed
it was by design. That retraction is recorded in
[§10.6](#106-read-the-test-before-declaring-it-broken).

**What is not measured, and is stated as out of scope:** WebSockets and other long-lived
connections. The drain is tuned for request/response traffic — it must exceed the longest
in-flight request plus the keep-alive idle timeout — and a connection that is designed to
outlive that window is simply not covered by the claim.

## 6.4 Timeouts and intervals, and why each is the value it is

Not performance optimisation, but the places where a number was chosen on purpose:

| Knob | Value | Reasoning |
|---|---|---|
| Job lease | 60s | Long enough to survive a GC pause or a slow `docker` call; short enough that a genuinely dead worker's jobs are reclaimed promptly. |
| Lease heartbeat | ~20s | Three heartbeats per lease, so two can be lost without a false reclaim. |
| `MAX_ATTEMPTS` | 3 | Retries a transient failure, refuses to retry a deterministic one forever. |
| `health_passes` | 3 **consecutive** | Distinguishes stable from flapping; cumulative would not. |
| `health_interval` | 0.5s | 3 passes in ~1.5s best case, so the gate is not itself the slow part. |
| `health_timeout` | 30s | A cold Python app importing its dependencies can legitimately take tens of seconds. |
| `drain_seconds` | 2.0 | Must exceed longest in-flight request + keep-alive idle. Measured as sufficient in [§6.3](#63-the-cutover-measurement). |
| Reconcile interval | 10s | Bounds the "reaped within one interval" claim at 10 seconds. |
| Agent `statement_timeout` | 5s | An agent is not allowed to run a query that occupies the database. A platform-level limit, not a prompt-level one. |
| Container timeout | 15m | Set from the measurement in [§6.1](#61-the-dependency-install-6m12s--2m01s-against-a-15-minute-timeout). |
| Approval window | 15m | Long enough for a human to look; short enough that an approval does not become a standing grant. |

## 6.5 What was deliberately not optimised

- **The attack suite.** A clean run is **196 checks** on `flat_todo` and **520** on
  `crm_chain` **[verified]**. These run against real connections as real roles. The number of
  checks scales with tables × personas × operations, and reducing it would mean testing less.
- **The console.** Server-rendered HTML with HTMX. No measured problem, so no optimisation.
- **Database access patterns.** No N+1 hunting, no query tuning. The control plane's load is
  one operator and a webhook.

The common thread: this is an internal control plane handling a handful of events per day,
not a request-serving system. The only latency that reaches a user is the deploy duration,
and the only latency that reaches an *end* user is the cutover — which is why those are the
two things measured.

---

# 7. Reliability and security

## 7.1 The threat model, written down

A security system without a stated threat model is a vibe. This one:

**In scope — what the system defends against:**

| Threat | Defence |
|---|---|
| App code that forgets its `WHERE` clause | RLS policies on every table, `FORCE ROW LEVEL SECURITY`, non-owner roles |
| App code with a SQL injection hole | Same. Injected SQL still runs as `runtime` under the same policies |
| A missing or wrong policy | Structural checks + behavioural attack suite; any failure blocks the deploy |
| Identity leaking across pooled connections | `set_config(..., true)` transaction-local; proven against PgBouncer in transaction mode |
| An AI agent exceeding its person's access | Separate `agent` role, same policies, parity suite requires identical results |
| An agent being talked into a destructive action | Narrow typed tools, no raw SQL/shell, dry run, risk score, approval bound to `diff_hash` |
| A forged GitHub webhook | HMAC-SHA256 verification; unset secret rejects everything |
| Replayed webhook delivery | Delivery-id idempotency; a duplicate creates no second job |
| A paused worker resuming and corrupting state | Fencing tokens on the job queue; generation fence on external effects |
| A stale worker routing traffic | Traffic is granted only by a fenced write to `live_generation` |
| Untrusted migration code reading other apps' data | Runs in a container, against a separate throwaway Postgres instance |
| A committed `.env` becoming live configuration | `.env` files are excluded from deployment |
| The first stranger claiming a fresh install | Bootstrap operator required in production; the setup page is the documented risk |
| Tampering with the audit trail | Hash-chained append-only log; `UPDATE`/`DELETE` not implemented |

**Out of scope — stated, not hidden:**

- **The platform operator.** They hold the socket and the keys. There is no defence here and
  none is claimed.
- **Container escape / the Docker socket.** The worker is root-equivalent on the host. Per-app
  isolation is **defence in depth, not a sandbox.**
- **The app's logic above the database.** RLS governs rows, not pages.
- **Side channels.** Timing, error-message differences, row counts inferred from behaviour.
- **The `migrator` role.** It has `BYPASSRLS` by design; nothing constrains it.
- **Network isolation between apps.** Not claimed.
- **Denial of service.** No quotas, no rate limits beyond the agent's statement timeout.
- **Any database reached outside the proven schema.** If an app connects somewhere else, that
  somewhere else is unverified.

That out-of-scope list is in the product's evidence reports as well as here. A report that
does not carry its own scope invites the reader to assume the scope is "everything," and that
assumption is how security tooling gets people hurt.

## 7.2 What a verification report covers **[verified — the agreed scope statement]**

**Covered:** the schema hash; the access model version; the four role names; which templates
are in use; SELECT/INSERT/UPDATE/DELETE for each; personas A, B, Admin and anonymous; the
count of probes run; and the runtime-vs-agent parity count.

**Not covered:** everything in the out-of-scope list above.

## 7.3 Failure scenarios and what the system does

| Failure | Behaviour | Proven by |
|---|---|---|
| Worker killed mid-deploy | Lease expires, job is reclaimed by the next claim, pipeline resumes or the deployment is blocked | CP6 — **killed at 5 named stages, all recover** |
| Worker paused, then resumes | Its fence is stale: it cannot finish, park or heartbeat the job, and cannot publish a route | Item 1 + CP2 |
| Two workers claim the same job | Impossible by `FOR UPDATE SKIP LOCKED`; if one is a zombie, the fence settles it | Item 1 |
| Containers left over from a dead worker | Found by label, **adopted not duplicated**; stale generations reaped within one reconcile interval | CP6 |
| Proxy restarted and empty | Published into successfully | [§5.18](#518-caddys-admin-api-does-not-create-intermediate-objects) |
| Proxy configuration drifted or hand-edited | Re-asserted from the database every loop | CP6 |
| Proxy unreachable | Deploy **blocked**. No raw-port fallback | CP3 |
| New version fails its health gate | Never published; **old version keeps serving** | CP4 |
| App migration fails | Deploy blocked before anything is published | M7 |
| Attack suite finds one failure | Deploy blocked | M5 |
| Runtime and agent results differ | Deploy blocked (`agent_differs`) | M5 |
| Duplicate webhook delivery | No second job | CP1 |
| Webhook secret unset | Every delivery rejected | CP1 |
| Live generation with nothing listening | Reported `lost`, **left alone** — the reconciler never starts an app | CP6 |
| Two control planes start together | Both try to migrate; a `pg_advisory_lock` serialises them | Phase D |
| Audit record cannot be written | The action is **not executed** | gateway |
| Approval's `diff_hash` no longer matches | Approval void, action back to `pending` | gateway |
| Action claimed twice | Guarded `UPDATE ... WHERE status = expected`; zero rows means someone else has it | gateway |

The row I would point at in an interview is the fourth from the bottom: **a live generation
with nothing running is reported, not repaired.** It is the one place the system deliberately
declines to self-heal, and the reasoning in
[§3.13](#313-a-reconciler-that-will-never-start-an-app) is the kind of restraint that is hard
to argue for and easy to regret omitting.

## 7.4 Defence in depth, layer by layer

```
 layer 7  Postgres RLS         ← THE boundary. Everything else is depth.
 layer 6  non-owner roles      FORCE RLS is only a guarantee for non-owners
 layer 5  NOINHERIT            so privileges are not picked up implicitly
 layer 4  statement_timeout    the agent cannot occupy the database
 layer 3  narrow typed tools   no raw SQL, no shell, no credentials
 layer 2  container isolation  untrusted migrations, separate build database
 layer 1  network              only :80 and :443 published; admin APIs internal
```

The ordering is honest about where the strength is. If layers 1–6 all failed, layer 7 still
returns zero rows for an unidentified caller. If layer 7 failed, nothing else saves you. That
is why the project's rules forbid anything that weakens layer 7 — `USING (true)`, freehand
SQL, `ALTER DEFAULT PRIVILEGES`, granting `migrator` to the app — even when it would be
convenient.

## 7.5 Grants, and the things never granted

**Never granted:** `TRUNCATE`, `REFERENCES`, `TRIGGER`. **Never used:** `ALTER DEFAULT
PRIVILEGES`. **Grants always go last.**

The reasoning per item:

- **`TRUNCATE` bypasses RLS entirely.** It is a table-level operation, so no row policy
  applies. Granting it to `runtime` would hand an app the ability to delete every tenant's
  rows in one statement, with every policy in place and no policy violated.
- **`REFERENCES`** lets a role create a foreign key to a table, which leaks existence
  information about rows it cannot read and can constrain the referenced table's behaviour.
- **`TRIGGER`** lets a role attach code that runs with the *table owner's* rights. That is a
  privilege escalation primitive, handed over as a convenience.
- **`ALTER DEFAULT PRIVILEGES`** makes future objects automatically carry grants. That is an
  invisible, forward-dated grant to tables that do not exist yet and have never been
  attacked. Every grant in this system is explicit and made after the objects exist.
- **Grants last** so that the window between "object exists" and "object is policied" is never
  a window in which the object is also reachable.

## 7.6 Secrets handling

| Secret | Who may read it | Enforcement |
|---|---|---|
| `runtime` DSN | the app | Encrypted in the state root with `VD_SECRET_KEY` |
| `migrator` DSN | the build job only | Passed into the container, never in `argv` ([§5.27](#527-keeping-secrets-out-of-argv)) |
| `agent-*` | **the gateway only** | A separate process from the console is how this is enforced rather than asserted |
| sidecar key | login gates ↔ control plane | `VD_SIDECAR_API_KEY` |
| gateway key | console → gateway | `VD_GATEWAY_API_KEY` |
| session secret | control plane | No unsigned mode; refuses to start without it |
| `VD_SECRET_KEY` | control plane, worker | `VD_SECRET_KEY_FILE` alternative so it need not appear in a process listing or `docker inspect` |
| webhook secret | control plane | Unset ⇒ reject everything |

Two notes:

- **`VD_SECRET_KEY` must not change between restarts.** Change it and every stored app
  credential becomes unreadable and every app's next deploy fails. The env template says this
  in bold, because the failure is total and the cause is invisible.
- **Losing the state root loses every app's database credentials**, and they are encrypted
  with `VD_SECRET_KEY` — so losing *either* one loses both. Also in the env template, next to
  the instruction to back it up.

## 7.7 Error handling philosophy

Three rules, applied consistently:

1. **Validate at boundaries, trust internally.** Webhook payloads, console form submissions,
   agent tool arguments and environment configuration are validated. Internal function calls
   between our own modules are not re-validated at every hop.
2. **Return a value for an expected outcome; raise for an unexpected one.** A fenced write
   matching zero rows is *expected* — a zombie worker is a modelled scenario — so `finish`,
   `park` and `heartbeat` return a boolean. A database being unreachable is unexpected, so it
   raises.
3. **Block rather than degrade.** No proxy means no deploy. No Docker means no deploy. A
   failed probe means no publish. There is no "best effort" mode, because every best-effort
   mode in this system is a security downgrade disguised as availability.

## 7.8 `/health` answers for the database, not for the process

The control plane's health endpoint checks database reachability. **[verified]**

This sounds pedantic and is not: the compose file has `restart: unless-stopped` and a
healthcheck, and those two together are only meaningful if "healthy" means "can do its job."
A process that is running, accepting connections and unable to reach Postgres is not healthy
in any sense a user cares about, but it answers a process-level ping perfectly.

---

# 8. Testing

This is the part of the project I would most want to be asked about, because the testing
strategy is the product. A security tool's value is entirely in whether its claims are
checkable.

## 8.1 The numbers **[verified]**

- **618 tests collected, 618 passed, 0 failed.**
- **487 test functions across 42 files.** The gap to 618 is parametrisation — most notably the
  runtime/agent parity axis, which runs the same assertions under two different database
  roles.
- Per-directory: pipeline 174, orchestration 135, gateway 131, kernel 40, sidecar 33,
  attack 26, derive 26, shims 22, e2e 17, concurrency 14.

The 487-vs-618 discrepancy is worth a line because it was briefly confusing: a file-level
inventory counted functions, the runner counted parametrised cases. Both are right. For the
record, 618 is the number that matters, because each parametrised case is an independently
failing assertion.

## 8.2 The rules, and why each exists

From `CLAUDE.md`, enforced throughout:

**Test expectations come from the seeding plan, never from reading the policies back.**
The suite knows user A owns rows 1–5 because it inserted them. If expectations were derived
from the generated policies, a derivation bug would produce policies that match the
expectations derived from those same policies — a tautology that passes forever. This is the
most important testing rule in the project.

**For M1, M2, M5 and M11, the failing test comes first.** Those four are the kernel, the
identity shim, the policy engine and the agent core — the components where "it appears to
work" and "it works" are hardest to distinguish by inspection.

**Never delete or skip a test to make CI green.** With a named list of permanently protected
tests (next section).

**Every claim in the documentation names its test.** This is what CP7's `EVIDENCE.md` is for,
and it is not done yet.

## 8.3 The permanently protected tests **[verified]**

Six tests (or groups) that may not be deleted, skipped or weakened, ever:

| Test | What its removal would silently permit |
|---|---|
| The pooling test | Identity leaking between users on a pooled connection |
| The planted-bug tests | A verifier that passes everything |
| The acts-for escape tests | An agent exceeding the person it acts for |
| The audit chain tamper test | An undetectably edited audit trail |
| The fail-closed tests | "No identity" meaning "all rows" |

Writing this list down is a hedge against a specific, very human failure: it is 6pm, the suite
is red, the failing test is slow and inscrutable, and skipping it would make the problem go
away. The list makes that decision require an explicit act of vandalism rather than a moment
of weakness.

## 8.4 The central practice: planted bugs and counted failures

For every feature, deliberately break it and record **exactly how many tests fail.**

| Sabotage | Tests that fail **[verified]** |
|---|---|
| `USING (true)` in a policy | **18** |
| The 11 standard planted policy bugs | all **11** caught |
| `return True` in the webhook `verify()` | exactly **4** |
| Randomise `delivery_id` (break idempotency) | exactly **2** |
| Delete the fence check | **5** |
| Drop `apps.desired_generation` | exactly **4** |
| Make reconciler `keep` a scalar (`one < keep`) | exactly **2** |
| Re-add a raw-port fallback | exactly **3** |
| Make `proxy.require` a no-op | exactly **1** |
| CP4's three planted bugs | **1 / 1 / 1** |
| CP6's three planted bugs | **1 / 1 / 3** |

**Why "exactly N" and not "at least one":** the count is diagnostic.

- **Zero** means the test suite does not test the feature at all. That is the discovery in
  [§5.11](#511-a-missing-with-check-is-behaviourally-invisible) and
  [§5.10](#510-postgres-applies-the-select-policy-to-update-with-a-where-clause).
- **One** is often ideal — a single, specific, well-targeted test.
- **Eighteen** for `USING (true)` is also right, because that sabotage breaks the security
  model everywhere and *should* light up broadly.
- An **unexpected** count means something has shifted. If a sabotage that used to fail 4 tests
  now fails 2, two tests have stopped testing it, and that is a regression in the suite that
  no amount of green reports would reveal.

I would describe this as the single most valuable practice I took from this project. It
converts "we have tests" — unfalsifiable — into "this specific sabotage breaks these specific
tests," which is a claim you can check.

## 8.5 Three complementary layers of verification

Each layer exists because the others have a proven blind spot.

**1. Behavioural probes.** Connect as the real `runtime` and `agent` roles and try to read and
write other users' rows. Personas: A, B, Admin, anonymous. These prove the system does the
right thing for real connections against the real planner.

*Blind spot:* cannot see a missing `WITH CHECK` ([§5.11](#511-a-missing-with-check-is-behaviourally-invisible)),
and `update_other`/`delete_other` are shadowed by a correct SELECT policy
([§5.10](#510-postgres-applies-the-select-policy-to-update-with-a-where-clause)).

**2. Structural checks.** Read `pg_policy` and friends and assert the configuration: every
table has four policies, every policy has both `USING` and `WITH CHECK`, every policy names
both `runtime` and `agent`, `FORCE ROW LEVEL SECURITY` is on, no policy contains
`USING (true)`, and the predicate is literally one of the five template renderings.

*Blind spot:* proves the configuration is as intended, not that Postgres enforces it the way
you think. Also where [§5.9](#59-three-small-expensive-harness-bugs)'s `bytes`-vs-`str` bug
lived — a structural check comparing the wrong types silently never matches.

**3. Parity.** The whole behavioural suite, run twice: once as `runtime`, once as `agent`.
Byte-identical results required, with a dedicated `agent_differs` check.

*Why it is a separate layer:* it is the only check of the gateway's core claim. The first two
layers would both pass an implementation in which the agent role had subtly broader access.

A clean run is **196 checks** for the `flat_todo` fixture and **520** for `crm_chain`
**[verified]**. The difference is the fixture: `crm_chain` has more tables and FK chains, so
tables × personas × operations is larger.

## 8.6 Fixtures as adversaries

The fixtures are designed to be *hard*, not representative:

- **`flat_todo`** — the simple case. Direct `owner_column` on each table.
- **`crm_chain`** — ownership reached through foreign key chains, which is what `fk_chain`
  exists for and where derivation can plausibly get it wrong.
- **`todo_alembic`** — the same app with Alembic migrations instead of plain `.sql`, to
  exercise the other migration path.
- **A Node app** — deployed and marked **"Unprotected."** A fixture whose purpose is to prove
  the system labels what it cannot protect rather than pretending.

And `tests/pipeline/gitrepo.py` builds **real git repositories** on the fly, so the clone path
exercises actual git rather than a stub.

## 8.7 Edge cases that have dedicated tests

Each of these exists because of a specific worry or a specific bug:

| Edge case | Why |
|---|---|
| No identity at all | Must be zero rows, never everything |
| Empty-string identity | `NULLIF` must treat it as no identity |
| Anonymous persona | A caller with no user id is a first-class case, not an error |
| Admin persona | `admin_only` must work, and must not leak to non-admins |
| Pooled connection handed over mid-session | 500 concurrent × 5 users × 10 runs through PgBouncer |
| Second deploy of the same app | [§5.14](#514-no-app-with-plain-sql-migrations-could-be-deployed-twice) |
| Proxy restarted empty | [§5.18](#518-caddys-admin-api-does-not-create-intermediate-objects) |
| Worker killed at 5 named pipeline stages | All must recover |
| Reconcile loop run twice | Second pass must report no drift |
| Orphaned containers from a previous worker | Adopted, not duplicated |
| Duplicate webhook delivery id | No second job |
| Unsigned / wrongly-signed / unset-secret webhook | All rejected |
| `drain_seconds=0` | The harness must be *able* to see failures |
| `SIGKILL` instead of a graceful stop | Must produce many failures |
| Insert blocked for the wrong reason | Must count as a failure, not a pass ([§5.12](#512-an-insert-failing-for-the-wrong-reason-was-counted-as-a-pass)) |
| Approval whose `diff_hash` has changed | Void, back to pending |
| Action claimed twice | At most one execution |
| Audit log row tampered with | Chain verification fails |

## 8.8 What the test strategy got wrong

Three test-suite failures found the hard way, all worth more than the successes:

1. **A shared long-lived fixture hid an initialisation bug.** 533 tests shared one Caddy
   container and therefore never once published into an unconfigured proxy — the state of
   every real first deploy ([§5.18](#518-caddys-admin-api-does-not-create-intermediate-objects)).
   *Counter-practice:* at least one test must run against a never-written-to instance.

2. **Every test deployed exactly once.** The suite was constant along the "number of deploys"
   axis, so the entire product worked only for first deploys
   ([§5.14](#514-no-app-with-plain-sql-migrations-could-be-deployed-twice)).
   *Counter-practice:* ask what value is identical in every test — that value is untested.

3. **Crash simulation was in-process.** `Killed(BaseException)` raised inside the worker's loop
   simulates control flow but not process death, so child-process consequences are invisible
   ([§5.19](#519-stopping-the-worker-stops-every-app-it-started--open-unfixed)).
   *Counter-practice:* at least one crash test must actually kill a process.

The pattern across all three: **the test suite was blind in exactly the direction its own
convenience pointed.** Fast fixtures, uniform setup, in-process simulation — each a reasonable
engineering choice, each one creating a systematic blind spot that no amount of adding more
tests in the same style would close.

## 8.9 The negative-control discipline

Every claim that something is *prevented* needs a demonstration that it can *happen*:

- The M2 unsafe shim must leak — and had to be fixed when it didn't
  ([§5.13](#513-the-negative-control-that-refused-to-leak)).
- `drain_seconds=0` must be able to produce failures.
- `SIGKILL` must produce many.
- Planted bugs must be caught, and the count recorded.

A test that has never been seen to fail is a test of unknown value. Running it against a
broken system is the only way to find out what it measures.

---

# 9. Deployment

## 9.1 The local stack

`docker compose -f infra/local/docker-compose.yml up -d`

| Service | Address | Notes |
|---|---|---|
| Postgres | `127.0.0.1:55432` | admin `vd_admin` / `vd_local_password` |
| PgBouncer | `127.0.0.1:55433` | transaction mode. **Moved off 56432** because Hyper-V reserves 55834–56433 ([§5.16](#516-the-port-hyper-v-had-already-reserved)) |
| buildpg | `127.0.0.1:55434` | the throwaway. No app ever lives here |
| Caddy | `127.0.0.1:55080` | apps are published here **and nowhere else** |
| Caddy admin | `127.0.0.1:55019` | route table POSTed here from the database |

Three things worth knowing about the local stack:

- **Caddy is not optional and not behind a profile.** Apps live at
  `http://<subdomain>.localhost:55080` only. A deploy with no proxy **blocks** rather than
  handing out a port.
- **`*.localhost` does not resolve on Windows.** So the tests hit `127.0.0.1:55080` with an
  explicit `Host:` header — `tests/pipeline/conftest.visit`. A small thing that would
  otherwise make the entire pipeline suite unrunnable on the development machine.
- **Docker is required twice over**: for the stack, and because app migrations only ever run in
  a container.

Run the suite with `.venv/Scripts/python.exe -m pytest -q` from the repository root. The
virtualenv is **not** auto-activated and is **Python 3.13.7** — which differs from the image's
**3.12.15**, a gap noted in [§9.7](#97-what-is-still-unproven).

## 9.2 The production stack

`infra/prod/` — four files: `Dockerfile`, `docker-compose.yml`, `Caddyfile`, `env.example`.

One Linux host, everything on it.

```
cp infra/prod/env.example infra/prod/.env && $EDITOR infra/prod/.env
docker compose -f infra/prod/docker-compose.yml up -d --build
```

**The published-ports claim, asserted in the compose file rather than described in a runbook:**
**exactly three ports** reach the internet — 80 and 443 on Caddy, and nothing else anywhere.
Postgres, buildpg, Caddy's admin API, the console and the gateway all answer on the compose
network only. A `ports:` line is the only form of that claim anyone can verify, which is why it
is made there. **[verified by `docker compose config`: only Caddy publishes ports, and the
Docker socket is mounted on the worker alone.]**

**`.env` is read twice, on purpose:** once by compose for the `${...}` interpolations, and once
by each container as its environment. One file, so the paths the host mounts and the paths the
processes are told about cannot disagree.

**No defaults for anything that matters.** A stack brought up without an `.env` fails to
interpolate rather than coming up with a password that is in the repository.

## 9.3 Configuration surface

Grouped as the env template groups it, with the non-obvious ones explained.

**Where things are on this host**

| Variable | Note |
|---|---|
| `VD_ROOT` | The checked-out repository, mounted at **this same path** inside the containers ([§3.22](#322-code-is-mounted-into-the-production-image-not-copied)) |
| `VD_STATE_ROOT` | Per-app secrets, deployment checkouts, logs. **Back this up.** `chown -R 10000:10000` before the first `up` |
| `VD_DOCKER_GID` | The host's `docker` group gid (`getent group docker \| cut -d: -f3`). Named rather than guessed, because it differs per host |

**Which installation this is**

| Variable | Note |
|---|---|
| `VD_ENV=prod` | Turns every convenience default into a refusal to start ([§3.21](#321-vd_envprod-turns-conveniences-into-refusals)) |
| `VD_MIGRATE_ON_BOOT=1` | Only the control plane honours it; under a `pg_advisory_lock`, so two containers cannot race. The worker never migrates |
| `VD_PUBLIC_SCHEME=https` | **Decides three things at once** — the console session cookie's `Secure` flag, an app's login cookie's `Secure` flag, and whether the route table written into Caddy listens on 443 (which is what makes Caddy fetch a certificate). It cannot be set half way, and it must be set before the first deploy |
| `VD_RUNTIME=apprt` | Two containers per generation, sharing a network namespace the login gate owns |

**Names on the internet**

`VD_APP_DOMAIN` (needs a **wildcard A record**, `*.apps.example.com`), `VD_APP_PORT`,
`VD_CONSOLE_DOMAIN` (its own A record — **leave it empty and the console has no public address
at all**, which is a defensible way to run this, reached over an SSH tunnel to 8100 instead),
`VD_ACME_EMAIL` (an ACME account with no contact is how a renewal failure becomes an outage
nobody was told about).

**Databases** — three credential sets. `VD_CONTROL_DATABASE_URL` (the control plane's own
tables), `VD_APP_ADMIN_DSN` (the customer database, from which every app's DSN is derived), and
`VD_BUILD_DATABASE_URL` (the throwaway, with a **deliberately different password**).

**Secrets** — six, each generated with `openssl rand -hex 32`:
`VD_SESSION_SECRET`, `VD_SECRET_KEY`, `VD_SIDECAR_API_KEY`, `VD_GATEWAY_API_KEY`,
`VD_GITHUB_WEBHOOK_SECRET`, plus `VD_BOOTSTRAP_EMAIL`/`VD_BOOTSTRAP_PASSWORD`.

**How our processes find each other** — compose **service names, not loopback**:
`http://control-plane:8100`, `http://gateway:8200`, `http://caddy:2019`. Inside a container
`127.0.0.1` is that container, and these addresses are dialled from four different ones (the
control plane, the worker, the build job, and every app's login gate). `VD_APP_NETWORK` and
`VD_BUILD_NETWORK` are separate variables for the reason in
[§4.8](#48-the-production-hardening-phases). `VD_CONTAINER_HOST` is an escape hatch that
should not be needed.

## 9.4 Why this cannot run on a PaaS

A question worth answering because it looks like it should be a trivial deploy. It was
evaluated and rejected: **Render (and any similar platform) cannot host the worker.**

| Requirement | Why a PaaS cannot provide it |
|---|---|
| The host's Docker socket | Not offered, and offering it would be a container escape for every customer |
| Identical paths on both sides of the worker's filesystem | The worker hands host paths to the host daemon; a PaaS's filesystem is not the daemon's |
| Ownership of :443 with per-app wildcard subdomains | The platform owns the ingress and the certificates |
| Starting arbitrary long-lived containers on demand | That is the platform's own job, not a customer capability |

The honest framing: **a deployment platform has to be the host.** You cannot host a thing whose
function is hosting on a thing that hosts for you. Hence one Linux box.

## 9.5 The deployment plan, staged

**Stage 0 — build and audit the image locally.** Done. This stage paid for itself immediately:
it caught the missing `docker` binary ([§5.21](#521-the-production-image-had-no-docker-binary--found-before-the-first-deploy)).
The audit checks performed:

- `command -v docker` inside the image (the one that found the bug)
- every entrypoint imports under the image's Python
- `compileall` clean (with `PYTHONPYCACHEPREFIX` pointed at `/tmp`, [§5.23](#523-ninety-errors-that-were-all-the-same-read-only-mount))
- the 13 pins match the versions the green suite runs on
- `docker compose config` interpolates completely, publishes only Caddy's ports, and mounts the
  socket on the worker alone

**Stage 1 — DNS.** A wildcard `*.apps.<domain>` and a `console.<domain>`, both pointed at the
box. Before the first `up`, because ACME needs them.

**Stage 2 — the host.** Clone to `/opt/vibedeploy`. Create the state root and
`chown -R 10000:10000` it. Read `VD_DOCKER_GID` off the host. Fill `.env`.

**Stage 3 — come up on `http` first.** `VD_PUBLIC_SCHEME=http`. This is deliberately a
reduced-variable run: the apprt runtime has never executed on real Linux, and bringing it up
without TLS and ACME in the mix means a failure has one plausible cause instead of three.

**Stage 4 — flip to `https` and redeploy.** Once an app has been deployed and reached over
`http`, set `VD_PUBLIC_SCHEME=https` and deploy again. The flip changes cookie flags and the
proxy listener together.

The staging principle: **introduce one unproven thing at a time.** Stage 3 exists purely
because combining "first Linux run" with "first ACME issuance" would make the inevitable first
failure ambiguous.

## 9.6 CI/CD

**There is no CI pipeline.** **[verified — no workflow files; the test command in `CLAUDE.md`
is a local one.]**

Stated plainly because it is a real gap and a defensible one at this stage:

- The suite requires Docker, a Postgres instance, a PgBouncer, a Caddy, and roughly twenty
  minutes. That is a non-trivial CI environment, not a `pip install && pytest` job.
- Parts of it would need redesign to run in CI at all: the two-pytest-areas conflict
  ([§5.20](#520-two-pytest-areas-truncate-the-same-database)) means the areas must be
  serialised, and the PyPI saturation issue
  ([§5.30](#530-a-long-pipeline-run-saturates-pypi)) would be worse from a cloud runner.
- The practice that substitutes for it is the planted-bug discipline plus the rule that no
  checkpoint is done until the full suite is green.

**[inferred]** The honest assessment: the absence of CI is the largest process gap in the
project. The suite is good enough that automating it would be high-value; what is missing is
the environment, not the tests.

**What does exist in place of a deployment pipeline:** the system deploys *other people's*
apps with health gates, fencing and reconciliation — and is itself deployed by
`docker compose up -d --build` with code mounted from a git checkout. There is a certain
irony in that, and it is the correct trade for now: the platform's own release cadence is one
person pulling a branch.

## 9.7 What is still unproven

The list I would give an interviewer if asked "what would you check first in production?":

1. **`VD_RUNTIME=apprt` has never run on real Linux.** It is the production default. Docker
   Desktop masks exactly the differences most likely to break it — `host.docker.internal`
   existing, and loopback ports being reachable from the bridge
   ([§5.4](#54-hostdockerinternal-is-a-docker-desktop-property-not-a-docker-one)). This is the
   single biggest known risk.
2. **Python version gap.** The image is **3.12.15**; the development virtualenv is **3.13.7**.
   Every entrypoint has been verified to import under the image's interpreter, but the suite
   has only ever run under 3.13.
3. **The apprt image's shim installation is unverified.** The app runtime image must install
   `vibedeploy_shim`; it is absent from the *platform* image, which is expected, but the apprt
   side has not been confirmed. This needs checking before the first app deploy, not after.
4. **ACME has never run.** No certificate has ever been issued by this installation.
5. **CP7 does not exist.** No chaos suite, no `EVIDENCE.md`. The reliability claims are backed
   by the test suite, not by an evidence document.
6. **The worker logs nowhere** ([§5.28](#528-the-worker-logs-nowhere--open)). First production
   incident will be diagnosed from database rows.
7. **Backup and restore have never been exercised.** The env template says to back up the state
   root; nothing has ever been restored from one.

## 9.8 Operational notes

- **Stopping the worker, under the legacy local runtime, stops every app**
  ([§5.19](#519-stopping-the-worker-stops-every-app-it-started--open-unfixed)). Under apprt it
  does not, because containers are children of the daemon.
- **Caddy's `/data` volume must survive.** It holds the certificates and the ACME account key.
  Lose it and every restart is a fresh set of issuance requests against a rate limit.
- **Do not hand-edit the Caddyfile for routes.** They are re-asserted from the database within
  one reconcile interval ([§3.14](#314-the-proxy-route-table-is-derived-never-trusted)).
- **`VD_SECRET_KEY` is immutable in practice.** Changing it orphans every stored credential.
- **The compose project name decides the network name**, and `VD_APP_NETWORK` must follow it.
  Change `name:` in the compose file and that variable has to change too.
- **Docker Desktop on Windows:** containers sometimes exit 255 after the host sleeps. `up -d`
  again.

---

# 10. Lessons learned

Ordered by how much they changed how I work, not by topic.

## 10.1 An isolation boundary is also a detector

The strongest single lesson. Moving app migrations from a subprocess into a real container was
planned as a security improvement, and it *also* revealed that the DSN being used was the
**customer** database rather than the throwaway one
([§5.5](#55-the-scratch-dsn-pointed-at-the-customer-database)).

The mechanism is general: a process holding every credential cannot tell you it used the wrong
one. Least privilege is usually sold as damage limitation, but its more immediate payoff is
**observability of mistakes**. Give a component exactly what it needs and misconfiguration
becomes a loud error instead of a silent success.

I now expect every isolation improvement to surface at least one latent bug, and I treat it as
mildly suspicious when one doesn't.

## 10.2 "The tests pass" is not a claim until you break something

Three separate bugs in this project were found by *deliberately sabotaging working code* and
noticing the suite didn't care:

- A missing `WITH CHECK` — invisible, because Postgres reuses `USING`
  ([§5.11](#511-a-missing-with-check-is-behaviourally-invisible)).
- `update_other` / `delete_other` probes that could never fire, shadowed by a correct SELECT
  policy ([§5.10](#510-postgres-applies-the-select-policy-to-update-with-a-where-clause)).
- An insert probe that counted *any* error as enforcement
  ([§5.12](#512-an-insert-failing-for-the-wrong-reason-was-counted-as-a-pass)).

None of these are findable by adding more tests in the same style. They are findable only by
asking "if this were broken, which test would fail?" and then checking the answer empirically.

The practice that falls out of this — recording **exactly how many tests** each sabotage breaks
— is the one thing from this project I would introduce on day one of any team.

## 10.3 Look for the axis your tests are constant along

The suite was large, thorough, and uniformly blind to second deploys, because every test
deployed exactly once
([§5.14](#514-no-app-with-plain-sql-migrations-could-be-deployed-twice)). The entire product
worked only for first deploys and nobody knew.

Similarly: 533 tests shared one long-running Caddy and therefore never published into an
unconfigured one ([§5.18](#518-caddys-admin-api-does-not-create-intermediate-objects)) — the
state of every real first deploy.

The diagnostic question is cheap and worth asking periodically: **what value is identical in
every single test?** That value is untested no matter how many tests there are. Test *count*
is a measure of effort; test *variance* is a measure of coverage.

## 10.4 Shared, efficient fixtures buy speed with blind spots

A corollary of the above, but worth separating because the cause is different. Fast fixtures
are good engineering. A shared long-lived service is the obvious way to make an integration
suite tolerable. And it systematically prevents the suite from ever seeing a cold start — which
is the state of every real first deploy and every post-reboot recovery.

The rule I'd adopt: **for anything that writes configuration into an external system, at least
one test runs against an instance that has never been written to.** Not a reset instance — a
virgin one.

## 10.5 A response is a promise that the work is durable

FastAPI runs `yield`-dependency teardown **after** the response is sent, so commit-on-teardown
produced two bugs that looked like application logic errors
([§5.8](#58-fastapi-runs-yield-dependency-teardown-after-the-response-reaches-the-client)): a
build job callback returning 204 and then reading a stale status, and a `303` redirect whose
follow-up `GET` 404'd.

The bug is in neither the handler nor the dependency. It lives in the ordering guarantee
between layers, and it only appears when the client is fast enough to come back first — so a
slower machine hides it entirely.

The generalisation: if you send a response before committing, you have lied, and the client is
entitled to act on the lie immediately. Any **write-then-redirect** or **write-then-callback**
flow deserves a specific audit for this.

## 10.6 Read the test before declaring it broken

A mistake I made in this project, recorded because the correction is the lesson. I saw a
negative control (`drain_seconds=0`) produce **zero failures** and declared it broken — "a
negative control that passes proves nothing."

Reading the test showed it asserts only that enough requests were made and that no wrong rows
came back, and **deliberately asserts nothing about the failure count**, because the observed
rate is ~1 in 257 and requiring a rare event would make the test flaky. The docstring records
the rate. The real negative control is the `SIGKILL` test, which fires hard and did fire.

I was wrong, and I retracted it. Two things I took from it: the suspicion itself was correct
and worth voicing (an unfirable negative control *is* a serious problem — see
[§5.13](#513-the-negative-control-that-refused-to-leak), where exactly that happened); but
**the cost of reading the test first was about ninety seconds**, and skipping it produced a
confidently wrong statement. Suspect loudly, verify before concluding.

## 10.7 Write down the divergence instead of resolving it quietly

Two conflicts between the spec and the implementation were recorded rather than smoothed over:

- `BYPASSRLS` and `SET role = owner` cannot coexist, so `SET role` was omitted — and the
  conflict with `Readme.md` §9.1 was written down
  ([§5.3](#53-bypassrls-and-set-role--owner-are-mutually-exclusive--a-spec-conflict)).
- §15 says the git clone happens in the build job; it still happens in the worker. Flagged,
  open, not hidden.

The temptation is always to implement whatever works and move on. Do that twice and the spec
no longer describes the system, and a spec that is subtly wrong is worse than no spec, because
people trust it. One paragraph of divergence notes preserves the document's value.

## 10.8 Deleting working code can be the right call

A tested, working Node/Prisma identity shim was **deleted**
([§4.5](#45-the-scope-reset-deleting-working-code)). Node apps are now deployed and labelled
"Unprotected."

The shim is the component that guarantees identity reaches the transaction — the thing the
500-concurrent-request pooling test exists to defend. Two shims means two of those, and the
second would have been maintained at half the attention. **A half-attention shim on a security
path is worse than no shim**, because "Unprotected" is an honest label and a weak shim is a
false one.

This was the hardest decision in the project and I am most confident it was correct. The
general form: *the cost of a feature on a security path is not the code, it is the attention
it needs forever.*

## 10.9 Fail-closed has a price, and you pay it in the fixtures

Truncating the database left `global_settings` empty, which made the kill switch read as ON —
by design — which broke the demo
([§5.24](#524-the-demo-kept-breaking-because-truncation-deleted-the-kill-switch)).

The correct response is to fix the fixture, never the default. But it is worth knowing in
advance that a consistently fail-closed system produces a specific and maddening developer
experience: **nothing is broken, and nothing works.** Every one of those failures is a
deliberate decision executing correctly. Budget for it, and make the test helpers restore
every row the system treats as "absent means deny."

## 10.10 The platform's conveniences are production's absences

`host.docker.internal` exists on Docker Desktop and not on Linux. Loopback-bound host ports
are reachable from Desktop's VM and not from a Linux bridge. `os.kill(pid, 0)` is a no-op on
POSIX and a kill on Windows. Git Bash rewrites `/src` into `C:/Program Files/Git/src`. A port
can be unavailable on Windows with nothing listening on it.

Five separate platform-difference bugs in one project
([§5.4](#54-hostdockerinternal-is-a-docker-desktop-property-not-a-docker-one),
[§5.17](#517-oskillpid-0-terminates-the-process-on-windows),
[§5.22](#522-git-bash-rewrote-a-container-path-into-a-windows-path),
[§5.16](#516-the-port-hyper-v-had-already-reserved)), and the biggest remaining risk in the
project is a sixth one waiting on the Linux box.

The practical consequence is the staged deploy plan in
[§9.5](#95-the-deployment-plan-staged): come up on `http` first, so that the first Linux run
has one unproven variable rather than three.

## 10.11 The best fix is often more truthful than the bug

`os.kill(pid, 0)` was replaced with "wait for the port to go quiet"
([§5.17](#517-oskillpid-0-terminates-the-process-on-windows)). That is not a workaround — it is
a better measurement. A pid being alive does not mean a process is still serving; a port being
quiet is a direct statement about the property that matters, observed from outside, with no
platform API involved.

When a platform quirk pushes you toward observing the externally-visible property instead of
the internal one, that is usually the direction you should have been going anyway.

## 10.12 A green build is not a working artifact

`apt-get install docker.io` exited 0 and installed a package that no longer contains
`/usr/bin/docker` ([§5.21](#521-the-production-image-had-no-docker-binary--found-before-the-first-deploy)).
`dpkg -l` confirmed it was installed. The binary did not exist.

**Verify the capability, not the installation.** `command -v docker`, not "the layer built."
And the same lesson at the stage level: Stage 0 of the deploy plan — build and audit the
artifact locally — cost minutes and prevented a first-deploy failure on a fresh server, where
every other variable would also have been unproven and the error would have been blamed on the
new environment.

## 10.13 Narrow claims survive; broad ones get falsified

The fencing guarantee is written as: *a stale worker cannot route traffic, and its leftovers
are reaped within one reconcile interval.* Not "a stale worker cannot do anything" — it can
start a container, and nothing can stop that once it holds the socket.

Likewise: per-app isolation is **defence in depth, not a sandbox**. The zero-downtime claim
**excludes WebSockets**. The evidence report carries its own **not-covered** list.

Every one of those narrowings is a place where the broader claim would have been more
impressive and false. A precise claim is checkable and therefore worth something; an impressive
one invites the reader to assume the scope is "everything," and that assumption is how security
tooling gets people hurt.

## 10.14 Operability has to be somebody's checkpoint

The worker logs nowhere ([§5.28](#528-the-worker-logs-nowhere--open)), and that single gap made
every other bug in this document more expensive to find. Several were diagnosed by reasoning
backwards from a wrong database row — work a log would have done.

It is still open for a mundane reason: **it was never a checkpoint's "done when."** Every
checkpoint had sharp, testable acceptance criteria, and plan-driven development is extremely
good at delivering exactly what is in the plan. Nothing that isn't in a checkpoint happens. So
cross-cutting concerns — logging, backups, CI — need to be a checkpoint of their own, or they
are permanently the next thing.

---

# 11. Interview preparation

Questions an interviewer would actually ask, with the answers I would give. Written as answers
rather than as bullet points, because the grading is on reasoning.

## 11.A The core design

### Q1. Explain this project in two minutes.

It is a self-hosted deployment control plane for Python/Postgres apps that verifies database
authorization before every release.

The problem it attacks: in a multi-tenant app, a handler that runs
`SELECT * FROM invoices` instead of `... WHERE user_id = $1` serves every tenant's data to
whoever asks. That bug is invisible in review, invisible in a single-user test suite, and
catastrophic on discovery.

The approach: push authorization into Postgres row-level security, so it is enforced *below*
the app rather than inside it. Then generate the policies deterministically from five fixed
templates, then attack the result — connect as the app's real low-privilege role and try to
read and write other users' rows — and block the release on any failure.

There's a reliability half too, because a deploy is a multi-step process with external effects
and will get interrupted: leases and fencing tokens on the job queue, a generation fence so
the fence covers containers and proxy routes and not just database rows, a health-gated
blue-green cutover whose downtime is **measured** rather than asserted, and a reconciliation
loop that converges the world back to what the database says.

And a third half that is really a demonstration: an agent gateway where an AI agent connects
as a separate Postgres role with the *same* policies, so "the agent gets exactly the access of
the person it works for" is enforced by the same mechanism rather than by a second security
model.

### Q2. Why RLS instead of a query-rewriting ORM scope?

Because the layer doing the rewriting is the same layer that wrote the bug.

An ORM scope protects the queries that go through it. The handler that uses a raw connection,
or a `text()` escape hatch, or a new code path somebody added last week, is unprotected — and
nothing tells you. You've moved the problem from "remember the WHERE clause" to "remember to
use the scope," which is the same class of problem with better ergonomics.

RLS is below the application. The policy is applied by the planner. `SELECT * FROM invoices`
and `SELECT * FROM invoices WHERE user_id = $1` return the same rows. That means the app's SQL
is *allowed* to be wrong, which is the only version of this guarantee worth having, because
the app's SQL *will* be wrong eventually.

I'd also give the honest costs: it's Postgres-only, policies are DDL so they need generating
and migrating, and RLS has genuine semantic corners that bit me — I'd then tell the
`WITH CHECK` story from Q12.

### Q3. Why only five policy templates? Isn't that restrictive?

Yes, and that is the feature.

With five templates, "is this policy correct?" is a **decidable structural question**. The
verifier can assert that every table has four policies, that each has both `USING` and
`WITH CHECK`, that each names both the runtime and agent roles, and that the predicate is
literally one of five known renderings. With arbitrary SQL predicates, the only available
check is behavioural — and I know from experience that behavioural checks miss exactly the
invisible failures.

A free-form predicate is also a free-form hole. `USING (true)` is a valid policy.

The cost is real and I'd state it: schemas that don't fit a template aren't supported. The
access-model contract can't even *express* a composite foreign key, so an ownership chain
through one is out of scope. That is a product gap I'd rather have written down than have
quietly filled with a freehand predicate nobody can verify.

### Q4. Why four roles and not three?

`owner`, `migrator`, `runtime`, `agent`.

`owner` is `NOLOGIN` — it owns the schema objects, and because it can't log in, owning isn't a
capability anyone holds. That matters because in Postgres a table's owner is exempt from its
policies unless you force the issue.

`migrator` has `BYPASSRLS` because a migration legitimately needs to see every row. It is
never given to the app or to the gateway.

`runtime` is the app. `agent` is the gateway acting for a person. The fourth role is the one
people question, and the answer is that it makes "the agent did this" a database-level fact
rather than an application-level annotation — and, more importantly, it gives the parity test
two subjects. The attack suite runs as `runtime` and again as `agent` and the results must be
identical. That test *is* the agent security claim. Without a separate role, there's nothing
to compare.

Both `runtime` and `agent` are `NOBYPASSRLS`, `NOINHERIT`, and not owners. `NOINHERIT` isn't
cosmetic: without it, role membership leaks privileges implicitly and the four-way separation
collapses into one role with four names.

### Q5. How does identity get into the database, and what could go wrong?

One way only:

```sql
set_config('app.user_id', $1, true)
```

A bind parameter, `is_local = true`, inside a transaction. Policies read it through
`vd_user_id()`, which is
`NULLIF(current_setting('app.user_id', true), '')::<keytype>`.

Three things go wrong if you do it any other way:

1. **Plain `SET` can't take a bind parameter.** So you interpolate a string into SQL — SQL
   injection in the authorization layer, which is the worst possible location for it.
2. **`SET` without `LOCAL` is session-scoped.** On a pooled connection the identity outlives
   the request and the next request inherits it: a user silently acting as whoever used that
   connection before them. This is not theoretical — there's a permanently protected test that
   drives 500 concurrent requests across 5 users through PgBouncer in transaction mode, ten
   times, specifically to prove it can't happen.
3. **`is_local = true` means it dies with the transaction** — no cleanup code to forget.

The `missing_ok` argument to `current_setting` plus the `NULLIF` are the fail-closed part: no
identity and empty-string identity both become `NULL`, and `NULL` matches nothing. No identity
means zero rows, never everything.

### Q6. Why not let an LLM derive the access model? It'd handle more schemas.

Because an LLM-authored policy has no stable failure mode you can test against.

The attack suite can't distinguish "the model was right" from "the model was lucky this run."
A security control you can't regression-test isn't a control. Deterministic derivation is wrong
in the *same way* every time — which means one test catches it once and forever, and the
planted-bug discipline works.

Where the LLM is allowed: asking the builder questions in natural language and summarising a
report. What it is never allowed to do is guess. If the builder doesn't answer the access-model
questions, the launch is **blocked**. A blocked launch is a bad day; a guessed access model is
a breach.

### Q7. Why is the verification an attack suite rather than reading the policies back?

Reading them back is marking your own homework. If derivation has a bug, the policy it wrote is
exactly the policy it expects to read.

So the hardest rule in the project is: **test expectations come from the seeding plan, never
from the policies.** The suite inserted user A's rows, so it knows user A owns rows 1–5. It
then connects as a real low-privilege role as user B and checks what B can see — against the
plan.

That said, I run *both*. Behavioural probes and structural checks, because each has a proven
blind spot and I found both the hard way. Q12 is the structural case; Q13 is the behavioural
one.

## 11.B Distributed systems and reliability

### Q8. You used `FOR UPDATE SKIP LOCKED` for the queue. Why is that not sufficient?

`SKIP LOCKED` holds a row lock for the life of the transaction. A deployment pipeline is
minutes long and makes external calls, so holding a transaction open across it is wrong — it
pins a connection and blocks vacuum. So you claim and commit, and now the lock is gone.

The only thing left preventing a second worker from taking the job is a lease timeout. And a
timeout alone gives you the classic distributed-lock failure: the first worker wasn't dead,
only paused — GC, host suspend, a slow `docker` call — and when it resumes it believes it still
owns the job.

So I added Kleppmann's **fencing token**. Every claim increments a `fence` column, and every
write that settles the job names the fence the worker was given:

```sql
UPDATE jobs SET status = 'done' WHERE id = :id AND fence = :fence
```

The resumed zombie's fence is stale, so its update matches zero rows. It can't finish, park, or
heartbeat a job that's moved on.

Two implementation details I'd volunteer:

- **`finish`, `park` and `heartbeat` return a boolean, and ignoring it is the bug.** A fenced
  write matching zero rows isn't an error — it's the *expected* outcome for a zombie — so it has
  to be reported and acted on. Discarding the return value recreates the exact problem the
  fence was added to solve.
- **Expired leases are reclaimed inside `claim()`, not by a sweeper.** A sweeper is a second
  actor with its own schedule, which creates a window where a job is owned by nobody and a
  window where it's owned by two. Folding reclamation into the claim makes "take this job" one
  atomic decision.

### Q9. The fence protects rows. What protects the containers and the proxy routes?

Right, and that's the whole reason CP2 exists. The job fence covers `jobs`. A deploy's effects
are containers on a host and routes in a proxy — and a stale worker that can't write to `jobs`
can still call `docker run`.

So: `apps.desired_generation`, bumped when work is queued, and one rule — **traffic is only
ever granted by a fence-protected database write**:

```sql
UPDATE apps SET live_generation = :generation
 WHERE id = :app AND desired_generation = :generation
```

That's the single publish path. Plus three labels on every Docker resource — `vd.app`,
`vd.generation`, `vd.deployment` — so the reconciler can find and reap orphans.

And then I'd state the claim precisely, because this is where people overclaim:

> A stale worker cannot route traffic, and its leftovers are reaped within one reconcile
> interval.

Not "a stale worker can't do anything." It can start a container; nothing stops that once it
holds the socket. What it can't do is make that container **reachable**, because reachability
requires a publish its generation no longer matches.

### Q10. Walk me through your blue-green cutover. Why that order?

```
start new → health gate (N consecutive) → publish (fenced) → re-assert proxy
          → wait drain → SIGTERM old → verify exit
```

Each step is where it is for a reason:

- **Start before gating**, obviously, but **gate before publishing** — so a failed gate is a
  non-event. The new generation is never published, the old one keeps serving, nothing
  user-visible happened.
- **N consecutive passes, not cumulative.** Three in a row says something about stability;
  three in total says something about patience. A flapping process accumulates three
  eventually. Any failure resets the counter. Defaults: 3 passes, 0.5s apart, 30s timeout.
- **The probe checks two things** — the login gate's `/__vd/health` *and* a raw TCP connect to
  the app port. The gate can be healthy while the app behind it is still importing, and an app
  the gate can't reach isn't ready however healthy the gate claims to be.
- **Publish is the fenced write** from Q9, and it's the only thing that grants traffic.
- **Re-assert the whole route table** rather than patching one route, so the proxy is derived
  from the database rather than incrementally edited.
- **Drain before SIGTERM, and the drain must exceed the longest in-flight request plus the
  keep-alive idle timeout.** Otherwise you cut a connection the proxy was about to reuse.
- **Verify the exit by the port going quiet, never by a pid.** That started as a Windows bug —
  `os.kill(pid, 0)` terminates the process there instead of being a no-op, so my liveness check
  was killing the thing it was measuring. The fix is better anyway: a live pid doesn't mean a
  process is still serving, and a quiet port is a direct statement about what a client can do.

### Q11. How do you know the cutover is actually zero-downtime?

Because I measured it, and because the harness is proven able to see failures.

The test drives steady RPS through a real flip and counts HTTP statuses **and connection
errors** separately. Counting connection errors matters: the failure mode of a bad drain is a
dropped TCP connection, which a status-code-only harness records as nothing at all, because it
never gets a status.

Recorded numbers: a normal cutover gave **307 requests, all 200** in one run and **278, all
200** in a later one, with p50 0.44s, p99 0.68s, max 0.79s.

The negative controls are the important part. A `SIGKILL` instead of drain-then-SIGTERM gives
`{200: 5, 502: 19}` and `{200: 9, 502: 22}` — it fires hard and reliably. A `drain_seconds=0`
run produced `{200: 256, 502: 1}` once and all-200 another time, which is consistent: one
failure in 257 requests is a ~0.4% event and a 234-request run can easily miss it.

Which is exactly why that test **asserts nothing about the failure count** — it asserts enough
requests were made and no wrong rows came back, and records the observed rate in its docstring.
Requiring a rare event would make it flaky. I'd also admit here that I misread this once:
I saw the all-200 drain=0 run and called the negative control broken before reading the test.
I was wrong, and it cost me nothing but a retraction, but the lesson was "verify before
concluding."

And the scope: WebSockets are explicitly excluded. The drain is tuned for request/response
traffic, and a connection designed to outlive that window isn't covered.

### Q12. Tell me about a bug that no amount of testing would have found.

A policy with `USING` but no `WITH CHECK`.

Postgres, when an UPDATE policy has no `WITH CHECK`, **reuses the `USING` expression** as the
check. So a policy missing its `WITH CHECK` behaves identically to a correct one for every
input you can construct — until the day the two expressions should have differed, which is
exactly the case `WITH CHECK` exists for: stopping a user from updating *their own* row into
*someone else's* ownership.

There is no black-box test that finds this. I found it by planting it as a deliberate bug and
watching the behavioural suite shrug.

The fix is a **structural** check: assert every policy has both clauses, explicitly, read out of
the catalog. And the lesson is the design lesson for the whole verifier — behavioural probes
prove real connections get the right answer; structural checks prove the configuration is right
even where behaviour can't tell the difference. Either alone has a blind spot, and this bug is
the proof.

### Q13. And a test that was passing for the wrong reason?

Two, and they're a matched pair.

**First:** the `update_other` and `delete_other` attack probes could never fire. An
`UPDATE ... WHERE id = 7` has to *find* row 7 first, and finding it goes through the **SELECT**
policy. If the SELECT policy is correct, the row is invisible, the UPDATE matches zero rows, and
the probe concludes "blocked" — when the UPDATE policy was never consulted at all. The probe
wasn't wrong about what it observed; it was wrong about what the observation *implied*. I found
it by trying to plant a bug in the UPDATE policy and watching the probe stay green. Those
policies are now verified structurally instead.

**Second:** an insert probe that treated *any* exception as evidence of enforcement. A
`NOT NULL` violation or a missing column counted as "RLS blocked it." Now only SQLSTATE
`42501` — `insufficient_privilege` — counts as a pass, and any other error is a **failure**,
because it means the probe never tested what it claimed to. Otherwise one column rename turns
it into an unconditional pass that tests nothing, forever.

The general rule: **a negative test must assert the reason, not just the outcome.**

### Q14. Why won't your reconciler start an app? That seems like its job.

Because a controller that starts things is a controller that can fight the pipeline.

The deploy path and the reconcile loop would both be converging the same resource with
different information, and the reconciler's information is always a snapshot. The bad case is a
flapping app being restarted forever by a loop that can't see why it died, racing a pipeline
that's mid-way through replacing it.

So the reconciler's job is strictly: adopt what exists (by label, so a previous worker's
container is recognised rather than duplicated), reap what's stale, restate the whole route
table from the database, unstick stranded deployments — and **report** what it can't fix. A
generation the database says is live with nothing listening is reported as `lost` and left
alone. That's a signal to a human.

Two details inside the loop I'd mention because they're where the bugs were:

- **Order matters: processes, then unstick, then `proxy.reassert` last.** The route table has to
  be written from the final state, not from one that's about to change.
- **`keep` is a set, not a value.** The naive version keeps "the generation holding traffic,"
  which reaps the brand-new generation a pipeline is still starting, because it doesn't hold
  traffic yet. So `keep` is the traffic holder *plus* every generation held by a live lease.
  Planting the scalar version fails exactly 2 tests.

It's also idempotent, and that's tested by running it twice and requiring the second pass to
report no drift.

### Q15. Why is the proxy config written from the database every ten seconds instead of just on deploy?

Because if the proxy's config is the source of truth, every partial failure leaves it
authoritative and wrong — a route to a dead generation, or a missing route for a live one, with
nothing to correct it.

If the database is the source of truth, the proxy becomes a cache that's continually refreshed,
and "re-assert" becomes a meaningful operation. That's what lets the loop fix a restarted or
hand-edited proxy. The `Caddyfile` has no routes in it and never did.

The cost, which I'd state: an operator can't usefully hand-edit routes; their change is gone
within ten seconds. That's a documented property.

It also produced a genuinely instructive bug. POSTing into a **freshly restarted, empty** Caddy
returns HTTP 500 `invalid traversal path`, because the admin API won't create intermediate
objects in its config tree — if `apps` doesn't exist, it refuses the traversal. A 533-test suite
missed it completely, because every test shared one long-running Caddy that had already been
configured. It only appears against a virgin proxy — which is the state of every real first
deploy and every post-reboot recovery. There's now a dedicated test for exactly that, and a
rule never to `POST /config/`.

### Q16. What's your threat model, and what are you explicitly not defending against?

Defended: an app that forgets its `WHERE` clause; an app with a SQL injection hole (injected
SQL still runs as `runtime` under the same policies); a missing or wrong policy; identity
leaking across pooled connections; an agent exceeding its person; a forged or replayed GitHub
webhook; a paused worker resuming; a stale worker routing traffic; untrusted migration code
reading other apps' data; a committed `.env` becoming live config; the first stranger claiming
a fresh install; audit tampering.

**Not defended**, and this list is in the product's own reports, not just in my head:

- **The platform operator.** They hold the socket and the keys.
- **Container escape.** The worker has the host's Docker socket. Anything that can reach that
  socket can ask for a privileged container, so **the worker is root-equivalent on the host
  whatever uid it runs as.** Per-app isolation is defence in depth, **not a sandbox.** That
  sentence is a comment at the top of the production compose file, not buried in a doc.
- The app's logic above the database; side channels; the `migrator` role; network isolation
  between apps; denial of service; any database reached outside the proven schema.

I'd make a point of saying that the out-of-scope list ships with the evidence report. A report
that doesn't carry its own scope invites the reader to assume the scope is "everything," and
that assumption is how security tooling gets people hurt.

### Q17. Which grants do you never issue, and why those?

`TRUNCATE`, `REFERENCES`, `TRIGGER`. And never `ALTER DEFAULT PRIVILEGES`. Grants always go
last.

- **`TRUNCATE` bypasses RLS entirely** — it's a table-level operation, so no row policy
  applies. Granting it to `runtime` hands an app the ability to delete every tenant's rows in
  one statement, with every policy intact and none violated.
- **`REFERENCES`** lets a role create a foreign key to a table, which leaks existence
  information about rows it can't read and constrains the referenced table's behaviour.
- **`TRIGGER`** lets a role attach code that runs with the table owner's rights. That's a
  privilege-escalation primitive handed over as a convenience.
- **`ALTER DEFAULT PRIVILEGES`** is an invisible, forward-dated grant to objects that don't
  exist yet and have never been attacked.
- **Grants last** so the window between "object exists" and "object is policied" is never also
  a window where it's reachable.

### Q18. Why run untrusted migrations in a container *and* against a separate Postgres instance? Isn't the container enough?

No, because the container boundary and the credential boundary are different boundaries.

The build job runs the app's own migrations, which means it must be handed admin rights over
whatever database it's pointed at — a migration legitimately creates tables and types. Point it
at the customer instance and you've handed code you have never read admin over every other
app's data. The container doesn't help with that; the credential is inside the container.

So there's a second Postgres instance, `buildpg`, with its own credentials and **no volume** —
a restart is a clean slate, which is what throwaway means. And the env template requires its
password to be *different* from the other two, with the reason inline: this is the one
credential handed to a container running code we've never read.

The best part of this story is how the misconfiguration was found. Before the container
existed, migrations ran as a subprocess in the worker, and the DSN was pointing at the
**customer** database. Nothing failed, because the subprocess had those credentials anyway.
Moving to a container — which gets only what it's given — turned a silent security bug into a
loud configuration error.

That's the lesson I'd lead with: **an isolation boundary is also a detector.** A process
holding every credential can't tell you it used the wrong one.

### Q19. If the Docker socket makes the worker root-equivalent, what's the point of the isolation?

Two honest answers.

First, it's **defence in depth and labelled as such**. Isolation between apps raises the cost
of an escalation chain and limits the damage of a non-root mistake, which is most mistakes. It
doesn't contain a determined attacker who already has the socket, and the documentation says
that in those words.

Second, and more importantly: **the enforcement the product actually rests on is in Postgres,
which is why that's where it was put.** The security claim isn't "apps can't reach each other
on the network." It's "a logged-in user only gets their own rows, even if the app's SQL is
wrong." That claim is enforced by role attributes and row policies inside the database, and it
survives container-level problems entirely.

The design response to the socket is to make it a *named* boundary: exactly one service gets
that mount, it's the only service that can start a container, and the compose file says so at
the top. The worker also isn't run as root — not because that's a privilege boundary (it
isn't), but because all three platform processes write to a shared state root and one of them
doing it as root would leave files the other two can't read. I'd be explicit that the uid is
about file ownership, not containment, because claiming otherwise would be the dishonest
version of this answer.

## 11.C The agent half

### Q20. How do you stop an AI agent from exceeding the user it acts for?

It connects as a separate Postgres role, `agent`, with **the same policies**, and identity is
set the same way — `set_config('app.user_id', ...)` with the id of the person it acts for.
There is no second authorization model to get out of sync.

And then the claim is *tested*: the entire adversarial attack suite runs twice, once as
`runtime` and once as `agent`, and the results must be identical. There's a dedicated
`agent_differs` check and any divergence blocks the deploy. That parity run is the whole
justification for the architecture — without it, "the agent gets exactly the person's access"
is a diagram.

On top of that, and strictly as depth: narrow typed tools only, no raw SQL, no shell, no
credentials; a 5-second `statement_timeout` on the role so an agent can't occupy the database;
and dry-run/approval for anything risky.

### Q21. Why are forbidden tools not even listed?

A listed-and-refused tool is an information leak and an invitation — it tells the agent the
capability exists and encourages it to find a path. Practically, "listed but guarded" means
the guard is a conditional somewhere, and conditionals get refactored; absence from the tool
list is enforced by construction.

The deeper reason is that a raw-SQL tool makes every other control cosmetic. Everything in this
system is about constraining what the database will answer. A tool that lets an agent write
arbitrary SQL as a privileged role walks straight around all of it. That's on my short list of
things to refuse outright even if asked for.

### Q22. Explain audit-before-action and why the ordering matters.

The gateway writes the "about to execute" record **before** executing, and if the write fails it
does **not** execute.

Audit-after-action has a window in which an action has happened and no record of it exists, and
that window is exactly the one an incident lands in. Inverting it means the worst case is a
record of something that didn't happen — recoverable, and visibly anomalous — instead of
something that happened with no record, which is not recoverable at all.

The log is hash-chained and append-only. `UPDATE` and `DELETE` on it are things this project
refuses to implement. The chain makes tampering **detectable** rather than prevented — you
can't stop a database admin editing a row, but you can make the edit break every subsequent
hash. Appends take an advisory transaction lock so concurrent writers can't interleave and
produce two rows claiming the same predecessor.

### Q23. Why is an approval bound to a diff hash?

Because an operator approves *a described effect*, not an action id.

The flow: dry-run as the agent role produces a diff and a `diff_hash`. The operator sees the
diff and approves. Before executing, the gateway **re-runs the dry run**; if the hash differs,
the data moved between description and execution, so the thing they approved no longer exists.
The approval is void and the action goes back to `pending`.

Executing anyway would be a confused-deputy attack with the operator as the deputy. There's a
15-minute window too — long enough for a human to look, short enough that an approval doesn't
become a standing grant.

### Q24. How do you guarantee an action executes at most once?

A guarded update, letting the database arbitrate:

```sql
UPDATE actions SET status = 'executing'
 WHERE id = :id AND status = :expected
```

Zero rows affected means someone else claimed it. The status machine is frozen in a contract
and only the listed transitions are legal.

It's the same trick as the job fence applied to a different table: **make the precondition part
of the write.** Any design where you read the status, decide, and then write is a race, no
matter how short the window.

### Q25. Why is the gateway a separate process from the console?

Because of what each is allowed to read. The gateway may read `vd/apps/*/agent-*` secrets; the
console may read **none** of them.

Two processes is how that's enforced rather than asserted. In one process it would be a
convention — a comment saying "don't read agent secrets from the console router" — and a
convention is one careless import away from being false.

It has a practical consequence I like: when the console needs to approve an agent action, it
can't reach for the secret itself. It asks the gateway to redo the dry run, authenticating with
`VD_GATEWAY_API_KEY`. The capability stays on one side of the line.

## 11.D Process, judgement and self-criticism

### Q26. What's the single practice from this project you'd take anywhere?

Recording **exactly how many tests fail** when you deliberately sabotage a feature.

Examples from this project: planting `USING (true)` fails 18 tests. Making the webhook
verifier `return True` fails exactly 4. Randomising the delivery id fails exactly 2. Dropping
`desired_generation` fails exactly 4. Making `proxy.require` a no-op fails exactly 1. Deleting
the fence check fails 5.

The count is diagnostic in both directions. **Zero** means the suite doesn't test the feature at
all — that's how I found the `WITH CHECK` and `update_other` problems. **One** is often ideal:
a specific, well-targeted test. **Eighteen** is also right for a sabotage that breaks the whole
model. And an **unexpected** count means something shifted: if a sabotage that used to fail 4
now fails 2, two tests have stopped testing it, and no number of green runs would ever tell
you.

It converts "we have tests," which is unfalsifiable, into "this sabotage breaks these tests,"
which anyone can check.

### Q27. Tell me about a decision you'd make differently.

Two.

**Logging.** The worker logs nowhere. Claiming jobs, starting containers, health gating,
publishing routes, reconciling drift — none of it produces a durable log. Several bugs in this
project were diagnosed by reasoning backwards from a wrong database row, which is work a log
would have done. It's the highest value-per-effort item outstanding.

The reason it's still open is instructive and slightly embarrassing: **it was never a
checkpoint's "done when."** Every checkpoint had sharp, testable acceptance criteria, and
plan-driven development is extremely good at delivering exactly what's in the plan — and
nothing else. So cross-cutting concerns like logging, backups and CI need to be a checkpoint of
their own or they're permanently the next thing. I'd write an operability checkpoint into the
plan from the start.

**CI.** There is none, and the suite is good enough to deserve one. The obstacle is the
environment, not the tests — it needs Docker, Postgres, PgBouncer, Caddy and twenty minutes,
and two of the test areas conflict over a shared database so they'd have to be serialised. I'd
have paid that setup cost earlier, because the longer a suite only runs on one machine the more
machine-specific it quietly becomes.

### Q28. What's the biggest risk in the project right now?

The production runtime has **never run on real Linux**.

`VD_RUNTIME=apprt` — two containers per generation sharing a network namespace the login gate
owns — is the production default, and it has only ever executed under Docker Desktop. Docker
Desktop masks precisely the differences most likely to break it: `host.docker.internal` exists
there and not on Linux, and a loopback-bound host port is reachable from Desktop's VM but not
from a Linux bridge.

I've already designed around the known cases — every inter-process address in production is a
compose service name rather than a loopback — but this is a category of difference where I've
already been bitten five separate times in this project, so I assume there's a sixth waiting.

Which is why the deploy plan brings the stack up on **`http` first**, before flipping to
`https`. If the first Linux run and the first ACME issuance happen together, the inevitable
first failure has three plausible causes instead of one. **Introduce one unproven thing at a
time.**

### Q29. You deleted a working, tested component. Defend that.

The Node/Prisma identity shim. It worked, it had tests, and I deleted it. Node apps now deploy
and are labelled **"Unprotected."**

The shim is the component that guarantees identity reaches the transaction — the thing the
500-concurrent-request pooling test exists to defend. Getting it right in one language is deep,
adversarial work. Two languages means two of those, and the second would have been maintained
at half the attention.

**A half-attention shim on a security path is worse than no shim**, because "Unprotected" is an
honest label a user can act on, and a weak shim is a false one they'll trust. Narrowing the
supported surface made the remaining guarantee true.

The generalisation: the cost of a feature on a security path isn't the code, it's the attention
it needs forever. I kept a snapshot commit of the pre-deletion tree so nothing was lost, and
wrote "don't rebuild this" into the project notes so the decision doesn't get quietly reversed
by a future me who's forgotten why.

### Q30. Your test suite is twenty minutes. Why haven't you made it faster?

Because the slow part is the part that catches things.

`tests/pipeline` is 174 of the 618 tests and the overwhelming majority of the runtime — real
containers, real `pip install`, real health gates with real sleeps, real drains. The way to
make it fast is to mock the container runtime, the proxy and the health gate.

Those are exactly the components whose real behaviour the suite exists to verify. A mocked suite
would have passed all of these cleanly: the production image with no `docker` binary, Caddy
refusing to be published into when empty, and the build job pointing at the wrong database. A
twenty-minute suite that finds those beats a ninety-second suite that doesn't.

I'd add two caveats so it doesn't sound like complacency. First, the runtime *is* managed: the
concurrency area is run separately because it's contended and distorts everything else, and one
real performance problem was fixed because it blocked a budget — `pip install --target` into a
Windows bind mount took **6m12s** against a 15-minute container timeout, and moving it into a
named volume took it to about **2m01s**. Second, wall time varies between about 4 and 28 minutes
with host load, which is documented so a slow run isn't mistaken for a regression. I once
measured a `docker run` at 156 minutes wall time with 0.2 seconds of user time — the laptop had
slept.

### Q31. How did you keep yourself honest on a solo project?

Four mechanisms, and they're mechanical on purpose, because judgement at 6pm on a red suite is
not reliable.

1. **A written spec that wins.** `Readme.md` is the source of truth, with a standing rule: if
   the code must differ, *flag the conflict* rather than resolve it quietly. There are two
   flagged conflicts in the notes right now — one where `BYPASSRLS` and `SET role` turned out
   to be mutually exclusive, and one where the spec puts the git clone in the build job and the
   code still does it in the worker. Both are open and written down. Do that twice the other way
   and the spec stops describing the system, which is worse than having no spec.
2. **A named list of permanently protected tests.** The pooling test, the planted-bug tests, the
   acts-for escape tests, the audit chain tamper test, the fail-closed tests. They may not be
   deleted, skipped or weakened. The list exists so that making a red suite go away requires an
   explicit act of vandalism rather than a moment of weakness.
3. **"Done when" written before the work, and the full suite green before moving on.** One
   checkpoint at a time.
4. **Negative controls for every prevention claim**, with the failure counts recorded. If I
   haven't seen a test fail, I don't know what it measures. And I had to fix a negative control
   that *didn't* leak when it was supposed to — it needed a `commit()`, and without it the
   absence of a leak was a transaction-visibility artifact rather than evidence of safety.

### Q32. What would you build next?

In order:

1. **CP7 — the chaos suite and `EVIDENCE.md`.** Six failure scenarios as tests with measured
   numbers *including the bad ones*, every claim naming its test, and an explicit statement of
   what is **not** covered. This is the artifact that makes the whole project legible to a
   stranger, and it's the last checkpoint standing between "I believe this" and "you can check
   it."
2. **Structured logging in the worker**, for the reasons in Q27.
3. **Fix the open process-tree bug and delete the legacy runtime.** Stopping the worker kills
   every app it started, because under the bare-process runtime apps are its children. The
   container runtime doesn't have the problem, so the fix is mostly finishing the migration. I'd
   also add a crash test that actually kills a process — the existing ones raise an exception
   inside the worker's loop, which faithfully simulates control flow and cannot by construction
   find process-tree problems.
4. **CI**, once the suite's environment is pinned down.
5. **Backup and restore, exercised.** The notes say to back up the state root. Nothing has ever
   been restored from one, so that's currently a hope rather than a procedure.

Notably **not** next: AWS, Kubernetes, more policy templates, or anything that widens the
surface before the existing surface is proven on a real host.

### Q33. Sell me on the thing you're proudest of.

That the central claim is **falsifiable by a stranger**.

Everything in this project is arranged so somebody who doesn't trust me can check it. The
policies come from five fixed templates, so correctness is a structural question. The
verification is an attack suite that connects as the real low-privilege role, with expectations
derived from the seeding plan rather than from the policies. The evidence report names the
schema hash, the roles, the templates, the personas and the probe count — and carries its own
**not-covered** list. The agent claim is proven by running the same adversarial suite under two
roles and requiring identical answers. The zero-downtime claim comes with measured numbers from
both a working cutover and a deliberately broken one. And every feature has a recorded count of
exactly how many tests break when you sabotage it.

The thing I'd want someone to take away is the inversion: I didn't try to convince anyone the
system is secure. I tried to make it cheap for them to prove it isn't — and then recorded what
happened when I tried.

---

# 12. Known inconsistencies, in this document and in the project

Listed rather than silently resolved, because picking a side without evidence is exactly the
kind of thing this document is supposed to avoid.

## 12.1 "8 structural checks" vs ~14 structural check keys

The progress journal describes the attack suite as **"7 behavioural probes + 8 structural
checks."** The current code — `kernel/messages.py` and `buildjob/attack.py` — carries
**23 message keys**, which break down as roughly **8 behavioural** (including `agent_differs`)
**+ 14 structural + 1 meta.**

**[inferred]** The "8" is most likely stale: structural checks were added over time, including
at least one traceable to a specific bug (the `WITH CHECK` structural assertion in
[§5.11](#511-a-missing-with-check-is-behaviourally-invisible)), and the journal line was never
updated. But the journal was written at the time and the code is current, so rather than assert
which is right, both numbers are given. **The current code is the authority.**

## 12.2 487 test functions vs 618 tests

Both correct. A file-level inventory counts **487 `test_` functions across 42 files**; the
runner collects **618 cases**. The difference is parametrisation — most prominently the
runtime/agent parity axis, which runs the same assertions under two database roles. 618 is the
number that matters, because each parametrised case fails independently.

## 12.3 "533 tests" appears in older notes; the suite is now 618

The gotchas journal says a **533-test** suite missed the empty-Caddy bug. That was the suite
size at the time. The suite is now 618. Both numbers are left as-is: the 533 is part of the
historical record of that bug, and amending it would misrepresent when it was written.

## 12.4 `tests/pipeline`: 165 tests / ~940s vs 174 tests now

The recorded **~940 seconds** figure is for a run of **165** pipeline tests. The directory now
collects **174** and has not been re-timed in isolation. The 940s number is correct for the
run it came from and is now slightly stale. **[verified that both numbers were recorded; the
current per-directory timing is unknown.]**

## 12.5 The `drain_seconds=0` numbers differ between runs

`{200: 256, 502: 1}` in the recorded CP5 run; **all 200** in a later re-run of 234 requests.
Not a contradiction — a ~0.4% event missed by a slightly shorter run. Discussed in
[§6.3](#63-the-cutover-measurement). Both numbers are given rather than the more convenient
one.

## 12.6 Open conflicts between `Readme.md` and the code

| Where | Spec says | Code does | Status |
|---|---|---|---|
| §15 | the git clone happens in the **build job** | the clone happens in the **worker** | **Open and flagged.** Not resolved in either direction |
| §9.1 | `migrator` gets `BYPASSRLS` **and** `SET role = owner` | `BYPASSRLS` only; `SET role` deliberately omitted | Resolved in code, conflict recorded ([§5.3](#53-bypassrls-and-set-role--owner-are-mutually-exclusive--a-spec-conflict)) |
| §14 | patch `Starlette.__init__` | patch `build_middleware_stack` on both Starlette and FastAPI | Resolved in code, reason recorded ([§5.7](#57-readmemd-14-said-to-patch-starlette__init__--it-does-not-reach-fastapi)) |
| contract 7.2 | — | cannot express a **composite foreign key**, so `fk_chain` through one is unsupported | Known limitation, written down |

The project rule is that `Readme.md` wins and divergences get flagged. The §15 clone conflict is
the one still genuinely unresolved, and resolving it is part of the outstanding CP8 work.

## 12.7 Documentation debts in the project

- **`Readme.md` §15 and `infra/local/RUNBOOK.md` have not been updated** for Alembic support or
  for the dependency volume. Both describe an earlier state.
- **`EVIDENCE.md` does not exist.** The reliability claims in this document are backed by the
  test suite, not by the evidence artifact CP7 is supposed to produce.
- **No CI configuration exists** ([§9.6](#96-cicd)).

## 12.8 A memory that was wrong, and was corrected

My own project notes recorded the development Python as **3.12**. The virtualenv is actually
**3.13.7**; the production image is **3.12.15**. The note was corrected at the source rather
than worked around, which is the right handling for a stale fact — but it is worth recording
here as a caution about this document's own provenance: notes written at the time are the best
evidence available, and they are not infallible.

## 12.9 Things this document does not know

- **[unknown]** The order in which most modules were written, or how long any individual bug
  took to diagnose. Four squashed commits.
- **[unknown]** How many approaches were tried and abandoned without leaving a file behind.
- **[unknown]** Total project wall-clock duration.
- **[unknown]** The specific friction encountered with the `mcp` SDK at version 2.3.0. The
  journal has an entry for it; the detail was not available when this document was written, and
  rather than reconstruct something plausible it is left blank.
- **[unknown]** Per-directory test timings other than the two recorded splits.

---

## Closing note on provenance

This document was assembled from `memory/progress.md` (443 lines of milestone records with
non-vacuity proofs), `memory/gotchas.md` (319 lines, one entry per non-obvious failure),
`Readme.md` (the frozen V0.5 spec), `CLAUDE.md` (the non-negotiable rules), the source tree,
and the output of specific test and verification runs.

Everything tagged **[verified]** traces to one of those. Everything tagged **[inferred]** is a
conclusion drawn from them and labelled so you can disagree with it. Everything tagged
**[unknown]** is a question this document cannot answer and declines to invent an answer for.

If one claim in here is worth checking first, check this one: the suite is **618 tests, 618
passing**, and planting `USING (true)` in a single policy template makes **18** of them fail.
The second number is the one that makes the first number mean anything.

