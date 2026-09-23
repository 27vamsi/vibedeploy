"""Harness for the pooling proof. Readme.md section 8 M2.

Runs the sloppy app as a real subprocess, reached over real HTTP, talking to
Postgres through PgBouncer in transaction mode. The shim is delivered the way it
is delivered in production: a bootstrap directory on PYTHONPATH, never an import
in the app.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
import httpx
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

PGBOUNCER_HOST = os.environ.get("VD_PGBOUNCER_HOST", "127.0.0.1")
PGBOUNCER_PORT = int(os.environ.get("VD_PGBOUNCER_PORT", "56432"))

USER_COUNT = 5
NOTES_PER_USER = 3


@dataclass
class Deployment:
    roles: AppRoles
    identity_key: str
    users: list[uuid.UUID] = field(default_factory=list)
    notes: dict[uuid.UUID, set[str]] = field(default_factory=dict)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest_asyncio.fixture(scope="session")
async def deployment() -> Deployment:
    """A fresh app id every run.

    Reusing one is poison: PgBouncer keeps server connections logged in as the
    old role's oid, which no longer has USAGE on the recreated schema, and every
    query then fails with `relation "notes" does not exist`.
    """
    admin = await asyncpg.connect(dsn(ADMIN_USER, ADMIN_PASSWORD))
    roles = AppRoles.generate(f"app_c{secrets.token_hex(4)}")
    built = Deployment(roles=roles, identity_key=secrets.token_hex(32))

    try:
        await drop_app_roles(admin, roles)
        await create_app_roles(admin, roles)
        await create_helpers(admin, roles, key_type="uuid")

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
            for n in range(USER_COUNT):
                user_id = uuid.uuid4()
                built.users.append(user_id)
                built.notes[user_id] = set()
                await mig.execute(
                    "INSERT INTO users (id, email) VALUES ($1, $2)",
                    user_id,
                    f"user{n}@example.com",
                )
                for k in range(NOTES_PER_USER):
                    note_id = uuid.uuid4()
                    await mig.execute(
                        "INSERT INTO notes (id, owner_id, body) VALUES ($1, $2, $3)",
                        note_id,
                        user_id,
                        f"note {k} of user {n}",
                    )
                    built.notes[user_id].add(str(note_id))
        finally:
            await mig.close()

        tables = ["users", "notes"]
        await reassign_objects_to_owner(admin, roles)
        await enable_rls(admin, roles, tables)
        await apply_owner_column_policy(
            admin, roles, table="users", column="id", admin_enabled=False
        )
        await apply_owner_column_policy(
            admin, roles, table="notes", column="owner_id", admin_enabled=False
        )
        await grant_app_roles(admin, roles, tables)

        yield built
    finally:
        await drop_app_roles(admin, roles)
        await admin.close()


class ServerHandle:
    def __init__(self, base_url: str, log_path: Path, process):
        self.base_url = base_url
        self._log_path = log_path
        self._process = process

    def output(self) -> str:
        return self._log_path.read_text(encoding="utf-8", errors="replace")


def _start_server(deployment: Deployment, *, leak: bool, tmp_path: Path):
    """Launch the app exactly as a deployed container would."""
    port = _free_port()
    runtime_dsn = (
        f"postgresql+asyncpg://{deployment.roles.runtime}:"
        f"{deployment.roles.runtime_password}@{PGBOUNCER_HOST}:{PGBOUNCER_PORT}/{PG_DB}"
    )

    sep = ";" if sys.platform == "win32" else ":"
    env = dict(os.environ)
    env["PYTHONPATH"] = sep.join([str(BOOTSTRAP), str(SHIM_PKG), str(REPO_ROOT)])
    env["VD_TEST_DSN"] = runtime_dsn
    env["VD_IDENTITY_KEY"] = deployment.identity_key
    env["VD_APP_ID"] = deployment.roles.app_id
    env["PYTHONUNBUFFERED"] = "1"
    if leak:
        env["VD_TEST_LEAK"] = "1"
        # The broken variant must not be rescued by the shim.
        env["VD_SHIM_DISABLED"] = "1"

    log_path = tmp_path / f"server-{'leaky' if leak else 'shimmed'}.log"
    handle = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.concurrency.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    try:
        while time.time() < deadline:
            if process.poll() is not None:
                handle.flush()
                raise RuntimeError(
                    f"server exited early:\n{log_path.read_text(errors='replace')}"
                )
            try:
                if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise RuntimeError("server did not become ready")
    except Exception:
        process.kill()
        handle.close()
        raise

    return ServerHandle(base_url, log_path, process), process, handle


def _stop(process, handle) -> None:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
    handle.close()


@pytest.fixture(scope="session")
def server(deployment, tmp_path_factory):
    """The real thing: sloppy app, shim auto-installed, behind PgBouncer."""
    handle, process, log = _start_server(
        deployment, leak=False, tmp_path=tmp_path_factory.mktemp("shimmed")
    )
    try:
        yield handle
    finally:
        _stop(process, log)


@pytest.fixture(scope="session")
def leaky_server(deployment, tmp_path_factory):
    """The negative control. Proves the test can see a leak when there is one."""
    handle, process, log = _start_server(
        deployment, leak=True, tmp_path=tmp_path_factory.mktemp("leaky")
    )
    try:
        yield handle
    finally:
        _stop(process, log)
