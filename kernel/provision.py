"""Applies the kernel SQL to a database. Readme.md section 9.

Used by the build job against its throwaway Postgres and by the worker against
RDS, so it lives here rather than in either one.

The order these are called in matters and is fixed: roles, helpers, migrations,
RLS + FORCE, policies, then grants last. A table is never reachable before it
has rules on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import asyncpg

from kernel.render import (
    fk_chain_expression,
    generate_password,
    key_type as validate_key_type,
    quote_ident,
    quote_literal,
    render,
    validate_app_id,
)

# Readme.md section 3 rule 6 and contract 7.3. Nothing else is a policy.
TEMPLATES = (
    "owner_column",
    "fk_chain",
    "shared",
    "read_only_shared",
    "admin_only",
)


@dataclass(frozen=True)
class AppRoles:
    """The four roles an app gets. Readme.md section 3 rule 3.

    `owner` cannot log in. `migrator` is the only one with BYPASSRLS and is
    never handed to the app or the gateway. `runtime` and `agent` are both
    unprivileged and non-owning, which is what makes the policies bind to them.
    """

    app_id: str
    schema: str
    owner: str
    migrator: str
    runtime: str
    agent: str
    migrator_password: str
    runtime_password: str
    agent_password: str

    @classmethod
    def generate(cls, app_id: str) -> "AppRoles":
        validate_app_id(app_id)
        return cls(
            app_id=app_id,
            schema=app_id,
            owner=f"{app_id}_owner",
            migrator=f"{app_id}_migrator",
            runtime=f"{app_id}_runtime",
            agent=f"{app_id}_agent",
            migrator_password=generate_password(),
            runtime_password=generate_password(),
            agent_password=generate_password(),
        )

    @property
    def app_facing(self) -> tuple[str, str]:
        """The roles that policies are written against."""
        return (self.runtime, self.agent)


def _idents(roles: AppRoles) -> dict[str, str]:
    return {
        "schema": quote_ident(roles.schema),
        "owner": quote_ident(roles.owner),
        "migrator": quote_ident(roles.migrator),
        "runtime": quote_ident(roles.runtime),
        "agent": quote_ident(roles.agent),
    }


async def create_app_roles(conn: asyncpg.Connection, roles: AppRoles) -> None:
    await conn.execute(
        render(
            "roles.sql",
            **_idents(roles),
            migrator_password=quote_literal(roles.migrator_password),
            runtime_password=quote_literal(roles.runtime_password),
            agent_password=quote_literal(roles.agent_password),
        )
    )


async def create_helpers(
    conn: asyncpg.Connection, roles: AppRoles, key_type: str
) -> None:
    await conn.execute(
        render(
            "helpers.sql",
            schema=quote_ident(roles.schema),
            owner=quote_ident(roles.owner),
            runtime=quote_ident(roles.runtime),
            agent=quote_ident(roles.agent),
            key_type=validate_key_type(key_type),
        )
    )


def _admin_clause(roles: AppRoles, admin_enabled: bool) -> str:
    """Readme.md section 9.3: `{admin}` is a suffix, or nothing at all.

    Only owner_column and fk_chain take it. `shared` already lets every logged
    in person through, and the other two are defined by `vd_role()` already.
    """
    if not admin_enabled:
        return ""
    return f" OR {quote_ident(roles.schema)}.vd_role() = 'admin'"


async def apply_owner_column_policy(
    conn: asyncpg.Connection,
    roles: AppRoles,
    *,
    table: str,
    column: str,
    admin_enabled: bool,
) -> None:
    await conn.execute(
        render(
            "templates/owner_column.sql",
            schema=quote_ident(roles.schema),
            runtime=quote_ident(roles.runtime),
            agent=quote_ident(roles.agent),
            table=quote_ident(table),
            column=quote_ident(column),
            admin=_admin_clause(roles, admin_enabled),
        )
    )


async def apply_fk_chain_policy(
    conn: asyncpg.Connection,
    roles: AppRoles,
    *,
    table: str,
    path: Sequence[dict],
    admin_enabled: bool,
) -> None:
    await conn.execute(
        render(
            "templates/fk_chain.sql",
            schema=quote_ident(roles.schema),
            runtime=quote_ident(roles.runtime),
            agent=quote_ident(roles.agent),
            table=quote_ident(table),
            expression=fk_chain_expression(
                schema=roles.schema,
                table=table,
                path=path,
                admin=_admin_clause(roles, admin_enabled),
            ),
        )
    )


async def apply_fixed_policy(
    conn: asyncpg.Connection, roles: AppRoles, *, table: str, template: str
) -> None:
    """The three templates that take no arguments beyond the table."""
    if template not in ("shared", "read_only_shared", "admin_only"):
        raise ValueError(f"not a fixed template: {template!r}")
    await conn.execute(
        render(
            f"templates/{template}.sql",
            schema=quote_ident(roles.schema),
            runtime=quote_ident(roles.runtime),
            agent=quote_ident(roles.agent),
            table=quote_ident(table),
        )
    )


async def apply_policy(
    conn: asyncpg.Connection,
    roles: AppRoles,
    *,
    table: str,
    entry: dict,
    admin_enabled: bool,
) -> None:
    """Apply one access model table entry. Contract 7.2 -> contract 7.3."""
    template = entry["template"]
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}; allowed: {TEMPLATES}")
    if template == "owner_column":
        await apply_owner_column_policy(
            conn,
            roles,
            table=table,
            column=entry["column"],
            admin_enabled=admin_enabled,
        )
    elif template == "fk_chain":
        await apply_fk_chain_policy(
            conn,
            roles,
            table=table,
            path=entry["path"],
            admin_enabled=admin_enabled,
        )
    else:
        await apply_fixed_policy(conn, roles, table=table, template=template)


async def reassign_objects_to_owner(
    conn: asyncpg.Connection, roles: AppRoles
) -> None:
    """Hand everything the migrator just created to the owner role.

    Run as platform admin immediately after migrations and before RLS is
    enabled. The migrator keeps BYPASSRLS while it works (so backfills reach
    every row) and ends up owning nothing, which is what stops FORCE from being
    sidestepped by a role that has a password.
    """
    await conn.execute(
        f"REASSIGN OWNED BY {quote_ident(roles.migrator)} "
        f"TO {quote_ident(roles.owner)}"
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
    """Open and close the migration window.

    FORCE only governs the table owner. The runtime and agent roles are never
    owners, so they stay filtered for the whole window.
    """
    schema = quote_ident(roles.schema)
    clause = "FORCE" if force else "NO FORCE"
    for table in tables:
        await conn.execute(
            f"ALTER TABLE {schema}.{quote_ident(table)} {clause} ROW LEVEL SECURITY"
        )


async def grant_app_roles(
    conn: asyncpg.Connection, roles: AppRoles, tables: Sequence[str]
) -> None:
    """Grants come last, so a table is never readable before it has rules.

    Never TRUNCATE (it ignores RLS entirely), never REFERENCES or TRIGGER. No
    ALTER DEFAULT PRIVILEGES: that would grant on future tables before anyone
    has written policies for them.
    """
    schema = quote_ident(roles.schema)
    targets = ", ".join(quote_ident(r) for r in roles.app_facing)
    for table in tables:
        await conn.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {schema}.{quote_ident(table)} "
            f"TO {targets}"
        )
    await conn.execute(
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {targets}"
    )


async def drop_app_roles(conn: asyncpg.Connection, roles: AppRoles) -> None:
    """Tear-down for scratch databases and test fixtures. Idempotent."""
    await conn.execute(f"DROP SCHEMA IF EXISTS {quote_ident(roles.schema)} CASCADE")
    for name in (roles.agent, roles.runtime, roles.migrator, roles.owner):
        if await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name):
            await conn.execute(f"DROP OWNED BY {quote_ident(name)} CASCADE")
            await conn.execute(f"DROP ROLE {quote_ident(name)}")
