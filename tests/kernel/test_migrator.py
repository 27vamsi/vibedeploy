"""The migrator role. Readme.md section 9.1.

FORCE filters the owner, so a data backfill written by an app's migration would
silently touch zero rows. The migrator carries BYPASSRLS so backfills work. That
is only safe because its credentials never reach the app or the gateway.
"""

from __future__ import annotations

import asyncpg

from kernel.render import quote_ident
from tests.kernel.conftest import dsn


async def test_migrator_has_bypassrls_and_the_others_do_not(app, admin):
    attrs = {
        r["rolname"]: r["rolbypassrls"]
        for r in await admin.fetch(
            "SELECT rolname, rolbypassrls FROM pg_roles WHERE rolname = ANY($1::text[])",
            [
                app.roles.owner,
                app.roles.migrator,
                app.roles.runtime,
                app.roles.agent,
            ],
        )
    }
    assert attrs[app.roles.migrator] is True
    assert attrs[app.roles.owner] is False
    assert attrs[app.roles.runtime] is False
    assert attrs[app.roles.agent] is False


async def test_migrator_can_backfill_every_row_under_force(app):
    """The reason BYPASSRLS exists on this role."""
    conn = await asyncpg.connect(dsn(app.roles.migrator, app.roles.migrator_password))
    try:
        async with conn.transaction():
            status = await conn.execute("UPDATE notes SET body = body")
            touched = int(status.rsplit(" ", 1)[-1])
    finally:
        await conn.close()

    assert touched == len(app.notes_a) + len(app.notes_b)


async def test_migrated_objects_are_owned_by_the_owner_role(app, admin):
    """The migrator runs as the owner, so nothing ends up owned by a login role."""
    owners = await admin.fetch(
        "SELECT c.relname, pg_get_userbyid(c.relowner) AS owner "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relkind = 'r'",
        app.roles.schema,
    )
    assert owners
    for row in owners:
        assert row["owner"] == app.roles.owner


async def test_runtime_and_agent_cannot_reach_the_schema_they_do_not_own(app, admin):
    """They get USAGE, never CREATE: an app cannot add an unprotected table."""
    for name in (app.roles.runtime, app.roles.agent):
        can_create = await admin.fetchval(
            "SELECT has_schema_privilege($1, $2, 'CREATE')", name, app.roles.schema
        )
        assert can_create is False, f"{name} must not be able to create objects"


async def test_public_schema_grants_nothing_to_everyone(app, admin):
    """Nothing should ever land in the public schema by accident."""
    can_create = await admin.fetchval(
        "SELECT has_schema_privilege('public', 'public', 'CREATE')"
    )
    assert can_create is False
