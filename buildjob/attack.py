"""The attack. Readme.md section 13.

Two halves, both of which have to pass:

  - **Behavioural.** Act as each persona and try to do the thing they must not
    be able to do. Every probe runs in its own transaction and is rolled back,
    so a check that succeeds when it should not cannot poison the next one.
    Expectations come from the seed plan, never from reading a policy back.
  - **Structural.** Ask the database how it is configured. A policy that is
    perfect but sits on a table with RLS switched off proves nothing, and no
    behavioural probe would notice as long as the probe's own persona happens
    to own the rows it looks at.

The whole suite then runs a second time as the **agent** role and the two runs
are compared check by check. A difference is itself a failure: an agent that
can do something its person cannot is the exact thing this product claims is
impossible.

Every failure is worded by `kernel.messages`. Nothing here writes a sentence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import asyncpg

from buildjob.introspect import introspect
from buildjob.seed import SeedPlan, Values, build_row
from kernel.messages import message
from kernel.provision import AppRoles
from kernel.render import quote_ident

MEMBERS = ("A", "B")
PERSONAS = ("A", "B", "ADMIN", "NONE")
OTHER = {"A": "B", "B": "A"}

# The only functions in an app schema that are allowed to be SECURITY DEFINER
# are none: ours are deliberately SECURITY INVOKER so they carry no privilege.
OUR_FUNCTIONS = ("vd_user_id", "vd_role")

DANGEROUS_PRIVILEGES = ("TRUNCATE", "REFERENCES", "TRIGGER")

COMMANDS = ("SELECT", "INSERT", "UPDATE", "DELETE")

# Postgres raises 42501 for "new row violates row-level security policy". That
# is the only error that counts as a write being refused by a policy; anything
# else means the probe failed for its own reasons and proved nothing.
DENIED = asyncpg.InsufficientPrivilegeError


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    table: str | None = None
    persona: str | None = None
    role: str | None = None
    message: str | None = None

    @property
    def identity(self) -> tuple:
        """What has to match between the runtime run and the agent run."""
        return (self.name, self.table, self.persona, self.ok)


@dataclass
class Report:
    """Contract 7.4's `verification` block, plus the checks behind it."""

    checks: list[Check]

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def passed(self) -> int:
        return self.total - len(self.failures)

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_contract(self) -> dict:
        return {
            "total": self.total,
            "passed": self.passed,
            "failures": [
                {
                    "check": c.name,
                    "table": c.table,
                    "persona": c.persona,
                    "role": c.role,
                    "message": c.message,
                }
                for c in self.failures
            ],
        }


def _ok(name: str, **fields) -> Check:
    return Check(name=name, ok=True, **fields)


def _fail(name: str, *, table=None, persona=None, role=None, **words) -> Check:
    return Check(
        name=name,
        ok=False,
        table=table,
        persona=persona,
        role=role,
        message=message(name, table=table, persona=persona, role=role, **words),
    )


# --------------------------------------------------------------------------
# Expectations, from the plan
# --------------------------------------------------------------------------


def expected_visible(
    model: Mapping[str, Any], plan: SeedPlan, table: str, persona: str
) -> set[tuple]:
    """Exactly which rows this persona is allowed to see. The answer key.

    Derived from what the seeder inserted and chose to own, so a policy that is
    wrong in either direction (too few rows, too many) is caught by the same
    comparison.
    """
    template = model["tables"][table]["template"]
    if persona == "NONE":
        return set()
    if template == "admin_only":
        return plan.keys(table) if persona == "ADMIN" else set()
    if template in ("shared", "read_only_shared"):
        return plan.keys(table)
    if persona == "ADMIN" and model["admin_enabled"]:
        return plan.keys(table)
    return plan.owned_by(table, persona)


# --------------------------------------------------------------------------
# Acting as a persona
# --------------------------------------------------------------------------


async def _set_identity(conn, plan: SeedPlan, persona: str | None, blank: bool) -> None:
    """Readme.md section 14 step 2, and nothing else.

    Bind parameters, transaction-local, both settings every time. `blank` is
    the shim's own "nobody is logged in" state: empty strings, which must be as
    dead as never having set anything at all.
    """
    if persona is None and not blank:
        return
    if blank or persona == "NONE":
        user_id, role = "", ""
    else:
        user_id = str(plan.user_ids[persona])
        role = "admin" if persona == "ADMIN" else "member"
    await conn.execute(
        "SELECT set_config('app.user_id', $1, true),"
        "       set_config('app.role', $2, true)",
        user_id,
        role,
    )


