# Running the demo locally

Readme.md section 26 is the script. This is the list of commands that puts the
four processes in front of it. Steps 9 and 12 are AWS and are not built.

Everything here is also asserted by tests, so if a step does not behave, the
test named next to it is the shorter way to find out why:

| Steps | Test |
|---|---|
| 1-3, clean branch goes live | `tests/pipeline/test_clean.py` |
| 3, a planted bug blocks the deploy | `tests/pipeline/test_planted_bugs.py` |
| 2, an app's own settings | `tests/pipeline/test_app_env.py` |
| 3, app code runs nowhere near us | `tests/pipeline/test_isolation.py` |
| 4-8, 10, 11 | `tests/pipeline/test_demo.py` |
| 3-4, the app answers through the proxy | `tests/pipeline/test_proxy.py` |
| 3-4, a second deploy replaces the first without dropping a request | `tests/pipeline/test_cutover.py`, `tests/pipeline/test_zero_downtime.py` |
| a worker killed mid-deploy recovers with nobody looking | `tests/pipeline/test_reconcile.py` |

## 0. The four processes that are not ours

```bash
docker compose -f infra/local/docker-compose.yml up -d
```

| Service | Port | What lives in it |
|---|---|---|
| `postgres` | `127.0.0.1:55432` | the control plane, and every app |
| `pgbouncer` | `127.0.0.1:55433` | the same, in transaction mode |
| `buildpg` | `127.0.0.1:55434` | nothing, on purpose |
| `caddy` | `127.0.0.1:55080` | every deployed app's front door |
| `caddy` admin | `127.0.0.1:55019` | the route table, written by the worker |

Admin is `vd_admin` / `vd_local_password` on the three databases.

`buildpg` is the build job's throwaway (section 18.2). It is a separate
**instance**, not a scratch schema, because the build job is admin in it and the
build job runs the app's own migrations — code we have never read. It has no
compose volume, so it is empty again after a `docker compose down`, and
`VD_BUILD_DATABASE_URL` overrides it.

All three ports are below 55834 on purpose. Hyper-V reserves blocks of high
ports on Windows — `netsh interface ipv4 show excludedportrange protocol=tcp` —
and Docker cannot bind one of those, which looks like "an attempt was made to
access a socket in a way forbidden by its access permissions".

`caddy` is not optional, and `--profile` is why it used to look it. Every app is
published at `http://<subdomain>.localhost:55080` and at no other address: a
deploy with no proxy to publish a route into **blocks**, because the alternative
is handing out the sidecar's port and then "an app is reachable through its
login gate" would hold only until somebody guessed one. There are no site blocks
in the `Caddyfile` — routes are written over the admin API on 55019 from what the
control plane's database says is live, which is what lets them be re-asserted
rather than trusted.

`VD_PROXY_ADMIN_URL`, `VD_APP_DOMAIN` and `VD_APP_PORT` move all three.

> On **Windows**, `*.localhost` does not resolve — `todo.localhost` fails where
> `localhost` works — so a browser there needs a line in
> `C:\Windows\System32\drivers\etc\hosts`, or `curl --resolve`. The test suite
> does neither: it sends the request to `127.0.0.1:55080` with the hostname in a
> `Host:` header, which is all a resolver would have contributed. Caddy routes on
> the header.

