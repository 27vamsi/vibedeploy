# Running the demo locally

Readme.md section 26 is the script. This is the list of commands that puts the
four processes in front of it. Steps 9 and 12 are AWS and are not built.

Everything here is also asserted by tests, so if a step does not behave, the
test named next to it is the shorter way to find out why:

| Steps | Test |
|---|---|
| 1-3, clean branch goes live | `tests/pipeline/test_clean.py` |
| 3, a planted bug blocks the deploy | `tests/pipeline/test_planted_bugs.py` |
| 4-8, 10, 11 | `tests/pipeline/test_demo.py` |

## 0. The two processes that are not ours

```bash
docker compose -f infra/local/docker-compose.yml up -d postgres pgbouncer
```

Postgres on `127.0.0.1:55432`, PgBouncer (transaction mode) on
`127.0.0.1:56432`. Admin is `vd_admin` / `vd_local_password`.

## 1. One environment, four processes

Two secrets have no defaults, because a default nobody chose is not a secret:

```bash
export VD_SIDECAR_API_KEY=$(python -c "import secrets;print(secrets.token_hex(16))")
export VD_GATEWAY_API_KEY=$(python -c "import secrets;print(secrets.token_hex(16))")
```

`VD_SIDECAR_API_KEY` is how a deployed app's sidecar asks the control plane who
is logged in. `VD_GATEWAY_API_KEY` is how the console asks the gateway about
actions in flight — the console holds no `agent-*` secret of its own, which is
the whole reason the gateway is a second process.

Everything else defaults to the compose stack above:
`VD_CONTROL_DATABASE_URL`, `VD_APP_ADMIN_DSN`, `VD_STATE_ROOT` (`.vd-state/`),
`VD_CONTROL_PLANE_URL` (`:8100`), `VD_GATEWAY_URL` (`:8200`).

Create and migrate the control plane database once:

```bash
.venv/Scripts/python.exe -m control_plane.migrate
```

Then, with that environment exported in each shell:

```bash
# the console and the internal API the build job reports back to
.venv/Scripts/python.exe -m uvicorn control_plane.app:create_app --factory --port 8100

# the gateway: /mcp for agents, /internal/* for the console
.venv/Scripts/python.exe -m uvicorn gateway.app:build --factory --port 8200

# the worker, which runs the pipeline one job at a time
.venv/Scripts/python.exe -m worker.main
```

Check they are up:

```bash
curl -s http://127.0.0.1:8100/health
curl -s http://127.0.0.1:8200/internal/health
```

## 2. Steps 1-3: submit a repo and watch it be proved

Open <http://127.0.0.1:8100/>.

1. **New app.** Name it anything; for `repo` use the absolute path of the
   fixture repository, `<repo root>/fixtures/repos/todo`. A local directory with
   `base/` and `branches/<name>/` is a repository as far as the worker is
   concerned, so the demo needs no network.
2. **Deploy** with `commit_sha` = `clean`, and answer the four questions of
   section 10 — for the todo app: *me*, *small*, *own_data*, sensitive *no*.
3. The deployment page shows detection, the derived access model, and the
   attack suite's counts run **as the runtime role and again as the agent
   role**. It then stops and waits, because going live is a person's decision.
   Press **Confirm** and it goes live with an endpoint printed on the page.

To show step 3 refusing instead: deploy `permissive_policy` and the page names
`literal_true`. `membership` is refused at derivation; `unlinked` stops to ask a
question rather than blocking. The table in `fixtures/repos/todo/README.md` says
which check catches which branch.

## 3. Step 4: two people, one unfiltered endpoint

Add two people on the app page — `alice@example.com` and `bob@example.com` —
and a third as `admin`, who will be the one allowed to approve things.

Open the app's endpoint in two browsers, sign in as each at `/__vd/login`, and
give each of them a todo. `fixtures/repos/todo/base/app.py` runs
`SELECT ... FROM todos` with no `WHERE` and contains no auth code at all, and
each browser still sees only its own rows. That is Postgres, not the app.

## 4. Step 5: create the agent

On the app page, follow **Agents**. Switch agents on for the app, then create
one. The policy is contract 7.5; the demo's is:

```yaml
agent: todo-helper
acts_for: alice@example.com
session_ttl_minutes: 30
rate_limit_per_minute: 240
postgres:
  default: {read: auto}
  tables:
    todos: {read: auto, update: auto, delete: approve}
    users: {}
```

The key — `vd_agent_<id>.<secret>` — is printed on that page **once**. Copy it
now; nothing can show it again, and the console never stores it.

Point an MCP client at `http://127.0.0.1:8200/mcp` (Streamable HTTP) with
`Authorization: Bearer vd_agent_...`. `tools/list` offers `db_query`,
`db_update`, `db_delete` and `get_action_status` — and not `db_create`, because
the policy never mentions creating a row and a forbidden tool is not listed at
all.

## 5. Steps 6-8, 10, 11: the part worth watching

- **6.** `db_query` on `todos` returns Alice's rows only. The agent connects as
  the app's `agent` role with Alice's identity set for that transaction.
- **7.** Aim `db_update` at Bob's todo. The answer is "No rows you can access
  matched." — nothing changed, and nothing leaked the existence of the row.
  `db_query` on `users` is refused outright, because `users: {}` permits
  nothing. Both appear on **Activity**.
- **8.** `db_delete` one of Alice's todos. It comes back
  `pending_approval` with an `action_id`; **Approvals** shows what would change
  and its hash, the admin approves, and the row goes. **Activity** then offers
  **Undo**, which puts it back. The agent learns the outcome by calling
  `get_action_status`.
- **10.** The global kill switch, on the same **Agents** page as the per-app
  one, denies the agent's *next* call even though its session is already open:
  the checks in 19.1 are made every time and none of them is cached.
- **11.** **Evidence** is one sentence with the counts under it, computed from
  the hash-chained `audit_log` and nothing else, and downloadable as JSON.
  **Audit** says whether the chain is intact.