class _Probe:
    """One rolled-back transaction, acting as one persona."""

    def __init__(self, conn, plan, persona, *, blank: bool = False):
        self._conn = conn
        self._plan = plan
        self._persona = persona
        self._blank = blank
        self._tx = None

    async def __aenter__(self):
        self._tx = self._conn.transaction()
        await self._tx.start()
        await _set_identity(self._conn, self._plan, self._persona, self._blank)
        return self._conn

    async def __aexit__(self, *exc):
        await self._tx.rollback()
        return False


# --------------------------------------------------------------------------
# SQL fragments
# --------------------------------------------------------------------------


def _target(roles: AppRoles, table: str) -> str:
    return f"{quote_ident(roles.schema)}.{quote_ident(table)}"


def _key_list(pk: Sequence[str]) -> str:
    return ", ".join(quote_ident(c) for c in pk)


def _key_predicate(pk: Sequence[str]) -> str:
    return " AND ".join(f"{quote_ident(c)} = ${i + 1}" for i, c in enumerate(pk))


def _touchable(graph, table: str) -> str:
    """A column we can set to itself, to test USING without tripping a check.

    Assigning a column its own value leaves the row identical, so a policy's
    WITH CHECK can never be the reason the statement is refused. Whatever
    happens is USING's doing, which is what this probe is about.
    """
    entry = graph["tables"][table]
    pk = set(entry["primary_key"])
    for column in entry["columns"]:
        if column["name"] not in pk:
            return column["name"]
    return entry["primary_key"][0]


# --------------------------------------------------------------------------
# Behavioural checks
# --------------------------------------------------------------------------


async def _read_all(conn, roles, graph, model, plan, table, persona) -> Check:
    pk = graph["tables"][table]["primary_key"]
    async with _Probe(conn, plan, persona) as c:
        rows = await c.fetch(
            f"SELECT {_key_list(pk)} FROM {_target(roles, table)}"
        )
    got = {tuple(r) for r in rows}
    expected = expected_visible(model, plan, table, persona)
    if got == expected:
        return _ok("read_all", table=table, persona=persona)
    return _fail(
        "read_all",
        table=table,
        persona=persona,
        expected=len(expected),
        got=len(got),
    )


def _forbidden_key(model, plan, table, persona) -> tuple | None:
    """A key this persona must not be able to reach. None means no probe."""
    allowed = expected_visible(model, plan, table, persona)
    for key in sorted(plan.owned_by(table, OTHER[persona]), key=repr):
        if key not in allowed:
            return key
    return None


async def _read_other(conn, roles, graph, plan, table, persona, key) -> Check:
    pk = graph["tables"][table]["primary_key"]
    async with _Probe(conn, plan, persona) as c:
        rows = await c.fetch(
            f"SELECT {_key_list(pk)} FROM {_target(roles, table)} "
            f"WHERE {_key_predicate(pk)}",
            *key,
        )
    if not rows:
        return _ok("read_other", table=table, persona=persona)
    return _fail("read_other", table=table, persona=persona)


async def _update_other(conn, roles, graph, plan, table, persona, key) -> Check:
    """Postgres applies the SELECT policy here too, because the WHERE clause
    reads a column. So this probe only fires once SELECT is wrong as well. It
    stays a separate check anyway: that overlap is a detail of how Postgres
    happens to evaluate UPDATE, not something the product may rely on."""
    pk = graph["tables"][table]["primary_key"]
    column = quote_ident(_touchable(graph, table))
    async with _Probe(conn, plan, persona) as c:
        try:
            rows = await c.fetch(
                f"UPDATE {_target(roles, table)} SET {column} = {column} "
                f"WHERE {_key_predicate(pk)} RETURNING {_key_list(pk)}",
                *key,
            )
        except asyncpg.PostgresError:
            # Reaching the row at all is the failure. An error here means the
            # statement got past USING and tripped over something else.
            return _fail("update_other", table=table, persona=persona)
    if not rows:
        return _ok("update_other", table=table, persona=persona)
    return _fail("update_other", table=table, persona=persona)


async def _delete_other(conn, roles, graph, plan, table, persona, key) -> Check:
    pk = graph["tables"][table]["primary_key"]
    async with _Probe(conn, plan, persona) as c:
        try:
            rows = await c.fetch(
                f"DELETE FROM {_target(roles, table)} "
                f"WHERE {_key_predicate(pk)} RETURNING {_key_list(pk)}",
                *key,
            )
        except asyncpg.PostgresError:
            # A foreign key complaint proves the delete found the row, which
            # means the policy did not hide it. Still a failure.
            return _fail("delete_other", table=table, persona=persona)
    if not rows:
        return _ok("delete_other", table=table, persona=persona)
    return _fail("delete_other", table=table, persona=persona)


