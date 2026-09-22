"""Applies the kernel SQL to a database. Readme.md section 9.

Used by the build job against its throwaway Postgres and by the worker against
RDS, so it lives here rather than in either one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import asyncpg

from kernel.render import (
    generate_password,
    key_type as validate_key_type,
    quote_ident,
    quote_literal,
    render,
    validate_app_id,
)


@dataclass(frozen=True)
class AppRoles:
    app_id: str
    schema: str
    owner: str
    migrator: str
    runtime: str
    migrator_password: str
    runtime_password: str

    @classmethod
    def generate(cls, app_id: str) -> "AppRoles":
        validate_app_id(app_id)
        return cls(
            app_id=app_id,
            schema=app_id,
            owner=f"{app_id}_owner",
            migrator=f"{app_id}_migrator",
            runtime=f"{app_id}_runtime",
            migrator_password=generate_password(),
            runtime_password=generate_password(),
        )


def _idents(roles: AppRoles) -> dict[str, str]:
    return {
        "schema": quote_ident(roles.schema),
        "owner": quote_ident(roles.owner),
        "migrator": quote_ident(roles.migrator),
        "runtime": quote_ident(roles.runtime),
    }


async def create_app_roles(conn: asyncpg.Connection, roles: AppRoles) -> None:
    await conn.execute(
        render(
            "roles.sql",
            **_idents(roles),
            migrator_password=quote_literal(roles.migrator_password),
            runtime_password=quote_literal(roles.runtime_password),
        )
    )


async def create_helpers(
    conn: asyncpg.Connection, roles: AppRoles, key_type: str
) -> None:
    await conn.execute(
        render(
            "helpers.sql",
            schema=quote_ident(roles.schema),
            runtime=quote_ident(roles.runtime),
            key_type=validate_key_type(key_type),
        )
    )


async def apply_owner_column_policy(
    conn: asyncpg.Connection,
    roles: AppRoles,
    *,
    table: str,
    column: str,
    admin_enabled: bool,
) -> None:
    schema = quote_ident(roles.schema)
    admin = f" OR {schema}.vd_role() = 'admin'" if admin_enabled else ""
    await conn.execute(
        render(
            "templates/owner_column.sql",
            schema=schema,
            runtime=quote_ident(roles.runtime),
            table=quote_ident(table),
            column=quote_ident(column),
            admin=admin,
        )
    )


async def enable_rls(
    conn: asyncpg.Connection, roles: AppRoles, tables: Sequence[str]
) -> None:
    schema = quote_ident(roles.schema)
    for table in tables:
        t = f"{schema}.{quote_ident(table)}"
        await conn.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        await conn.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")


async def set_force_rls(
    conn: asyncpg.Connection, roles: AppRoles, tables: Sequence[str], *, force: bool
) -> None:
    """Open and close the migration window (see tests/kernel/test_migrator.py).

    FORCE only governs the table owner. The runtime role is never the owner, so
    it stays filtered for the whole window.
    """
    schema = quote_ident(roles.schema)
    clause = "FORCE" if force else "NO FORCE"
    for table in tables:
        await conn.execute(
            f"ALTER TABLE {schema}.{quote_ident(table)} {clause} ROW LEVEL SECURITY"
        )


async def grant_runtime(
    conn: asyncpg.Connection, roles: AppRoles, tables: Sequence[str]
) -> None:
    """Grants come last, so a table is never readable before it has rules.

    Never TRUNCATE (it ignores RLS), never REFERENCES or TRIGGER. No
    ALTER DEFAULT PRIVILEGES: that would grant on future tables before we have
    written policies for them.
    """
    schema = quote_ident(roles.schema)
    runtime = quote_ident(roles.runtime)
    for table in tables:
        await conn.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {schema}.{quote_ident(table)} "
            f"TO {runtime}"
        )
    await conn.execute(
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {runtime}"
    )


async def drop_app_roles(conn: asyncpg.Connection, roles: AppRoles) -> None:
    """Tear-down for scratch databases and test fixtures."""
    await conn.execute(f"DROP SCHEMA IF EXISTS {quote_ident(roles.schema)} CASCADE")
    for name in (roles.runtime, roles.migrator, roles.owner):
        if await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name):
            await conn.execute(f"DROP OWNED BY {quote_ident(name)} CASCADE")
            await conn.execute(f"DROP ROLE {quote_ident(name)}")
