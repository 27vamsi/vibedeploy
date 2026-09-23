"""Proof that the two settings the kernel depends on are load-bearing.

These deliberately misconfigure a copy of the setup and show the data leaking.
If one of these ever stops failing-when-broken, the protection is decorative.
Never delete or skip them to make CI green.
"""

from __future__ import annotations

import asyncpg
import pytest

from kernel.provision import set_force_rls
from kernel.render import quote_ident
from tests.kernel.conftest import (
    ADMIN_PASSWORD,
    ADMIN_USER,
    dsn,
    set_identity,
)


@pytest.mark.negative
async def test_force_rls_is_what_filters_the_table_owner(app, admin):
    """With FORCE the owner is filtered too; without it the owner sees all.

    The owner is NOLOGIN precisely so no credential for it exists, but FORCE is
    the belt to that braces.
    """
    schema = quote_ident(app.roles.schema)
    owner = quote_ident(app.roles.owner)
    everything = set(app.notes_a) | set(app.notes_b)

    conn = await asyncpg.connect(dsn(ADMIN_USER, ADMIN_PASSWORD))
    try:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL ROLE {owner}")
            await set_identity(conn, None)
            rows = await conn.fetch(f"SELECT id FROM {schema}.notes")
        assert rows == [], "FORCE is on, so even the owner must be filtered"

        await set_force_rls(admin, app.roles, ["notes"], force=False)
        try:
            async with conn.transaction():
                await conn.execute(f"SET LOCAL ROLE {owner}")
                await set_identity(conn, None)
                leaked = await conn.fetch(f"SELECT id FROM {schema}.notes")
            assert {r["id"] for r in leaked} == everything, (
                "with NO FORCE the owner should see everything; if this fails "
                "the test is no longer proving what FORCE does"
            )
        finally:
            await set_force_rls(admin, app.roles, ["notes"], force=True)
    finally:
        await conn.close()


@pytest.mark.negative
async def test_bypassrls_would_defeat_every_policy(app, admin):
    """Why runtime and agent are NOBYPASSRLS: the attribute overrides policies."""
    name = f"{app.roles.app_id}_throwaway"
    schema = quote_ident(app.roles.schema)
    everything = set(app.notes_a) | set(app.notes_b)

    await admin.execute(
        f"CREATE ROLE {quote_ident(name)} LOGIN PASSWORD 'throwaway' BYPASSRLS "
        f"NOSUPERUSER NOCREATEDB NOCREATEROLE"
    )
    try:
        await admin.execute(f"GRANT USAGE ON SCHEMA {schema} TO {quote_ident(name)}")
        await admin.execute(
            f"GRANT SELECT ON {schema}.notes TO {quote_ident(name)}"
        )

        conn = await asyncpg.connect(dsn(name, "throwaway"))
        try:
            async with conn.transaction():
                await set_identity(conn, None)
                leaked = await conn.fetch(f"SELECT id FROM {schema}.notes")
        finally:
            await conn.close()

        assert {r["id"] for r in leaked} == everything, (
            "a BYPASSRLS role should see every row with no identity set; if "
            "this fails the test is no longer proving what NOBYPASSRLS buys"
        )
    finally:
        await admin.execute(f"DROP OWNED BY {quote_ident(name)} CASCADE")
        await admin.execute(f"DROP ROLE {quote_ident(name)}")
