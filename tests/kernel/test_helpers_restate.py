"""The identity helpers, stated twice. Readme.md section 9.2.

Every policy in the product calls `vd_user_id()` or `vd_role()`, and the worker
used to create them only on the deploy that created the schema. That is a
different question from the one it needs answered. An app whose first deploy
provisioned the schema and then blocked — in the migration step, which is the
likely place — came back on its next deploy with `fresh=False`, skipped these,
and failed writing a policy that called a function nobody had created. Not once:
permanently, because every later deploy took the same branch.

So `enforce` states them every time, and that is only allowed to be a safe thing
to do if running this SQL over a schema that already has it is not an error.
This file is that one property.
"""

from __future__ import annotations

import asyncpg
import pytest

from kernel.provision import create_helpers
from kernel.render import quote_ident
from tests.kernel.conftest import dsn


async def test_the_helpers_can_be_created_over_themselves(app, admin):
    """What `CREATE FUNCTION` without `OR REPLACE` made impossible."""
    await create_helpers(admin, app.roles, key_type="uuid")
    await create_helpers(admin, app.roles, key_type="uuid")


async def test_restating_them_does_not_drop_the_grants(app, admin):
    """`CREATE OR REPLACE` keeps the existing privileges, so a re-run that also
    re-granted could hide a lost grant. The app has to still be able to call
    them, which is the only thing the grant is for."""
    await create_helpers(admin, app.roles, key_type="uuid")

    schema = quote_ident(app.roles.schema)
    runtime = await asyncpg.connect(dsn(app.roles.runtime, app.roles.runtime_password))
    try:
        assert await runtime.fetchval(f"SELECT {schema}.vd_role()") is None
    finally:
        await runtime.close()


async def test_restating_them_does_not_hand_them_to_everyone(app, admin):
    """The `REVOKE ... FROM PUBLIC` has to survive the replace too, or a
    re-stated helper would be callable by any role on the instance."""
    await create_helpers(admin, app.roles, key_type="uuid")

    acl = await admin.fetchval(
        """
        SELECT array_to_string(proacl, ',')
        FROM pg_proc
        JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
        WHERE nspname = $1 AND proname = 'vd_user_id'
        """,
        app.roles.schema,
    )
    # A grant to PUBLIC is spelled with an empty grantee, i.e. a leading `=`.
    assert acl is not None
    assert not any(entry.startswith("=") for entry in acl.split(","))


async def test_a_changed_key_type_is_still_refused(app, admin):
    """The one thing `OR REPLACE` must not quietly allow.

    `enforce` checks the schema hash before it gets here, so this should be
    unreachable — but the reason it is safe to state these unconditionally is
    that Postgres itself will not let the return type move, and that is worth
    asserting rather than assuming.
    """
    with pytest.raises(asyncpg.exceptions.InvalidFunctionDefinitionError):
        await create_helpers(admin, app.roles, key_type="bigint")