def _insert_sql(roles, table: str, row: Mapping[str, Any]) -> tuple[str, list]:
    columns = sorted(row)
    if not columns:
        return f"INSERT INTO {_target(roles, table)} DEFAULT VALUES", []
    placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
    sql = (
        f"INSERT INTO {_target(roles, table)} "
        f"({', '.join(quote_ident(c) for c in columns)}) "
        f"VALUES ({placeholders})"
    )
    return sql, [row[c] for c in columns]


async def _insert_as_other(
    conn, roles, graph, model, plan, values, table, persona
) -> Check:
    template = model["tables"][table]["template"]
    victim = OTHER[persona] if template in ("owner_column", "fk_chain") else None
    row = build_row(graph, model, plan, values, table, victim)
    sql, args = _insert_sql(roles, table, row)
    async with _Probe(conn, plan, persona) as c:
        try:
            await c.execute(sql, *args)
        except DENIED:
            return _ok("insert_as_other", table=table, persona=persona)
        except asyncpg.PostgresError:
            # Refused, but not by a policy. That proves nothing, so it is not
            # allowed to count as a pass.
            return _fail("insert_as_other", table=table, persona=persona)
    return _fail("insert_as_other", table=table, persona=persona)


def _reassign_values(graph, model, plan, table, persona) -> dict | None:
    """The columns to overwrite so the row lands in the other persona's name."""
    entry = model["tables"][table]
    victim = OTHER[persona]
    if entry["template"] == "owner_column":
        return {entry["column"]: plan.user_ids[victim]}
    if entry["template"] != "fk_chain":
        return None
    hop = entry["path"][0]
    columns = [hop["column"]] if "column" in hop else list(hop["columns"])
    parents = (
        [hop["to_column"]] if "to_column" in hop else list(hop["to_columns"])
    )
    parent = plan.one_owned_by(hop["to"], victim)
    if parent is None:
        return None
    return dict(zip(columns, (parent.values[p] for p in parents), strict=True))


async def _reassign(conn, roles, graph, model, plan, table, persona) -> Check:
    mine = plan.one_owned_by(table, persona)
    overwrite = _reassign_values(graph, model, plan, table, persona)
    if mine is None or overwrite is None:
        return _fail("seed_missing_rows", table=table, persona=persona)

    pk = graph["tables"][table]["primary_key"]
    columns = sorted(overwrite)
    assignments = ", ".join(
        f"{quote_ident(c)} = ${i + 1}" for i, c in enumerate(columns)
    )
    predicate = " AND ".join(
        f"{quote_ident(c)} = ${len(columns) + i + 1}" for i, c in enumerate(pk)
    )
    args = [overwrite[c] for c in columns] + list(mine.key)

    async with _Probe(conn, plan, persona) as c:
        try:
            await c.execute(
                f"UPDATE {_target(roles, table)} SET {assignments} "
                f"WHERE {predicate}",
                *args,
            )
        except DENIED:
            return _ok("reassign", table=table, persona=persona)
        except asyncpg.PostgresError:
            return _fail("reassign", table=table, persona=persona)
    return _fail("reassign", table=table, persona=persona)


async def _no_identity(
    conn, roles, graph, model, plan, values, table, *, blank: bool
) -> list[Check]:
    """No logged-in person means nothing, in all four directions.

    Run twice by the caller: once having set nothing at all, and once with the
    empty strings a shim writes when there is no identity. Those two must be
    indistinguishable, which is the whole point of the `NULLIF` in the helpers.
    """
    pk = graph["tables"][table]["primary_key"]
    target = _target(roles, table)
    column = quote_ident(_touchable(graph, table))
    key = next(iter(sorted(plan.keys(table), key=repr)), None)
    out: list[Check] = []

    async with _Probe(conn, plan, None, blank=blank) as c:
        rows = await c.fetch(f"SELECT {_key_list(pk)} FROM {target}")
    out.append(
        _ok("no_identity", table=table)
        if not rows
        else _fail("no_identity", table=table, action="read")
    )

    if key is not None:
        for action, sql in (
            (
                "change",
                f"UPDATE {target} SET {column} = {column} "
                f"WHERE {_key_predicate(pk)} RETURNING 1",
            ),
            (
                "delete",
                f"DELETE FROM {target} WHERE {_key_predicate(pk)} RETURNING 1",
            ),
        ):
            async with _Probe(conn, plan, None, blank=blank) as c:
                try:
                    touched = await c.fetch(sql, *key)
                except asyncpg.PostgresError:
                    out.append(_fail("no_identity", table=table, action=action))
                    continue
            out.append(
                _ok("no_identity", table=table)
                if not touched
                else _fail("no_identity", table=table, action=action)
            )

    row = build_row(graph, model, plan, values, table, "A")
    sql, args = _insert_sql(roles, table, row)
    async with _Probe(conn, plan, None, blank=blank) as c:
        try:
            await c.execute(sql, *args)
        except DENIED:
            out.append(_ok("no_identity", table=table))
        except asyncpg.PostgresError:
            out.append(_fail("no_identity", table=table, action="create"))
        else:
            out.append(_fail("no_identity", table=table, action="create"))
    return out


