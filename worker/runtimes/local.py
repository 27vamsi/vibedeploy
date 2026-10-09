"""Live, locally: the app and its gate, as two real processes. Readme.md 16.

    browser -> sidecar :N -> app :M -> Postgres

Nothing is faked between the two. The app is started with the identity shim
delivered exactly the way a deployed container gets it, a bootstrap directory on
`PYTHONPATH` and never an import, so the app's own code has no idea it is there.
That is the same arrangement `tests/e2e` already proves end to end.

Two deliberate omissions against section 16, both deferred to M8 with the rest
of the cloud runtime:

  - the app listens on 127.0.0.1 rather than sharing a network namespace with
    the sidecar, so locally it is reachable directly. That is a property of this
    machine, not of the enforcement: reaching the app directly gets no identity
    header, and the shim then sets nobody, and the policies return no rows.
  - there is no Caddy in front, so the endpoint is a port rather than
    `<subdomain>.localhost`.

The repo root is deliberately **not** on the app's `PYTHONPATH`. An app must not
be able to import the control plane, the worker or the kernel.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from control_plane import secrets as secret_store
from control_plane.config import REPO_ROOT, ControlPlaneConfig
from control_plane.secrets import SecretStore

BOOTSTRAP = REPO_ROOT / "shims" / "python" / "bootstrap"
SHIM_PKG = REPO_ROOT / "shims" / "python"
PATH_SEP = ";" if sys.platform == "win32" else ":"

START_TIMEOUT = 60.0

NO_ENTRYPOINT = (
    "We could not find the file that starts this app. V0.5 looks for `app.py`,"
    " `main.py` or `application.py` in the root of the repository, exposing an"
    " application called `app`."
)


class StartupFailed(RuntimeError):
    """Starting the app failed, in words a builder can act on."""


@dataclass(frozen=True)
class Running:
    app_id: str
    endpoint: str
    app_port: int
    sidecar_port: int
    app_pid: int
    sidecar_pid: int


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _record_path(config: ControlPlaneConfig, app_id: str) -> Path:
    return config.state_root / "running" / f"{app_id}.json"


def _uvicorn(target: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        target,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]


def _spawn(argv: list[str], env: dict[str, str], cwd: Path, log: Path) -> subprocess.Popen:
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log, "w", encoding="utf-8")
    return subprocess.Popen(
        argv, cwd=str(cwd), env=env, stdout=handle, stderr=subprocess.STDOUT
    )


async def _wait_for_port(process: subprocess.Popen, port: int, name: str, log: Path) -> None:
    """Listening is the readiness signal, because we do not own the app's routes.

    Anything else would be a convention imposed on an app we did not write.
    """
    deadline = asyncio.get_running_loop().time() + START_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        if process.poll() is not None:
            raise StartupFailed(f"{name} stopped while starting up.\n{_tail(log)}")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.2)
            continue
        writer.close()
        await writer.wait_closed()
        del reader
        return
    raise StartupFailed(f"{name} never started listening.\n{_tail(log)}")


async def _wait_for_health(process: subprocess.Popen, url: str, name: str, log: Path) -> None:
    deadline = asyncio.get_running_loop().time() + START_TIMEOUT
    async with httpx.AsyncClient(timeout=2.0) as client:
        while asyncio.get_running_loop().time() < deadline:
            if process.poll() is not None:
                raise StartupFailed(f"{name} stopped while starting up.\n{_tail(log)}")
            try:
                if (await client.get(url)).status_code == 200:
                    return
            except httpx.HTTPError:
                await asyncio.sleep(0.2)
    raise StartupFailed(f"{name} never became healthy.\n{_tail(log)}")


def _tail(log: Path, limit: int = 2000) -> str:
    try:
        return log.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def stop(config: ControlPlaneConfig, app_id: str) -> None:
    """Stop whatever this app was running, if anything.

    Local only, and it trusts a recorded pid, which is fine on a laptop and is
    one of the reasons this module does not exist in the cloud.
    """
    record = _record_path(config, app_id)
    try:
        running = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for pid in (running.get("sidecar_pid"), running.get("app_pid")):
        if not pid:
            continue
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    record.unlink(missing_ok=True)


async def start(
    config: ControlPlaneConfig,
    *,
    app_id: str,
    checkout: Path,
    entrypoint: str | None,
    logs: Path,
) -> Running:
    if not entrypoint:
        raise StartupFailed(NO_ENTRYPOINT)

    store = SecretStore(config.state_root / "secrets")
    runtime = store.get(secret_store.secret_name(app_id, secret_store.RUNTIME))
    gate = store.get(secret_store.secret_name(app_id, secret_store.SIDECAR))

    stop(config, app_id)

    app_port, sidecar_port = _free_port(), _free_port()
    app_url = f"http://127.0.0.1:{app_port}"
    endpoint = f"http://127.0.0.1:{sidecar_port}"

    base = dict(os.environ)
    base["PYTHONUNBUFFERED"] = "1"

    app_log = logs / "app.log"
    app_env = {
        **base,
        # Bootstrap first: `sitecustomize` has to be found before anything the
        # app itself might ship. The repo root is absent on purpose.
        "PYTHONPATH": PATH_SEP.join([str(BOOTSTRAP), str(SHIM_PKG), str(checkout)]),
        "VD_DATABASE_URL": runtime["dsn"].replace(
            "postgresql://", "postgresql+asyncpg://", 1
        ),
        "VD_IDENTITY_KEY": gate["identity_key"],
        "VD_APP_ID": app_id,
    }
    app_process = _spawn(_uvicorn(entrypoint, app_port), app_env, checkout, app_log)
    await _wait_for_port(app_process, app_port, "The app", app_log)

    sidecar_log = logs / "sidecar.log"
    sidecar_env = {
        **base,
        # The gate signs the identity header with the same code the shim
        # verifies it with, so the shim package is on its path too. The
        # bootstrap directory is not: nothing of ours wants `sitecustomize`.
        "PYTHONPATH": PATH_SEP.join([str(SHIM_PKG), str(REPO_ROOT)]),
        "VD_APP_ID": app_id,
        "VD_IDENTITY_KEY": gate["identity_key"],
        "VD_SESSION_KEY": gate["session_key"],
        "VD_CONTROL_PLANE_URL": config.public_url,
        "VD_SIDECAR_API_KEY": config.sidecar_api_key,
        "VD_UPSTREAM": app_url,
        # No TLS in front of it locally, so a Secure cookie would never be sent
        # back and nobody could stay logged in.
        "VD_COOKIE_SECURE": "0",
    }
    sidecar_process = _spawn(
        _uvicorn("sidecar.server:app", sidecar_port), sidecar_env, REPO_ROOT, sidecar_log
    )
    try:
        await _wait_for_health(
            sidecar_process, f"{endpoint}/__vd/health", "The login gate", sidecar_log
        )
    except StartupFailed:
        # An app with no gate in front of it must not be left listening.
        sidecar_process.terminate()
        app_process.terminate()
        raise

    running = Running(
        app_id=app_id,
        endpoint=endpoint,
        app_port=app_port,
        sidecar_port=sidecar_port,
        app_pid=app_process.pid,
        sidecar_pid=sidecar_process.pid,
    )
    record = _record_path(config, app_id)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(asdict(running)), encoding="utf-8")
    return running
