"""The whole local stack, as three real processes. Readme.md section 8 M6.

    browser  ->  sidecar :N  ->  app :M  ->  Postgres
                     |
                     +-------->  control plane stub :K

Nothing is faked between the browser and the database. The app runs as its own
process with the shim delivered the way a deployed container gets it (a
bootstrap directory on `PYTHONPATH`, never an import), and the sidecar runs as
its own process reached over real HTTP.

The app listens on 127.0.0.1 only, which is the local stand-in for section 16's
"sidecar and app share a network namespace": the tests can still reach it
directly, and one of them does, precisely to show what that would get you.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
import httpx
import pytest
import pytest_asyncio

from buildjob.seed import SeedPlan
from tests.attack.conftest import build
from tests.kernel.conftest import (
    ADMIN_PASSWORD,
    ADMIN_USER,
    PG_DB,
    PG_HOST,
    PG_PORT,
    dsn,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "shims" / "python" / "bootstrap"
SHIM_PKG = REPO_ROOT / "shims" / "python"

PASSWORDS = {"A": "alice-password", "B": "bob-password"}
EMAILS = {"A": "alice@example.com", "B": "bob@example.com"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Stack:
    app_id: str
    plan: SeedPlan
    model: dict
    identity_key: str
    session_key: str
    api_key: str
    sidecar_url: str
    app_url: str
    control_plane_url: str
    processes: list = field(default_factory=list)

    def invoices_of(self, persona: str) -> set[str]:
        """Straight from the seeding plan. Never from asking the app."""
        return {str(key[0]) for key in self.plan.owned_by("invoices", persona)}

    def sub(self, persona: str) -> str:
        return str(self.plan.user_ids[persona])


class _Process:
    def __init__(self, name: str, argv: list[str], env: dict, log: Path):
        self._log_path = log
        self._handle = open(log, "w", encoding="utf-8")
        self._name = name
        self.process = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            env=env,
            stdout=self._handle,
            stderr=subprocess.STDOUT,
        )

    def output(self) -> str:
        self._handle.flush()
        return self._log_path.read_text(encoding="utf-8", errors="replace")

    def wait_for(self, url: str, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"{self._name} exited early:\n{self.output()}")
            try:
                if httpx.get(url, timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        raise RuntimeError(f"{self._name} never became ready:\n{self.output()}")

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self._handle.close()


def _env(**extra) -> dict:
    sep = ";" if sys.platform == "win32" else ":"
    env = dict(os.environ)
    env["PYTHONPATH"] = sep.join([str(BOOTSTRAP), str(SHIM_PKG), str(REPO_ROOT)])
    env["PYTHONUNBUFFERED"] = "1"
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _uvicorn(target: str, port: int) -> list[str]:
    return [
        sys.executable, "-m", "uvicorn", target,
        "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
    ]


@pytest_asyncio.fixture(scope="session")
async def stack(tmp_path_factory):
    logs = tmp_path_factory.mktemp("e2e")
    admin = await asyncpg.connect(dsn(ADMIN_USER, ADMIN_PASSWORD))
    processes: list[_Process] = []
    try:
        async with build(admin, "crm_chain") as app:
            roles, plan = app.roles, app.plan

            # Give the two seeded members recognisable logins. The control
            # plane owns this in production (section 16: "Adding a user inserts
            # a row into the app's users table, as migrator").
            mig = await asyncpg.connect(dsn(roles.migrator, roles.migrator_password))
            try:
                for persona in ("A", "B"):
                    await mig.execute(
                        "UPDATE users SET email = $1, name = $2 WHERE id = $3",
                        EMAILS[persona],
                        persona,
                        plan.user_ids[persona],
                    )
            finally:
                await mig.close()

            identity_key = secrets.token_hex(32)
            session_key = secrets.token_hex(32)
            api_key = secrets.token_hex(16)

            cp_port, app_port, gate_port = (_free_port() for _ in range(3))
            control_plane_url = f"http://127.0.0.1:{cp_port}"
            app_url = f"http://127.0.0.1:{app_port}"
            sidecar_url = f"http://127.0.0.1:{gate_port}"

            users = {
                EMAILS[p]: {
                    "password": PASSWORDS[p],
                    "sub": str(plan.user_ids[p]),
                    "role": "member",
                    "session_version": 1,
                }
                for p in ("A", "B")
            }

            control_plane = _Process(
                "control plane stub",
                _uvicorn("tests.e2e.control_plane_stub:app", cp_port),
                _env(
                    VD_APP_ID=roles.app_id,
                    VD_SIDECAR_API_KEY=api_key,
                    VD_STUB_USERS=json.dumps(users),
                ),
                logs / "control_plane.log",
            )
            processes.append(control_plane)
            control_plane.wait_for(f"{control_plane_url}/health")

            runtime_dsn = (
                f"postgresql+asyncpg://{roles.runtime}:{roles.runtime_password}"
                f"@{PG_HOST}:{PG_PORT}/{PG_DB}"
            )
            demo = _Process(
                "demo app",
                _uvicorn("fixtures.apps.invoices.app:app", app_port),
                _env(
                    VD_DATABASE_URL=runtime_dsn,
                    VD_IDENTITY_KEY=identity_key,
                    VD_APP_ID=roles.app_id,
                ),
                logs / "app.log",
            )
            processes.append(demo)
            demo.wait_for(f"{app_url}/health")

            gate = _Process(
                "sidecar",
                _uvicorn("sidecar.server:app", gate_port),
                _env(
                    VD_APP_ID=roles.app_id,
                    VD_IDENTITY_KEY=identity_key,
                    VD_SESSION_KEY=session_key,
                    VD_CONTROL_PLANE_URL=control_plane_url,
                    VD_SIDECAR_API_KEY=api_key,
                    VD_UPSTREAM=app_url,
                    VD_COOKIE_SECURE="0",
                    # Every "browser" in the suite is 127.0.0.1 and there are
                    # only two seeded people, so the suite would spend both
                    # allowances on itself. The limits are proven at their real
                    # values in tests/sidecar/test_login.py instead.
                    VD_LOGIN_IP_LIMIT="10000",
                    VD_LOGIN_EMAIL_LIMIT="10000",
                ),
                logs / "sidecar.log",
            )
            processes.append(gate)
            gate.wait_for(f"{sidecar_url}/__vd/health")

            yield Stack(
                app_id=roles.app_id,
                plan=plan,
                model=app.model,
                identity_key=identity_key,
                session_key=session_key,
                api_key=api_key,
                sidecar_url=sidecar_url,
                app_url=app_url,
                control_plane_url=control_plane_url,
                processes=processes,
            )
    finally:
        for process in reversed(processes):
            process.stop()
        await admin.close()


async def sign_in(stack: Stack, persona: str) -> httpx.AsyncClient:
    """One browser, signed in. The caller closes it."""
    browser = httpx.AsyncClient(
        base_url=stack.sidecar_url, follow_redirects=True, timeout=20
    )
    response = await browser.post(
        "/__vd/login",
        data={
            "email": EMAILS[persona],
            "password": PASSWORDS[persona],
            "next": "/",
        },
    )
    assert response.status_code == 200, response.text
    return browser


@pytest.fixture
def emails():
    return EMAILS