async def behavioural(
    conn: asyncpg.Connection,
    roles: AppRoles,
    graph: Mapping[str, Any],
    model: Mapping[str, Any],
    plan: SeedPlan,
) -> list[Check]:
    """Every probe, as every persona, against every table in the model."""
    values = Values(graph["enums"])
    checks: list[Check] = []

    for table in sorted(model["tables"]):
        template = model["tables"][table]["template"]

        for persona in PERSONAS:
            checks.append(
                await _read_all(conn, roles, graph, model, plan, table, persona)
            )

        for persona in MEMBERS:
            key = _forbidden_key(model, plan, table, persona)
            if key is not None:
                checks.append(
                    await _read_other(conn, roles, graph, plan, table, persona, key)
                )
                checks.append(
                    await _update_other(conn, roles, graph, plan, table, persona, key)
                )
                checks.append(
                    await _delete_other(conn, roles, graph, plan, table, persona, key)
                )
            if template != "shared":
                checks.append(
                    await _insert_as_other(
                        conn, roles, graph, model, plan, values, table, persona
                    )
                )
            if template in ("owner_column", "fk_chain"):
                checks.append(
                    await _reassign(conn, roles, graph, model, plan, table, persona)
                )

        for blank in (False, True):
            checks.extend(
                await _no_identity(
                    conn, roles, graph, model, plan, values, table, blank=blank
                )
            )

    return checks


# --------------------------------------------------------------------------
# Structural checks
# --------------------------------------------------------------------------


_ROLE_FLAGS = """
SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = $1
"""

_ROLE_OWNS = """
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_roles r ON r.oid = c.relowner
WHERE n.nspname = $1 AND r.rolname = $2 AND c.relkind IN ('r', 'p', 'v', 'm')
ORDER BY c.relname
"""

_ROLE_MEMBER_OF = """
SELECT 1 FROM pg_auth_members m
JOIN pg_roles child ON child.oid = m.member
JOIN pg_roles parent ON parent.oid = m.roleid
WHERE child.rolname = $1 AND parent.rolname = $2
"""


def _is_literal_true(expression: str | None) -> bool:
    return expression is not None and expression.strip().lower() in ("true", "(true)")


def _policy_checks(graph, roles, table) -> list[Check]:
    policies = graph["policies"].get(table, [])
    out: list[Check] = []

    for command in COMMANDS:
        applicable = [
            p for p in policies if p["command"] in (command, "ALL")
        ]
        if not applicable:
            out.append(_fail("missing_policy", table=table, command=command))
            continue
        out.append(_ok("missing_policy", table=table))

        for role in roles.app_facing:
            if any(role in p["roles"] for p in applicable):
                out.append(_ok("policy_role_missing", table=table, role=role))
            else:
                out.append(
                    _fail(
                        "policy_role_missing",
                        table=table,
                        role=role,
                        command=command,
                    )
                )

        if command in ("INSERT", "UPDATE"):
            if any(p["check"] is not None for p in applicable):
                out.append(_ok("missing_check", table=table))
            else:
                out.append(_fail("missing_check", table=table, command=command))

    for policy in policies:
        bad = _is_literal_true(policy["using"]) or _is_literal_true(policy["check"])
        if bad:
            out.append(
                _fail("literal_true", table=table, command=policy["command"])
            )
        else:
            out.append(_ok("literal_true", table=table))

    return out


