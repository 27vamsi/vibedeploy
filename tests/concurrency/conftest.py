"""Fixtures for the M2 pooling proof. Readme.md section 8, M2.

The app runs in its own process behind PgBouncer in transaction mode, reached
over real HTTP, because that is the shape the leak would take in production:
one identity per request, many requests sharing few server connections.

Needs the local stack:
    docker compose -f infra/local/docker-compose.yml up -d postgres pgbouncer
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from contextlib import closing
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
    grant_runtime,
)
from vibedeploy_shim.identity import KEY_ENV, b64url_encode, make_header

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIM_DIR = REPO_ROOT / "shims" / "python"
BOOTSTRAP_DIR = SHIM_DIR / "bootstrap"

ADMIN_DSN = os.environ.get(
    "VD_TEST_DSN",
    "postgresql://vd_admin:vd_local_password@127.0.0.1:55432/vibedeploy",
)
# PgBouncer, not Postgres. Section 5: transaction pooling is the point.
POOLER_HOST_PORT = os.environ.get("VD_TEST_POOLER", "127.0.0.1:56432")

# A fresh app id per run. Reusing one would hand us PgBouncer's cached server
# connections from the previous run: those are still logged in as the old role
# oid, which no longer has USAGE on the recreated schema, so search_path is
# silently skipped and every query reports "relation notes does not exist".
APP_ID_PREFIX = "app_pool_"
TEST_APP_ID = APP_ID_PREFIX + secrets.token_hex(4)
USER_COUNT = 5
NOTES_PER_USER = 4

IDENTITY_KEY = secrets.token_bytes(32)


@dataclass
class Fixture:
    roles: AppRoles
    # Ground truth from the seeding plan (section 12 rule 5), not from reading
    # policies back.
    users: list[uuid.UUID] = field(default_factory=list)
    notes: dict[uuid.UUID, set[str]] = field(default_factory=dict)

    def header(self, user: uuid.UUID) -> str:
        return make_header(
            app=TEST_APP_ID, sub=str(user), role="member", key=IDENTITY_KEY
        )

    @property
    def runtime_dsn(self) -> str:
        database = ADMIN_DSN.rsplit("/", 1)[1]
        return (
            f"postgresql+asyncpg://{self.roles.runtime}:"
            f"{self.roles.runtime_password}@{POOLER_HOST_PORT}/{database}"
        )


async def _sweep_previous_runs(admin: asyncpg.Connection) -> None:
    """Fresh app ids mean a crashed run leaves roles behind. Clear them."""
    leftovers = await admin.fetch(
        "SELECT rolname FROM pg_roles WHERE rolname ~ $1",
        f"^{APP_ID_PREFIX}[0-9a-f]+_owner$",
    )
    for row in leftovers:
        app_id = row["rolname"].removesuffix("_owner")
        await drop_app_roles(admin, AppRoles.generate(app_id))


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pool_fixture() -> Fixture:
    admin = await asyncpg.connect(ADMIN_DSN)
    roles = AppRoles.generate(TEST_APP_ID)
    fixture = Fixture(roles=roles)
    try:
        await _sweep_previous_runs(admin)
        await create_app_roles(admin, roles)
        await create_helpers(admin, roles, key_type="uuid")

        host_and_db = ADMIN_DSN.split("@", 1)[1]
        migrator = await asyncpg.connect(
            f"postgresql://{roles.migrator}:{roles.migrator_password}@{host_and_db}"
        )
        try:
            await migrator.execute(
                """
                CREATE TABLE notes (
                    id       uuid PRIMARY KEY,
                    owner_id uuid NOT NULL,
                    body     text NOT NULL
                );
                CREATE INDEX ON notes (owner_id);
                """
            )
            rows = []
            for u in range(USER_COUNT):
                user = uuid.UUID(f"aaaaaaaa-0000-4000-8000-{u:012d}")
                fixture.users.append(user)
                owned = set()
                for n in range(NOTES_PER_USER):
                    note = uuid.UUID(f"{u:08d}-0000-4000-8000-{n:012d}")
                    owned.add(str(note))
                    rows.append((note, user, f"note {n} of user {u}"))
                fixture.notes[user] = owned
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

        yield fixture
    finally:
        await drop_app_roles(admin, roles)
        await admin.close()


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AppServer:
    def __init__(self, url: str, process: subprocess.Popen, log: Path) -> None:
        self.url = url
        self.process = process
        self._log = log

    def logs(self) -> str:
        return self._log.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="session")
def app_server(pool_fixture: Fixture, tmp_path_factory):
    """Starts the app in `shim` or `leaky` mode and waits until it answers.

    One process per mode for the whole session: booting uvicorn is slower than
    any of the tests that use it.
    """

    tmp_path = tmp_path_factory.mktemp("app")
    started: dict[str, AppServer] = {}

    def start(mode: str = "shim") -> AppServer:
        if mode in started:
            return started[mode]
        port = _free_port()
        env = dict(os.environ)
        env["VD_APP_MODE"] = mode
        env["VD_APP_DATABASE_URL"] = pool_fixture.runtime_dsn
        env[KEY_ENV] = b64url_encode(IDENTITY_KEY)
        # shim mode gets the bootstrap directory, so Python imports
        # sitecustomize and the shim installs itself. leaky mode does not.
        path = [str(REPO_ROOT), str(SHIM_DIR)]
        if mode == "shim":
            path.insert(0, str(BOOTSTRAP_DIR))
        env["PYTHONPATH"] = os.pathsep.join(path)
        env["PYTHONUNBUFFERED"] = "1"

        log = tmp_path / f"{mode}-{port}.log"
        handle = log.open("wb")
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
        server = AppServer(f"http://127.0.0.1:{port}", process, log)
        started[mode] = server

        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"app exited early:\n{server.logs()}")
            try:
                if httpx.get(f"{server.url}/notes", timeout=2).status_code == 200:
                    return server
            except httpx.HTTPError:
                time.sleep(0.2)
        raise RuntimeError(f"app did not start:\n{server.logs()}")

    try:
        yield start
    finally:
        for server in started.values():
            server.process.terminate()
            try:
                server.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.process.kill()
