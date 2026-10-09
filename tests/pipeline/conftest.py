"""The whole local pipeline, wired up the way it actually runs. M7.

Two things are real processes here and one is not, and the split is deliberate:

  - the **control plane** runs as its own uvicorn process, because the build job
    reports its verdict over HTTP with a one-time token and that path has to be
    the one that is tested. A stub would test the stub.
  - the **build job** and the **migration task** are subprocesses, started by
    the worker exactly as it starts them in anger. App migrations never execute
    in this test process for the same reason they never execute in the worker.
  - the **worker loop** is driven in-process by calling `run_once`, so a test
    can say "now do the next thing" and then look. It is the same function the
    long-running worker calls; only the sleeping is missing.

Every test gets its own app id, so the schemas and roles a run leaves in the
local Postgres are named after it and are dropped at the end.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest_asyncio
from sqlalchemy import select, text

from control_plane import migrate, service
from control_plane.config import DEFAULT_APP_DSN, REPO_ROOT, ControlPlaneConfig
from control_plane.db import make_engine, make_sessionmaker
from control_plane.models import (
    AccessModel,
    App,
    AppUser,
    Deployment,
    Job,
    SCHEMA,
    VerificationRun,
)
from control_plane.web.routes import _sole_builder
from kernel.provision import AppRoles, drop_app_roles
from worker import main as worker_main
from worker.runtimes import local

REPO = REPO_ROOT / "fixtures" / "repos" / "todo"

# A database of its own, so a test run can truncate everything in it without
# ever being one typo away from the developer's own control plane.
CONTROL_DB = "vibedeploy_pipeline_test"
CONTROL_URL = (
    "postgresql+asyncpg://vd_admin:vd_local_password@127.0.0.1:55432/" + CONTROL_DB
)

# Section 10's four questions, answered the way a one-person todo app would be.
ANSWERS: dict[str, Any] = {
    "audience": "me",
    "size": "small",
    "visibility": "own_data",
    "sensitive": False,
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Harness:
    config: ControlPlaneConfig
    sessions: Any
    # Where the gateway process is listening (section 21). The console talks to
    # it over HTTP with `console_key`; an agent talks to `/mcp` with its own.
    gateway_url: str = ""
    console_key: str = ""
    app_ids: list[str] = field(default_factory=list)

    async def drain(self, limit: int = 10) -> None:
        """Run jobs until the queue has nothing runnable left.

        A parked job is not runnable, which is exactly how "stopped, waiting for
        a person" ends this loop without anything having to know about it.
        """
        for _ in range(limit):
            if not await worker_main.run_once(self.config, self.sessions, "test"):
                return
        raise AssertionError("the worker never ran out of jobs")

    async def deploy(self, branch: str, name: str | None = None) -> uuid.UUID:
        """Create an app, queue `branch`, and run until the pipeline stops."""
        async with self.sessions() as session, session.begin():
            builder = await _sole_builder(session)
            app = await service.create_app(
                session,
                builder=builder,
                name=name or f"{branch}-{secrets.token_hex(3)}",
                repo=str(REPO),
            )
            deployment = await service.start_deployment(
                session, app=app, commit_sha=branch, answers=ANSWERS
            )
            self.app_ids.append(app.app_id)
            deployment_id = deployment.id
        await self.drain()
        return deployment_id

    async def deployment(self, deployment_id: uuid.UUID) -> Deployment:
        async with self.sessions() as session:
            return await session.get(Deployment, deployment_id)

    async def app_of(self, deployment_id: uuid.UUID) -> App:
        async with self.sessions() as session:
            deployment = await session.get(Deployment, deployment_id)
            return await session.get(App, deployment.app_id)

    async def model_of(self, deployment_id: uuid.UUID) -> AccessModel | None:
        async with self.sessions() as session:
            return (
                await session.execute(
                    select(AccessModel).where(
                        AccessModel.deployment_id == deployment_id
                    )
                )
            ).scalar_one_or_none()

    async def answer(self, deployment_id: uuid.UUID, answers: dict[str, str]) -> None:
        async with self.sessions() as session, session.begin():
            deployment = await session.get(Deployment, deployment_id)
            await service.save_answers(
                session, deployment=deployment, answers=answers
            )
        await self.drain()

    async def confirm(self, deployment_id: uuid.UUID) -> None:
        async with self.sessions() as session, session.begin():
            deployment = await session.get(Deployment, deployment_id)
            await service.confirm_model(
                session, deployment=deployment, confirmed_by="builder@localhost"
            )
        await self.drain()

    async def job_of(self, deployment_id: uuid.UUID) -> Job:
        async with self.sessions() as session:
            return (
                await session.execute(
                    select(Job).where(Job.deployment_id == deployment_id)
                )
            ).scalar_one()

    async def runs(self, deployment_id: uuid.UUID) -> dict[str, VerificationRun]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(VerificationRun).where(
                        VerificationRun.deployment_id == deployment_id
                    )
                )
            ).scalars()
            return {row.role: row for row in rows}

    async def add_person(
        self,
        deployment_id: uuid.UUID,
        email: str,
        password: str,
        role: str = "member",
    ) -> AppUser:
        """Invite somebody and let the worker put them in the app's database."""
        async with self.sessions() as session, session.begin():
            deployment = await session.get(Deployment, deployment_id)
            app = await session.get(App, deployment.app_id)
            user = await service.add_app_user(
                session, app=app, email=email, password=password, role=role
            )
            user_id = user.id
        await self.drain()
        async with self.sessions() as session:
            return await session.get(AppUser, user_id)

    async def give_todo(self, app_id: str, owner: str, title: str) -> None:
        """Put a row in the live app's own database, as the admin.

        The fixture app only reads, so somebody has to write. Doing it here
        rather than through the app keeps the app free of any security-shaped
        code, which is the whole point of it.
        """
        conn = await asyncpg.connect(self.config.app_admin_dsn)
        try:
            await conn.execute(
                f'INSERT INTO "{app_id}".todos (owner_id, title) VALUES ($1, $2)',
                uuid.UUID(owner),
                title,
            )
        finally:
            await conn.close()