Docker itself has to be running for a second reason: an app's migrations only
ever run inside a container, so with no `docker` a deploy blocks rather than
falling back to a subprocess. The image is built on the first deploy that needs
it and cached after that; `VD_BUILD_IMAGE` renames it.

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
# *and* the reconciler, in the same process
.venv/Scripts/python.exe -m worker.main
```

`worker.main` is two loops, not one. The second is the **reconciler**: every
`VD_RECONCILE_INTERVAL` (10s) it compares what the control plane database says
should be live against what is actually on this machine — run records on disk
and Caddy's own route table — and closes the gap. It states the answer again
rather than undoing anything:

- a process for a generation nothing claims is stopped, which is what makes a
  killed worker's leftovers temporary rather than merely harmless;
- the whole route table is written again from the database, so a hand-edited
  Caddy, a stale worker's write, and a Caddy restarted empty are one problem
  with one fix;
- a deployment stuck in `building`, `verifying` or `deploying` with no worker
  holding its job is either left for the queue to resume or **blocked** with a
  sentence, depending on whether a build job was handed a one-shot token that
  nothing will ever spend.

It will not start an app. If the database says a generation is live and nothing
is listening, that is reported as `lost` and left alone, because restarting it
means re-running the app's migrations and re-applying its policies — which is a
deployment, and a deployment is a thing a person asks for. Reviving apps after
the machine rebooted is out of scope for V0.5 and the loop says so rather than
looking like it covers it.

Killing the worker mid-deploy is therefore a reasonable thing to do in front of
an audience: `tests/pipeline/test_reconcile.py` does it at five named points
(after the clone, during a build that never reported, after the migration,
mid-cutover, and after the flip before the drain) and each one ends somewhere
correct without anybody intervening.

Check they are up:

```bash
curl -s http://127.0.0.1:8100/health
curl -s http://127.0.0.1:8200/internal/health
```

## 2. Steps 1-3: submit a repo and watch it be proved

Open <http://127.0.0.1:8100/>.

1. **New app.** Any of three things work for `repo`:
   - a **git URL** (`https://github.com/you/your-app`) — public repositories
     only in V0.5. The worker clones it and records the commit it resolved;
   - a **local folder** (`C:\path\to\your-app`). No git and no network needed;
   - the absolute path of the fixture repository,
     `<repo root>/fixtures/repos/todo`. A local directory with `base/` and
     `branches/<name>/` is a repository as far as the worker is concerned.
     This is how the planted bugs are planted.
2. **Deploy** with the ref, which is what decides *which version* is built:
   - a branch, tag or commit (`main`) for a git repository;
   - **`working-tree`** for a folder: exactly what is on disk, uncommitted
     changes included. This also works on a folder that *is* a git repository,
     which is the point — asking for the folder and getting its last commit
     would run code you are not looking at.
   - `clean` (or any branch name) for the fixture repository.

   Then answer the four questions of section 10 — for the todo app: *me*,
   *small*, *own_data*, sensitive *no*.

   Never copied into a deployment: `.env` and `.env.*`, `.git`, `.venv`,
   `node_modules`, `__pycache__`. The `.env` exclusion is not tidiness: an app
   that loads its own `.env` and builds its own engine from the `DATABASE_URL`
   in it is talking to a database with no identity and no policies on it, which
   is outside everything this proves. The runtime DSN is handed to the app by
   the platform, as both `DATABASE_URL` and `VD_DATABASE_URL`.

   Anything else that file held goes in **Settings** on the app page, pasted in
   the same `NAME=value` shape, and arrives as environment variables at the next
   deploy. The page lists the names it holds and never the values. Refused, with
   the name said back: `DATABASE_URL`, `VD_*`, `PYTHON*`, `PATH`, `LD_*`,
   `DYLD_*` — they decide which database the app reaches, who it thinks is
   asking, and which code runs. `tests/pipeline/test_app_env.py`.
3. The deployment page shows detection, the derived access model, and the
   attack suite's counts run **as the runtime role and again as the agent
   role**. It then stops and waits, because going live is a person's decision.
   Press **Confirm** and it goes live, with its address —
   `http://<subdomain>.localhost:55080` — printed on the page.

**Deploying the same app again** is where the interesting part is. The new
version is started while the old one is still serving, watched until it has
answered `VD_HEALTH_PASSES` times *in a row* (3, every `VD_HEALTH_INTERVAL`
0.5s, giving up after `VD_HEALTH_TIMEOUT` 30s), and only then given the route.
The old one is drained for `VD_DRAIN_SECONDS` (2) before it is signalled, and the
worker then waits for its ports to go quiet rather than trusting the signal.

A version that starts and does not stay healthy is **blocked**, and blocking
revokes nothing: the route was never moved and the old process was never
stopped, so the people using the app see no interruption at all.

Measured, not asserted — `tests/pipeline/test_zero_downtime.py` prints it:
~300 authenticated requests at 8/s straight through a real redeploy, **0 non-2xx
and 0 connection errors**, with p50 unchanged and the worst single request about
1.3s while RLS is re-applied.

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
