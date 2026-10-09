"""A real app, and an agent pointed at one of its people.

The app is built by the same helper the attack suite uses, so the schema, the
policies and the seed plan here are the ones a deployed app would have. That
matters more than convenience: the claim under test is that an agent gets
exactly the access of the person it acts for, and the only honest way to test it
is against the policies a real deploy would write.

Expectations come from the seed plan (Readme.md section 3 rule 10). Nothing here
reads a policy back to decide what should have happened.
"""

from __future__ import annotations

import secrets as _secrets
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import text

from gateway.policy import Limits
from tests.attack.conftest import build
from tests.kernel.conftest import ADMIN_PASSWORD, ADMIN_USER, dsn


@pytest_asyncio.fixture(scope="session")
async def gateway_admin():
    import asyncpg

    conn = await asyncpg.connect(dsn(ADMIN_USER, ADMIN_PASSWORD))
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session")
async def crm(gateway_admin):
    """The demo shape: invoices -> orders -> customers -> users, plus a
    reference table and an audit table nobody owns."""
    async with build(gateway_admin, "crm_chain") as built:
        yield built


@pytest.fixture
def catalog(crm):
    from gateway.connectors.postgres import SchemaCatalog

    return SchemaCatalog.from_graph(crm.roles.schema, crm.graph)


@pytest.fixture
def connector(catalog):
    from gateway.connectors.postgres import PostgresConnector

    return PostgresConnector(catalog=catalog, limits=Limits())


def creds_for(built, persona: str, *, role: str = "member"):
    """Connect as the app's `agent` role, as `persona`.

    The agent role is NOBYPASSRLS, NOINHERIT and owns nothing, and the identity
    is set per transaction. There is no way for anything below to widen either.
    """
    from gateway.connectors.postgres import Creds

    return Creds(
        dsn=dsn(built.roles.agent, built.roles.agent_password),
        schema=built.roles.schema,
        user_id=str(built.plan.user_ids[persona]),
        role=role,
    )


@pytest.fixture
def alice(crm):
    """The person the agent acts for."""
    return creds_for(crm, "A")


# ---------------------------------------------------------------------------
# A whole gateway: control plane, app, person, agent
# ---------------------------------------------------------------------------
#
# Lives here rather than in one test module because more than one module needs
# it with a different policy, and because the only honest way to test the
# pipeline is against the rows a finished deploy would really have left behind.


class Wired:
    def __init__(self, *, app, gateway, sessions, agent_id, app_uuid, key, alice, admin):
        self.app = app
        self.gateway = gateway
        self.sessions = sessions
        self.agent_id = agent_id
        self.app_uuid = app_uuid
        self.key = key
        self.alice = alice
        self.admin = admin

    async def session_id(self):
        caller = await self.gateway.open_session(self.key)
        return caller.session_id

    async def name_of(self, row_id):
        return await self.app.admin.fetchval(
            f'SELECT name FROM "{self.app.roles.schema}".customers WHERE id = $1',
            row_id,
        )


@asynccontextmanager
async def wire(gateway_admin, root, *, control_url, policy, fixture="crm_chain"):
    """One app, one person, one agent, and a gateway pointed at all three."""
    from control_plane import migrate
    from control_plane.config import DEFAULT_APP_DSN, ControlPlaneConfig
    from control_plane.db import make_engine, make_sessionmaker
    from control_plane.secrets import AGENT, SecretStore, secret_name
    from gateway.pipeline import Gateway

    config = ControlPlaneConfig(
        database_url=control_url,
        app_admin_dsn=DEFAULT_APP_DSN,
        sidecar_api_key=_secrets.token_hex(16),
        state_root=root / "state",
        public_url="http://127.0.0.1:0",
    )
    await migrate.prepare(config)
    engine = make_engine(config)
    sessions = make_sessionmaker(engine)
    await empty(engine)

    async with build(gateway_admin, fixture) as app:
        store = SecretStore(config.state_root / "secrets")
        store.put(
            secret_name(app.roles.schema, AGENT),
            {
                "user": app.roles.agent,
                "password": app.roles.agent_password,
                "dsn": dsn(app.roles.agent, app.roles.agent_password),
            },
        )
        async with sessions() as session, session.begin():
            ids = await register(session, app, policy)

        gateway = Gateway(
            sessionmaker=sessions, secrets_root=config.state_root / "secrets"
        )
        yield Wired(app=app, gateway=gateway, sessions=sessions, **ids)
    await engine.dispose()


async def empty(engine):
    from control_plane.models import SCHEMA

    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = :s"
                " AND tablename NOT IN ('alembic_version', 'global_settings')"
            ),
            {"s": SCHEMA},
        )
        names = [f'"{SCHEMA}"."{row[0]}"' for row in rows]
        if names:
            await conn.execute(text(f"TRUNCATE {', '.join(names)} CASCADE"))
        await conn.execute(
            text(f'UPDATE "{SCHEMA}".global_settings SET agent_kill_switch = false')
        )


async def register(session, app, policy):
    """The rows a finished deploy would have left behind."""
    from control_plane import service
    from control_plane.models import App, AppUser, Deployment, DeploymentStatus, Protection

    builder = await service.create_builder(
        session, email="builder@localhost", password="not-a-real-password"
    )
    row = App(
        builder_id=builder.id,
        app_id=app.roles.schema,
        name="crm",
        subdomain="crm",
        repo="fixtures",
        protection=Protection.protected.value,
        status="live",
        agents_enabled=True,
    )
    session.add(row)
    await session.flush()

    deployment = Deployment(
        app_id=row.id,
        commit_sha="main",
        status=DeploymentStatus.live.value,
        schema_graph=app.graph,
    )
    session.add(deployment)
    await session.flush()
    row.live_deployment_id = deployment.id

    alice = AppUser(
        app_id=row.id,
        email="alice@example.com",
        password_hash="x",
        role="member",
        principal_key=str(app.plan.user_ids["A"]),
        disabled=False,
    )
    session.add(alice)
    # Somebody who may approve what the agent asks for (19.4). An admin of this
    # app, and a person: an agent has no row here at all.
    admin = AppUser(
        app_id=row.id,
        email="admin@example.com",
        password_hash="x",
        role="admin",
        principal_key=str(app.plan.user_ids["B"]),
        disabled=False,
    )
    session.add(admin)
    await session.flush()

    agent, key = await service.create_agent(
        session,
        app=row,
        name="alices-helper",
        acts_for=alice,
        created_by="builder@localhost",
        policy_yaml=policy,
    )
    return {
        "agent_id": agent.id,
        "app_uuid": row.id,
        "key": key,
        "alice": alice.id,
        "admin": admin.email,
    }