class _Process:
    def __init__(self, name: str, argv: list[str], env: dict, log: Path):
        self.name = name
        self._log = log
        self._handle = open(log, "w", encoding="utf-8")
        self.process = subprocess.Popen(
            argv,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=self._handle,
            stderr=subprocess.STDOUT,
        )

    def wait_for(self, url: str, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"{self.name} exited early:\n{self._tail()}")
            try:
                if httpx.get(url, timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        raise RuntimeError(f"{self.name} never became ready:\n{self._tail()}")

    def _tail(self) -> str:
        self._handle.flush()
        return self._log.read_text(encoding="utf-8", errors="replace")

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self._handle.close()


async def _truncate(config: ControlPlaneConfig) -> None:
    """Empty the control plane, but leave the singletons alone.

    `global_settings` holds one row that the migrations insert, and the
    gateway fails closed when it is missing: no settings row reads as "agents
    are switched off everywhere". Truncating it would leave every agent in the
    test database denied, which looks like a gateway bug and is not one.
    """
    engine = make_engine(config)
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
        # Put it back if an earlier run truncated it away, then make sure the
        # switch is off.
        await conn.execute(
            text(
                f'INSERT INTO "{SCHEMA}".global_settings (id, agent_kill_switch)'
                " VALUES (1, false) ON CONFLICT (id)"
                " DO UPDATE SET agent_kill_switch = false"
            )
        )
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def pipeline(tmp_path_factory):
    root = tmp_path_factory.mktemp("pipeline")
    port = _free_port()
    gateway_port = _free_port()
    console_key = secrets.token_hex(16)
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    config = ControlPlaneConfig(
        database_url=CONTROL_URL,
        app_admin_dsn=DEFAULT_APP_DSN,
        sidecar_api_key=secrets.token_hex(16),
        state_root=root / "state",
        public_url=f"http://127.0.0.1:{port}",
        gateway_url=gateway_url,
        gateway_api_key=console_key,
    )

    await migrate.prepare(config)
    await _truncate(config)

    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(REPO_ROOT),
        PYTHONUNBUFFERED="1",
        VD_CONTROL_DATABASE_URL=config.database_url,
        VD_APP_ADMIN_DSN=config.app_admin_dsn,
        VD_SIDECAR_API_KEY=config.sidecar_api_key,
        VD_STATE_ROOT=str(config.state_root),
        VD_CONTROL_PLANE_URL=config.public_url,
        VD_GATEWAY_URL=gateway_url,
        VD_GATEWAY_API_KEY=console_key,
    )
    control_plane = _Process(
        "control plane",
        [
            sys.executable, "-m", "uvicorn", "control_plane.app:create_app",
            "--factory", "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning",
        ],
        env,
        root / "control_plane.log",
    )
    # A separate process on purpose, and not a convenience: in AWS these are
    # two ECS tasks with two IAM roles, and the console's role cannot read an
    # `agent-*` secret. Running them as one process here would let a test pass
    # that could not possibly work deployed.
    gateway = _Process(
        "gateway",
        [
            sys.executable, "-m", "uvicorn", "gateway.app:build",
            "--factory", "--host", "127.0.0.1", "--port", str(gateway_port),
            "--log-level", "warning",
        ],
        env,
        root / "gateway.log",
    )

    engine = make_engine(config)
    harness = Harness(
        config=config,
        sessions=make_sessionmaker(engine),
        gateway_url=gateway_url,
        console_key=console_key,
    )
    try:
        control_plane.wait_for(f"{config.public_url}/health")
        gateway.wait_for(f"{gateway_url}/internal/health")
        yield harness
    finally:
        for app_id in harness.app_ids:
            local.stop(config, app_id)
        gateway.stop()
        control_plane.stop()
        await engine.dispose()
        await _drop_apps(config, harness.app_ids)


async def _drop_apps(config: ControlPlaneConfig, app_ids: list[str]) -> None:
    """Leave the local Postgres as we found it.

    Also sweeps the build job's scratch roles: they are dropped by the build job
    itself, but a build that was killed mid-flight would otherwise leave four
    roles and a schema behind on a developer's machine forever.
    """
    admin = await asyncpg.connect(config.app_admin_dsn)
    try:
        for app_id in app_ids:
            await drop_app_roles(admin, AppRoles.generate(app_id))
        leftovers = await admin.fetch(
            "SELECT DISTINCT substring(rolname from '^(app_b[0-9a-f]+)_')"
            " AS stem FROM pg_roles WHERE rolname LIKE 'app\\_b%'"
        )
        for row in leftovers:
            if row["stem"]:
                await drop_app_roles(admin, AppRoles.generate(row["stem"]))
    finally:
        await admin.close()


async def sign_in(endpoint: str, email: str, password: str) -> httpx.AsyncClient:
    """One browser, signed in through the gate. The caller closes it."""
    browser = httpx.AsyncClient(base_url=endpoint, follow_redirects=True, timeout=20)
    response = await browser.post(
        "/__vd/login", data={"email": email, "password": password, "next": "/"}
    )
    assert response.status_code == 200, response.text
    return browser


async def settle() -> None:
    """Give a just-started process a moment. Only used after a deploy goes live."""
    await asyncio.sleep(0.2)
