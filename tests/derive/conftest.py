"""Scratch schemas for the introspection tests. Readme.md section 8 M3.

Each fixture schema gets its own app id, its own Postgres schema and its own
four roles, exactly like a real build job. `flat_todo` is provisioned twice,
under two different app ids, because `schema_hash` is only useful if the same
SQL hashes the same no matter what the scratch schema happened to be called.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import asyncpg
import pytest_asyncio

from buildjob.introspect import introspect
from buildjob.migrate_scratch import apply_sql_migrations
from kernel.provision import AppRoles, create_app_roles, drop_app_roles
from tests.kernel.conftest import ADMIN_PASSWORD, ADMIN_USER, dsn

SCHEMAS = Path(__file__).resolve().parents[2] / "fixtures" / "schemas"

# name used by the tests -> fixture file to apply
SOURCES = {
    "flat_todo": "flat_todo",
    "crm_chain": "crm_chain",
    "messy": "messy",
    "flat_todo_elsewhere": "flat_todo",
}


@pytest_asyncio.fixture(scope="session")
async def graphs() -> dict[str, dict]:
    admin = await asyncpg.connect(dsn(ADMIN_USER, ADMIN_PASSWORD))
    provisioned: list[AppRoles] = []
    built: dict[str, dict] = {}
    try:
        for name, source in SOURCES.items():
            roles = AppRoles.generate(f"app_i{secrets.token_hex(4)}")
            await drop_app_roles(admin, roles)
            await create_app_roles(admin, roles)
            provisioned.append(roles)

            # The app's migrations run as the migrator, and the graph is read
            # before the kernel has enabled RLS or written a single policy.
            mig = await asyncpg.connect(dsn(roles.migrator, roles.migrator_password))
            try:
                await apply_sql_migrations(mig, SCHEMAS / f"{source}.sql")
            finally:
                await mig.close()

            # Deliberately read the graph on the admin connection, which has no
            # app schema on its search_path. The migrator's does, so reading it
            # there would hide a missing `SET LOCAL search_path` in the
            # introspector and let schema-qualified defaults into the hash.
            built[name] = await introspect(admin, roles.schema)
        yield built
    finally:
        for roles in provisioned:
            await drop_app_roles(admin, roles)
        await admin.close()