async def structural(
    conn: asyncpg.Connection,
    roles: AppRoles,
    model: Mapping[str, Any],
) -> list[Check]:
    """Ask the live database how it is set up, and hold it to section 13.

    Read on a connection that can see `pg_catalog` fully. Nothing here trusts
    the access model except for the list of tables it claims to cover.
    """
    graph = await introspect(conn, roles.schema)
    checks: list[Check] = []

    if not model["tables"]:
        return [_fail("no_protected_tables")]

    for table in sorted(graph["tables"]):
        if table not in model["tables"]:
            checks.append(_fail("unclassified_table", table=table))
        else:
            checks.append(_ok("unclassified_table", table=table))

    for table in sorted(model["tables"]):
        entry = graph["tables"][table]
        checks.append(
            _ok("rls_disabled", table=table)
            if entry["rls_enabled"]
            else _fail("rls_disabled", table=table)
        )
        checks.append(
            _ok("force_disabled", table=table)
            if entry["rls_forced"]
            else _fail("force_disabled", table=table)
        )
        checks.extend(_policy_checks(graph, roles, table))

        for role in roles.app_facing:
            for privilege in DANGEROUS_PRIVILEGES:
                held = await conn.fetchval(
                    "SELECT has_table_privilege($1, $2, $3)",
                    role,
                    f"{quote_ident(roles.schema)}.{quote_ident(table)}",
                    privilege,
                )
                checks.append(
                    _fail(
                        "dangerous_grant",
                        table=table,
                        role=role,
                        privilege=privilege,
                    )
                    if held
                    else _ok("dangerous_grant", table=table, role=role)
                )

    for role in roles.app_facing:
        flags = await conn.fetchrow(_ROLE_FLAGS, role)
        checks.append(
            _fail("role_bypassrls", role=role)
            if flags["rolbypassrls"]
            else _ok("role_bypassrls", role=role)
        )
        checks.append(
            _fail("role_superuser", role=role)
            if flags["rolsuper"]
            else _ok("role_superuser", role=role)
        )

        owned = await conn.fetch(_ROLE_OWNS, roles.schema, role)
        if owned:
            for row in owned:
                checks.append(
                    _fail("role_is_owner", table=row["relname"], role=role)
                )
        else:
            checks.append(_ok("role_is_owner", role=role))

        member = await conn.fetchval(_ROLE_MEMBER_OF, role, roles.owner)
        checks.append(
            _fail("role_in_owner_group", role=role, owner=roles.owner)
            if member
            else _ok("role_in_owner_group", role=role)
        )

    for view, entry in sorted(graph["views"].items()):
        checks.append(
            _ok("view_not_security_invoker", table=view)
            if entry["security_invoker"]
            else _fail("view_not_security_invoker", table=view, view=view)
        )

    for function in graph["functions"]:
        safe = not function["security_definer"]
        checks.append(
            _ok("security_definer_function")
            if safe
            else _fail("security_definer_function", function=function["name"])
        )

    return checks


# --------------------------------------------------------------------------
# The whole suite
# --------------------------------------------------------------------------


async def run(
    *,
    admin: asyncpg.Connection,
    runtime: asyncpg.Connection,
    agent: asyncpg.Connection,
    roles: AppRoles,
    graph: Mapping[str, Any],
    model: Mapping[str, Any],
    plan: SeedPlan,
) -> Report:
    """Structural checks once, behavioural checks as runtime and as agent.

    The two behavioural runs are compared check by check. Readme.md section 13:
    they must produce identical results, and that comparison is a counted check
    in its own right rather than an assertion nobody sees.
    """
    checks = await structural(admin, roles, model)

    as_runtime = await behavioural(runtime, roles, graph, model, plan)
    as_agent = await behavioural(agent, roles, graph, model, plan)

    checks += [Check(**{**vars(c), "role": "runtime"}) for c in as_runtime]
    checks += [Check(**{**vars(c), "role": "agent"}) for c in as_agent]

    # Both runs walk the same tables and personas in the same order, so they
    # line up position by position. Matching on names instead would quietly
    # pair a check with the wrong one of its namesakes.
    if len(as_runtime) != len(as_agent):
        raise RuntimeError(
            "the runtime and agent runs did not perform the same checks:"
            f" {len(as_runtime)} vs {len(as_agent)}"
        )
    for mine, theirs in zip(as_runtime, as_agent, strict=True):
        same = (mine.name, mine.table, mine.persona, mine.ok) == theirs.identity
        checks.append(
            _ok("agent_differs", table=mine.table, persona=mine.persona)
            if same
            else _fail(
                "agent_differs",
                table=mine.table,
                persona=mine.persona,
                check=mine.name,
            )
        )

    return Report(checks=checks)
