"""The M1 acceptance criteria, Readme.md section 8.

Every access test runs as the runtime role and again as the agent role via the
`app_conn` fixture. Identical results are the proof that putting an agent on an
app does not widen anything.

Expected rows come from the seeding plan in conftest, never from reading the
policies back.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from tests.kernel.conftest import dsn, set_identity


def affected(status: str) -> int:
    """asyncpg returns a command tag like 'UPDATE 3'."""
    return int(status.rsplit(" ", 1)[-1])


@pytest.mark.parametrize("who", ["a", "b"])
async def test_sees_only_own_rows(app, app_conn, who):
    async with app_conn.transaction():
        await set_identity(app_conn, app.user_of(who))
        rows = await app_conn.fetch("SELECT id FROM notes")

    assert {r["id"] for r in rows} == set(app.notes_of(who))


async def test_no_identity_sees_zero_rows(app, app_conn):
    """The headline claim: no identity is zero rows, not everything."""
    async with app_conn.transaction():
        await set_identity(app_conn, None)
        notes = await app_conn.fetch("SELECT id FROM notes")
        users = await app_conn.fetch("SELECT id FROM users")

    assert notes == []
    assert users == []


async def test_unfiltered_select_still_scopes_to_one_user(app, app_conn):
    """`SELECT * FROM notes` with no WHERE, the sloppy app's query."""
    async with app_conn.transaction():
        await set_identity(app_conn, app.user_a)
        rows = await app_conn.fetch("SELECT * FROM notes")

    assert len(rows) == len(app.notes_a)
    assert all(r["owner_id"] == app.user_a for r in rows)


async def test_identity_does_not_leak_to_the_next_transaction(app, app_conn):
    """Transaction-local settings plus a pooled connection.

    After a transaction that set a user ends, the next transaction on the same
    connection must see nothing. current_setting returns '' rather than NULL
    here, which is why the helper wraps it in NULLIF.
    """
    async with app_conn.transaction():
        await set_identity(app_conn, app.user_a)
        assert len(await app_conn.fetch("SELECT id FROM notes")) == len(app.notes_a)

    async with app_conn.transaction():
        leaked = await app_conn.fetchval("SELECT current_setting('app.user_id', true)")
        rows = await app_conn.fetch("SELECT id FROM notes")

    assert leaked in ("", None)
    assert rows == []


async def test_cannot_insert_a_row_owned_by_someone_else(app, app_conn):
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with app_conn.transaction():
            await set_identity(app_conn, app.user_a)
            await app_conn.execute(
                "INSERT INTO notes (id, owner_id, body) VALUES ($1, $2, $3)",
                uuid.uuid4(),
                app.user_b,
                "planted by a, owned by b",
            )


async def test_cannot_reassign_own_row_to_someone_else(app, app_conn):
    """WITH CHECK on UPDATE. Without it, A could hand a row to B."""
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with app_conn.transaction():
            await set_identity(app_conn, app.user_a)
            await app_conn.execute(
                "UPDATE notes SET owner_id = $1 WHERE id = $2",
                app.user_b,
                app.notes_a[0],
            )


async def test_update_of_another_users_rows_affects_nothing(app, app_conn):
    async with app_conn.transaction():
        await set_identity(app_conn, app.user_a)
        status = await app_conn.execute(
            "UPDATE notes SET body = 'owned' WHERE id = $1", app.notes_b[0]
        )

    assert affected(status) == 0


async def test_delete_of_another_users_rows_affects_nothing(app, app_conn):
    async with app_conn.transaction():
        await set_identity(app_conn, app.user_a)
        status = await app_conn.execute(
            "DELETE FROM notes WHERE id = $1", app.notes_b[0]
        )

    assert affected(status) == 0


async def test_truncate_is_denied(app, app_conn):
    """TRUNCATE ignores RLS entirely, so the privilege is never granted."""
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with app_conn.transaction():
            await set_identity(app_conn, app.user_a)
            await app_conn.execute("TRUNCATE notes")


