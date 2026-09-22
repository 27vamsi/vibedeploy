"""Fixtures for the enforcement kernel proofs (Readme.md M1).

Everything here talks to the local Postgres from infra/local/docker-compose.yml.
Start it with:  docker compose -f infra/local/docker-compose.yml up -d postgres
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg
import pytest_asyncio

from kernel.provision import (
    AppRoles,
    apply_owner_column_policy,
    create_app_roles,
    create_helpers,
    drop_app_roles,
    enable_rls,
    grant_runtime,
    set_force_rls,
)

ADMIN_DSN = os.environ.get(
    "VD_TEST_DSN",
    "postgresql://vd_admin:vd_local_password@127.0.0.1:55432/vibedeploy",
)

TEST_APP_ID = "app_kerneltest"

# Ground truth. Section 12 rule 5: the answer key comes from the seeding plan,
# never from reading the policies back.
USER_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
USER_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")

NOTES_A = [uuid.UUID("11111111-0000-4000-8000-00000000000%d" % i) for i in (1, 2)]
NOTES_B = [uuid.UUID("22222222-0000-4000-8000-00000000000%d" % i) for i in (1, 2)]


def dsn_for(user: str, password: str) -> str:
    host_and_db = ADMIN_DSN.split("@", 1)[1]
    return f"postgresql://{user}:{password}@{host_and_db}"


async def drop_probe_role(admin: asyncpg.Connection, name: str) -> None:
    """DROP ROLE refuses while grants on the app's tables still reference it."""
    if await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name):
        await admin.execute(f'DROP OWNED BY "{name}" CASCADE')
        await admin.execute(f'DROP ROLE "{name}"')


@dataclass
class KernelFixture:
    roles: AppRoles
    bypass_user: str
    bypass_password: str

    @property
    def runtime_dsn(self) -> str:
        return dsn_for(self.roles.runtime, self.roles.runtime_password)

    @property
    def migrator_dsn(self) -> str:
        return dsn_for(self.roles.migrator, self.roles.migrator_password)

    @property
    def bypass_dsn(self) -> str:
        return dsn_for(self.bypass_user, self.bypass_password)


@asynccontextmanager
async def identity(conn: asyncpg.Connection, user_id, role: str = ""):
    """What the shim does: transaction-local identity, bind parameters only.

    Rolled back at the end so checks never leave state behind (section 13).
    """
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(
            "SELECT set_config('app.user_id', $1, true), set_config('app.role', $2, true)",
            "" if user_id is None else str(user_id),
            role,
        )
        yield conn
    finally:
        await tx.rollback()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def admin():
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def kernel(admin: asyncpg.Connection) -> KernelFixture:
    roles = AppRoles.generate(TEST_APP_ID)
    bypass_user = f"{TEST_APP_ID}_bypass_probe"
    bypass_password = "probe_password"

    await drop_probe_role(admin, bypass_user)
    await drop_app_roles(admin, roles)

    await create_app_roles(admin, roles)
    await create_helpers(admin, roles, key_type="uuid")

    # Migrations run as the migrator, which acts as the owner role.
    migrator = await asyncpg.connect(dsn_for(roles.migrator, roles.migrator_password))
    try:
        await migrator.execute(
            """
            CREATE TABLE notes (
                id       uuid PRIMARY KEY,
                owner_id uuid NOT NULL,
                body     text NOT NULL
            );
            """
        )
        rows = [(n, USER_A, "a note") for n in NOTES_A] + [
            (n, USER_B, "b note") for n in NOTES_B
        ]
        await migrator.executemany(
            "INSERT INTO notes (id, owner_id, body) VALUES ($1, $2, $3)", rows
        )
    finally:
        await migrator.close()

    # Section 9.1 order: rules first, grants last.
    await enable_rls(admin, roles, ["notes"])
    await apply_owner_column_policy(
        admin, roles, table="notes", column="owner_id", admin_enabled=False
    )
    await grant_runtime(admin, roles, ["notes"])

    # A deliberately over-privileged role, used only to prove that
    # NOBYPASSRLS on the runtime role is what does the work.
    await admin.execute(
        f"""CREATE ROLE "{bypass_user}" LOGIN PASSWORD '{bypass_password}' BYPASSRLS"""
    )
    await admin.execute(f'GRANT USAGE ON SCHEMA "{roles.schema}" TO "{bypass_user}"')
    await admin.execute(
        f'GRANT SELECT ON "{roles.schema}"."notes" TO "{bypass_user}"'
    )
    await admin.execute(
        f'ALTER ROLE "{bypass_user}" SET search_path = "{roles.schema}"'
    )

    fixture = KernelFixture(roles, bypass_user, bypass_password)
    try:
        yield fixture
    finally:
        await drop_probe_role(admin, bypass_user)
        await drop_app_roles(admin, roles)


@pytest_asyncio.fixture(loop_scope="session")
async def runtime(kernel: KernelFixture):
    conn = await asyncpg.connect(kernel.runtime_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture(loop_scope="session")
async def as_owner(kernel: KernelFixture):
    """Connects as the migrator, which ALTER ROLE ... SET role makes the owner."""
    conn = await asyncpg.connect(kernel.migrator_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture(loop_scope="session")
async def bypass(kernel: KernelFixture):
    conn = await asyncpg.connect(kernel.bypass_dsn)
    try:
        yield conn
    finally:
        await conn.close()


@asynccontextmanager
async def force_rls_disabled(admin: asyncpg.Connection, kernel: KernelFixture):
    await set_force_rls(admin, kernel.roles, ["notes"], force=False)
    try:
        yield
    finally:
        await set_force_rls(admin, kernel.roles, ["notes"], force=True)
