"""How migrations get to see all rows while FORCE is on.

Readme.md section 9.1 says the migrator has BYPASSRLS so that data backfills
inside migrations do not silently touch zero rows, and also says
`ALTER ROLE <migrator> SET role = <owner>` so migrated objects are owned by the
owner role. Those two cannot both do their job: RLS bypass is decided by the
*effective* role, so once the migrator acts as the owner it inherits the
owner's NOBYPASSRLS and is filtered again.

Section 9.1 says "Pick one approach and test it in M1". We pick the documented
fallback - drop FORCE for the duration of the migration, put it back after -
because it works the same on RDS, where BYPASSRLS may not be grantable at all.
These tests pin both halves of that reasoning down.
"""

from __future__ import annotations

from .conftest import NOTES_A, NOTES_B, force_rls_disabled, identity


async def test_migrator_is_declared_bypassrls(admin, kernel):
    assert await admin.fetchval(
        "SELECT rolbypassrls FROM pg_roles WHERE rolname = $1", kernel.roles.migrator
    )


async def test_migrator_connects_as_the_owner_role(as_owner, kernel):
    assert await as_owner.fetchval("SELECT current_user") == kernel.roles.owner
    assert await as_owner.fetchval("SELECT session_user") == kernel.roles.migrator


async def test_bypassrls_does_not_survive_set_role_so_force_still_filters(as_owner):
    """The conflict, made visible: BYPASSRLS on the migrator buys nothing here."""
    async with identity(as_owner, None) as c:
        assert await c.fetch("SELECT id FROM notes") == []


async def test_backfill_reaches_every_row_inside_the_no_force_window(
    admin, kernel, as_owner
):
    """The approach we picked: a migration can actually see and update all rows."""
    async with force_rls_disabled(admin, kernel):
        tx = as_owner.transaction()
        await tx.start()
        try:
            status = await as_owner.execute("UPDATE notes SET body = body || '!'")
            assert status == "UPDATE %d" % (len(NOTES_A) + len(NOTES_B))
        finally:
            await tx.rollback()


async def test_force_is_restored_after_the_window(admin, kernel):
    forced = await admin.fetchval(
        "SELECT relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relname = 'notes'",
        kernel.roles.schema,
    )
    assert forced is True
