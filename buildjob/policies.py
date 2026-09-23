"""Turns an access model into policies on a live database. Readme.md 9.1.

The order is fixed and is the whole safety argument:

    1. RLS on, FORCE on, for every table in the model.
    2. Policies, dropping any previous `vd_` ones first.
    3. Indexes on the columns the chains walk.
    4. Grants, last.

A table is therefore never reachable by the app before it has rules on it, and
a table the model does not mention never gets a grant at all. Getting this
backwards even briefly would open a window where an unprotected table is
readable, which is exactly what "grants go last" exists to prevent.
"""

from __future__ import annotations

from typing import Any, Mapping

import asyncpg

from kernel.provision import AppRoles, apply_policy, enable_rls, grant_app_roles
from kernel.render import quote_ident


def path_columns(model: Mapping[str, Any]) -> dict[str, set[str]]:
    """Every column a policy has to filter or join on, per table.

    Readme.md section 9.3: "Index every path column." Without these, an EXISTS
    chain runs a sequential scan per row and the agent role's 5 second
    statement timeout starts firing on perfectly correct policies.
    """
    wanted: dict[str, set[str]] = {}
    for table, entry in model["tables"].items():
        if entry["template"] == "owner_column":
            wanted.setdefault(table, set()).add(entry["column"])
        elif entry["template"] == "fk_chain":
            for hop in entry["path"]:
                columns = (
                    [hop["column"]] if "column" in hop else list(hop["columns"])
                )
                wanted.setdefault(hop["from"], set()).update(columns)
    return wanted


async def create_path_indexes(
    conn: asyncpg.Connection, roles: AppRoles, model: Mapping[str, Any]
) -> None:
    schema = quote_ident(roles.schema)
    for table, columns in sorted(path_columns(model).items()):
        for column in sorted(columns):
            name = f"vd_idx_{table}_{column}"[:63]
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS {quote_ident(name)} "
                f"ON {schema}.{quote_ident(table)} ({quote_ident(column)})"
            )


async def apply_model(
    conn: asyncpg.Connection, roles: AppRoles, model: Mapping[str, Any]
) -> list[str]:
    """Apply a complete access model. Returns the tables it covered."""
    tables = sorted(model["tables"])
    await enable_rls(conn, roles, tables)
    for table in tables:
        await apply_policy(
            conn,
            roles,
            table=table,
            entry=model["tables"][table],
            admin_enabled=model["admin_enabled"],
        )
    await create_path_indexes(conn, roles, model)
    await grant_app_roles(conn, roles, tables)
    return tables
