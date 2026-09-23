"""A whole build job, run against a fixture schema. Readme.md section 8 M5.

`build()` does exactly what the worker will do and in the same order: create
roles, run the app's migrations as the migrator, hand the objects to the owner,
derive the access model from the graph, apply it, seed, and attack.

The planted-bug tests use the same helper and then break one thing before
attacking, which is the only way to know the suite is not vacuous. Each one
gets its own app id, schema and roles, so a bug planted in one cannot leak into
another.
"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import pytest_asyncio

from buildjob.attack import Report, run as run_attack
from buildjob.derive import Answers, derive
from buildjob.introspect import introspect, schema_hash
from buildjob.migrate_scratch import apply_sql_migrations
from buildjob.policies import apply_model
from buildjob.seed import SeedPlan, seed
from kernel.provision import (
    AppRoles,
    create_app_roles,
    create_helpers,
    drop_app_roles,
    reassign_objects_to_owner,
)
from tests.kernel.conftest import ADMIN_PASSWORD, ADMIN_USER, dsn

SCHEMAS = Path(__file__).resolve().parents[2] / "fixtures" / "schemas"

ANSWERS = {
    "flat_todo": Answers(
        audience="me", size="small", visibility="own_data", sensitive=False
    ),
    "crm_chain": Answers(
        audience="team",
        size="small",
        visibility="own_data",
        sensitive=True,
        follow_ups={
            "unlinked.plans": "read_only_shared",
            "unlinked.audit_log": "admin_only",
        },
    ),
}


@dataclass
class Built:
    """One provisioned, seeded, policy-covered app, ready to be attacked."""

    roles: AppRoles
    graph: dict[str, Any]
    model: dict[str, Any]
    plan: SeedPlan
    admin: asyncpg.Connection
    runtime: asyncpg.Connection
    agent: asyncpg.Connection

    async def attack(self) -> Report:
        return await run_attack(
            admin=self.admin,
            runtime=self.runtime,
            agent=self.agent,
            roles=self.roles,
            graph=self.graph,
            model=self.model,
            plan=self.plan,
        )

    async def as_migrator(self, sql: str) -> None:
        """Run something as the migrator, for planting a bug in app code."""
        conn = await asyncpg.connect(
            dsn(self.roles.migrator, self.roles.migrator_password)
        )
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    def qualified(self, table: str) -> str:
        return f'"{self.roles.schema}"."{table}"'


@asynccontextmanager
async def build(admin: asyncpg.Connection, fixture: str, *, apply: bool = True):
    """The worker's order, start to finish, on a throwaway schema."""
    roles = AppRoles.generate(f"app_a{secrets.token_hex(4)}")
    await drop_app_roles(admin, roles)
    await create_app_roles(admin, roles)

    runtime = agent = None
    try:
        mig = await asyncpg.connect(dsn(roles.migrator, roles.migrator_password))
        try:
            await apply_sql_migrations(mig, SCHEMAS / f"{fixture}.sql")
        finally:
            await mig.close()

        graph = await introspect(admin, roles.schema)
        result = derive(
            graph,
            ANSWERS[fixture],
            app_id=roles.app_id,
            schema_hash=schema_hash(graph),
        )
        assert result.model is not None, result.refusals
        model = result.model

        await create_helpers(admin, roles, key_type=model["principal"]["key_type"])
        await reassign_objects_to_owner(admin, roles)

        # Seeding happens as the migrator, before any policy exists, exactly
        # as section 12 says. The migrator keeps BYPASSRLS but owns nothing.
        mig = await asyncpg.connect(dsn(roles.migrator, roles.migrator_password))
        try:
            plan = await seed(mig, roles, graph, model)
        finally:
            await mig.close()

        if apply:
            await apply_model(admin, roles, model)

        runtime = await asyncpg.connect(dsn(roles.runtime, roles.runtime_password))
        agent = await asyncpg.connect(dsn(roles.agent, roles.agent_password))
        yield Built(
            roles=roles,
            graph=graph,
            model=model,
            plan=plan,
            admin=admin,
            runtime=runtime,
            agent=agent,
        )
    finally:
        for conn in (runtime, agent):
            if conn is not None:
                await conn.close()
        await drop_app_roles(admin, roles)


@pytest_asyncio.fixture(scope="session")
async def attack_admin():
    conn = await asyncpg.connect(dsn(ADMIN_USER, ADMIN_PASSWORD))
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session", params=["flat_todo", "crm_chain"])
async def clean(request, attack_admin):
    """A correctly built app. Nothing about it should be attackable."""
    async with build(attack_admin, request.param) as built:
        yield built
