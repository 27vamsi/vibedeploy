"""M1 acceptance: the database, not the app, decides who sees what.

Every test here maps to a bullet in Readme.md section 8, M1 "Done when".
The query the app runs is always the unfiltered one - that is the point.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from .conftest import NOTES_A, NOTES_B, USER_A, USER_B, force_rls_disabled, identity

UNFILTERED = "SELECT id FROM notes"


async def ids(conn, sql=UNFILTERED, *args) -> set[uuid.UUID]:
    return {r["id"] for r in await conn.fetch(sql, *args)}


# --- reading -----------------------------------------------------------------


async def test_runtime_with_identity_sees_only_its_own_rows(runtime):
    async with identity(runtime, USER_A) as c:
        assert await ids(c) == set(NOTES_A)
    async with identity(runtime, USER_B) as c:
        assert await ids(c) == set(NOTES_B)


async def test_runtime_without_identity_sees_nothing(runtime):
    async with identity(runtime, None) as c:
        assert await ids(c) == set()


async def test_runtime_cannot_read_another_users_row_by_primary_key(runtime):
    async with identity(runtime, USER_A) as c:
        rows = await c.fetch("SELECT id FROM notes WHERE id = $1", NOTES_B[0])
        assert rows == []


# --- writing -----------------------------------------------------------------


async def test_runtime_cannot_insert_a_row_owned_by_someone_else(runtime):
    async with identity(runtime, USER_A) as c:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await c.execute(
                "INSERT INTO notes (id, owner_id, body) VALUES ($1, $2, 'x')",
                uuid.uuid4(),
                USER_B,
            )


async def test_runtime_cannot_reassign_its_own_row_to_someone_else(runtime):
    async with identity(runtime, USER_A) as c:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await c.execute(
                "UPDATE notes SET owner_id = $1 WHERE id = $2", USER_B, NOTES_A[0]
            )


async def test_runtime_update_of_another_users_row_affects_zero_rows(runtime):
    async with identity(runtime, USER_A) as c:
        status = await c.execute(
            "UPDATE notes SET body = 'hacked' WHERE id = $1", NOTES_B[0]
        )
        assert status == "UPDATE 0"


async def test_runtime_delete_of_another_users_row_affects_zero_rows(runtime):
    async with identity(runtime, USER_A) as c:
        status = await c.execute("DELETE FROM notes WHERE id = $1", NOTES_B[0])
        assert status == "DELETE 0"


async def test_runtime_cannot_truncate(runtime):
    async with identity(runtime, USER_A) as c:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await c.execute("TRUNCATE notes")


# --- proving each control is load-bearing ------------------------------------


async def test_owner_role_is_filtered_while_force_is_on(as_owner):
    """FORCE is what stops the table owner from seeing everything."""
    async with identity(as_owner, None) as c:
        assert (await c.fetchval("SELECT current_user")).endswith("_owner")
        assert await ids(c) == set()


async def test_owner_role_sees_everything_once_force_is_off(admin, kernel, as_owner):
    """The same query, FORCE removed: all four rows. Proves the test can fail."""
    async with force_rls_disabled(admin, kernel):
        async with identity(as_owner, None) as c:
            assert await ids(c) == set(NOTES_A) | set(NOTES_B)


async def test_bypassrls_role_sees_everything(bypass):
    """Proves NOBYPASSRLS on the runtime role is doing real work."""
    async with identity(bypass, USER_A) as c:
        assert await ids(c) == set(NOTES_A) | set(NOTES_B)


async def test_runtime_role_has_no_dangerous_attributes(admin, kernel):
    row = await admin.fetchrow(
        "SELECT rolbypassrls, rolsuper, rolcreaterole, rolcreatedb, rolinherit "
        "FROM pg_roles WHERE rolname = $1",
        kernel.roles.runtime,
    )
    assert row["rolbypassrls"] is False
    assert row["rolsuper"] is False
    assert row["rolcreaterole"] is False
    assert row["rolcreatedb"] is False
    assert row["rolinherit"] is False

    is_member = await admin.fetchval(
        "SELECT pg_has_role($1, $2, 'member')", kernel.roles.runtime, kernel.roles.owner
    )
    assert is_member is False

    owns = await admin.fetchval(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND pg_get_userbyid(c.relowner) = $2",
        kernel.roles.schema,
        kernel.roles.runtime,
    )
    assert owns == 0


async def test_every_table_has_rls_enabled_and_forced(admin, kernel):
    row = await admin.fetchrow(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relname = 'notes'",
        kernel.roles.schema,
    )
    assert row["relrowsecurity"] is True
    assert row["relforcerowsecurity"] is True


async def test_policies_cover_all_four_commands_and_none_is_literal_true(admin, kernel):
    rows = await admin.fetch(
        "SELECT cmd, qual, with_check FROM pg_policies "
        "WHERE schemaname = $1 AND tablename = 'notes'",
        kernel.roles.schema,
    )
    assert {r["cmd"] for r in rows} == {"SELECT", "INSERT", "UPDATE", "DELETE"}
    for r in rows:
        for expr in (r["qual"], r["with_check"]):
            assert expr != "true", f"{r['cmd']} policy lets everything through"


# --- the pooling gotcha ------------------------------------------------------


async def test_identity_does_not_survive_its_transaction(runtime):
    """The empty-string gotcha (section 9.2).

    After a transaction-local setting ends, current_setting returns '' rather
    than NULL. NULLIF turns it back into NULL so the next transaction on the
    same pooled connection sees nothing instead of erroring or leaking.
    """
    async with identity(runtime, USER_A) as c:
        assert await ids(c) == set(NOTES_A)

    tx = runtime.transaction()
    await tx.start()
    try:
        leftover = await runtime.fetchval("SELECT current_setting('app.user_id', true)")
        assert leftover in ("", None)
        assert await ids(runtime) == set()
    finally:
        await tx.rollback()


async def test_vd_user_id_returns_null_rather_than_erroring_on_empty_string(runtime):
    async with identity(runtime, None) as c:
        assert await c.fetchval("SELECT vd_user_id()") is None
        assert await c.fetchval("SELECT vd_role()") is None