async def test_admin_claim_sees_every_row(app, app_conn):
    async with app_conn.transaction():
        await set_identity(app_conn, app.user_a, role="admin")
        rows = await app_conn.fetch("SELECT id FROM notes")

    assert {r["id"] for r in rows} == set(app.notes_a) | set(app.notes_b)


async def test_unknown_role_claim_grants_nothing_extra(app, app_conn):
    """Only the exact string 'admin' widens anything."""
    async with app_conn.transaction():
        await set_identity(app_conn, app.user_a, role="administrator")
        rows = await app_conn.fetch("SELECT id FROM notes")

    assert {r["id"] for r in rows} == set(app.notes_a)


@pytest.mark.parametrize("role_attr", ["runtime", "agent"])
async def test_app_facing_roles_hold_no_privileges(app, admin, role_attr):
    """Runtime and agent must both be ordinary, unprivileged login roles."""
    name = getattr(app.roles, role_attr)
    row = await admin.fetchrow(
        "SELECT rolsuper, rolbypassrls, rolinherit, rolcreaterole, rolcreatedb "
        "FROM pg_roles WHERE rolname = $1",
        name,
    )
    assert row["rolsuper"] is False
    assert row["rolbypassrls"] is False
    assert row["rolinherit"] is False
    assert row["rolcreaterole"] is False
    assert row["rolcreatedb"] is False

    is_member = await admin.fetchval(
        "SELECT pg_has_role($1, $2, 'MEMBER')", name, app.roles.owner
    )
    assert is_member is False

    owns = await admin.fetchval(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND pg_get_userbyid(c.relowner) = $2",
        app.roles.schema,
        name,
    )
    assert owns == 0


async def test_agent_role_carries_a_statement_timeout(app, admin):
    """An agent is driven by a model and will ask for something enormous."""
    settings = await admin.fetchval(
        "SELECT rolconfig FROM pg_roles WHERE rolname = $1", app.roles.agent
    )
    assert "statement_timeout=5s" in settings

    conn = await asyncpg.connect(dsn(app.roles.agent, app.roles.agent_password))
    try:
        assert await conn.fetchval("SHOW statement_timeout") == "5s"
    finally:
        await conn.close()


@pytest.mark.parametrize("role_attr", ["runtime", "agent"])
async def test_dangerous_privileges_are_never_granted(app, admin, role_attr):
    name = getattr(app.roles, role_attr)
    for priv in ("TRUNCATE", "REFERENCES", "TRIGGER"):
        held = await admin.fetchval(
            "SELECT has_table_privilege($1, $2, $3)",
            name,
            f"{app.roles.schema}.notes",
            priv,
        )
        assert held is False, f"{name} must not hold {priv}"


async def test_no_policy_uses_a_literal_true(app, admin):
    """`USING (true)` is the bug the whole product exists to catch."""
    exprs = await admin.fetch(
        "SELECT polname, pg_get_expr(polqual, polrelid) AS using_expr, "
        "       pg_get_expr(polwithcheck, polrelid) AS check_expr "
        "FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = $1",
        app.roles.schema,
    )
    assert exprs, "expected policies to exist"
    for row in exprs:
        for expr in (row["using_expr"], row["check_expr"]):
            assert expr != "true", f"{row['polname']} uses a literal true"


async def test_every_table_has_rls_enabled_and_forced(app, admin):
    rows = await admin.fetch(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relkind = 'r'",
        app.roles.schema,
    )
    assert {r["relname"] for r in rows} == {"users", "notes"}
    for row in rows:
        assert row["relrowsecurity"] is True, f"{row['relname']} has RLS off"
        assert row["relforcerowsecurity"] is True, f"{row['relname']} is not FORCEd"


@pytest.mark.parametrize("table", ["users", "notes"])
async def test_every_table_has_a_policy_for_all_four_commands(app, admin, table):
    cmds = await admin.fetch(
        "SELECT p.polcmd FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relname = $2",
        app.roles.schema,
        table,
    )
    # polcmd is Postgres' internal "char": r = SELECT, a = INSERT,
    # w = UPDATE, d = DELETE.
    assert {c["polcmd"].decode() for c in cmds} == {"r", "a", "w", "d"}
