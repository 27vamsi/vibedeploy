# vibedeploy

`Readme.md` at the repo root is the source of truth for V0.5. Read it before writing code.
If something here or in the code conflicts with `Readme.md`, follow `Readme.md` and flag the conflict.

## Non-negotiable rules (see Readme.md section 3)

- Fail closed: no identity => zero rows, never "everything".
- Postgres RLS decides access, not app code. `FORCE ROW LEVEL SECURITY` on every table.
- Three roles per app: `owner` (NOLOGIN), `migrator` (BYPASSRLS, never given to the app), `runtime` (NOBYPASSRLS, not owner).
- Identity is set only via `set_config('app.user_id', $1, true)` inside a transaction with bind params. Never plain `SET`, never string-formatted SQL.
- Never guess the access model. Ask the builder; no answer blocks launch.
- Policies come from the 5 templates only: `owner_column`, `fk_chain`, `shared`, `read_only_shared`, `admin_only`. No `USING (true)`.
- Detection is deterministic. No LLM decides anything in V0.5.
- Test expectations come from the seeding plan, never from reading policies.
- Untrusted app code (migrations) runs only in CodeBuild or a one-off ECS task, never in the worker or control plane.
- No `ALTER DEFAULT PRIVILEGES`.
- Local first.

## Working style

- One milestone at a time (M1..M12 in Readme.md section 8). Use that milestone's "Done when" as acceptance criteria.
- For M1, M2, M5: write the failing test first.
- Never delete or skip the concurrency test or the planted-bug tests to make CI green.
- Contracts in Readme.md section 7 are frozen: identity header format, access model JSON schema, policy templates, build-job callback JSON. Do not change them without asking.
