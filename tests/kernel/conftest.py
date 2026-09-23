"""Fixtures for the enforcement kernel tests. Readme.md section 8 M1.

One app is provisioned per session with the full four-role set, a two-table
schema and a known seeding plan. Expected results come from that plan and never
from reading the policies back (Readme.md section 3 rule 10).
"""

from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import dataclass, field

import asyncpg
import pytest
import pytest_asyncio

from kernel.provision import (
    AppRoles,
    apply_owner_column_policy,
    create_app_roles,
    create_helpers,
    drop_app_roles,
    enable_rls,
    grant_app_roles,
    reassign_objects_to_owner,
)

PG_HOST = os.environ.get("VD_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("VD_PG_PORT", "55432"))
PG_DB = os.environ.get("VD_PG_DB", "vibedeploy")
ADMIN_USER = os.environ.get("VD_PG_ADMIN_USER", "vd_admin")
ADMIN_PASSWORD = os.environ.get("VD_PG_ADMIN_PASSWORD", "vd_local_password")


def dsn(user: str, password: str) -> str:
    return f"postgresql://{user}:{password}@{PG_HOST}:{PG_PORT}/{PG_DB}"


@dataclass
class App:
    """A provisioned app plus the seeding plan that is the tests' answer key."""

    roles: AppRoles
    user_a: uuid.UUID
    user_b: uuid.UUID
    notes_a: list[uuid.UUID] = field(default_factory=list)
    notes_b: list[uuid.UUID] = field(default_factory=list)

    def notes_of(self, who: str) -> list[uuid.UUID]:
        return self.notes_a if who == "a" else self.notes_b

    def user_of(self, who: str) -> uuid.UUID:
        return self.user_a if who == "a" else self.user_b


async def set_identity(
    conn: asyncpg.Connection, user_id: uuid.UUID | None, role: str = ""
) -> None:
    """The only sanctioned way to set identity: transaction-local, bind params.

    Must be called inside an open transaction. Readme.md section 3 rule 4.
    """
    await conn.execute(
        "SELECT set_config('app.user_id', $1, true), set_config('app.role', $2, true)",
        "" if user_id is None else str(user_id),
        role,
    )


@pytest_asyncio.fixture(scope="session")
async def admin():
    conn = await asyncpg.connect(dsn(ADMIN_USER, ADMIN_PASSWORD))
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session")
async def app(admin) -> App:
    """Provision one app end to end, the same order the worker uses.

    Readme.md section 9.1: RLS and FORCE first, then policies, then grants.
    """
    roles = AppRoles.generate(f"app_t{secrets.token_hex(4)}")
    await drop_app_roles(admin, roles)
    await create_app_roles(admin, roles)
    await create_helpers(admin, roles, key_type="uuid")

    built = App(roles=roles, user_a=uuid.uuid4(), user_b=uuid.uuid4())

    # Schema and seed data go in as the migrator, which is how a real app's
    # migrations run.
    mig = await asyncpg.connect(dsn(roles.migrator, roles.migrator_password))
    try:
        await mig.execute(
            """
            CREATE TABLE users (
                id    uuid PRIMARY KEY,
                email text NOT NULL UNIQUE
            );
            CREATE TABLE notes (
                id       uuid PRIMARY KEY,
                owner_id uuid NOT NULL REFERENCES users(id),
                body     text NOT NULL
            );
            CREATE INDEX ON notes (owner_id);
            """
        )
        for who, user_id in (("a", built.user_a), ("b", built.user_b)):
            await mig.execute(
                "INSERT INTO users (id, email) VALUES ($1, $2)",
                user_id,
                f"{who}@example.com",
            )
            for n in range(2):
                note_id = uuid.uuid4()
                await mig.execute(
                    "INSERT INTO notes (id, owner_id, body) VALUES ($1, $2, $3)",
                    note_id,
                    user_id,
                    f"note {n} owned by {who}",
                )
                built.notes_of(who).append(note_id)
    finally:
        await mig.close()

    tables = ["users", "notes"]
    await reassign_objects_to_owner(admin, roles)
    await enable_rls(admin, roles, tables)
    await apply_owner_column_policy(
        admin, roles, table="users", column="id", admin_enabled=True
    )
    await apply_owner_column_policy(
        admin, roles, table="notes", column="owner_id", admin_enabled=True
    )
    await grant_app_roles(admin, roles, tables)

    try:
        yield built
    finally:
        await drop_app_roles(admin, roles)


@pytest_asyncio.fixture(scope="session")
async def runtime_conn(app):
    conn = await asyncpg.connect(
        dsn(app.roles.runtime, app.roles.runtime_password)
    )
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session")
async def agent_conn(app):
    conn = await asyncpg.connect(dsn(app.roles.agent, app.roles.agent_password))
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(params=["runtime", "agent"])
def app_conn(request):
    """Every access test runs twice: once as the app, once as the gateway.

    Readme.md section 13: the agent role must produce identical results to the
    runtime role. That is what proves an agent cannot exceed its person.
    """
    return request.getfixturevalue(f"{request.param}_conn")
