"""Live, locally: the app and its gate, as two real processes. Readme.md 16.

    browser -> sidecar :N -> app :M -> Postgres

Nothing is faked between the two. The app is started with the identity shim
delivered exactly the way a deployed container gets it, a bootstrap directory on
`PYTHONPATH` and never an import, so the app's own code has no idea it is there.
That is the same arrangement `tests/e2e` already proves end to end.

Three deliberate omissions against section 16, all of them properties of this
module and not of the enforcement, and all of them the reason `apprt` exists:

  - the app listens on 127.0.0.1 rather than sharing a network namespace with
    the sidecar, so locally it is reachable directly. Reaching it directly gets
    no identity header, the shim then sets nobody, and the policies return no
    rows — but "nobody can reach it" is a stronger claim than that and this
    cannot make it;
  - the app runs as the worker's own user, in the worker's own filesystem, and
    therefore within reach of the worker's own credentials;
  - the app runs on `sys.executable`, which is the *worker's* interpreter, so it
    imports the worker's packages and not the ones its own
    `requirements.txt` asked for.

The sidecar's port is the private half of the arrangement. It is what the proxy
dials, and `worker/runtimes/proxy.py` is what publishes `<subdomain>.localhost`
in front of it; nothing hands that port out as an app's address.

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
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Collection

import httpx

from control_plane import appenv
from control_plane import secrets as secret_store
from control_plane.config import REPO_ROOT, ControlPlaneConfig
from worker.pipeline import build, container
from worker.runtimes.base import NO_ENTRYPOINT, Running, StartupFailed, Watcher

BOOTSTRAP = REPO_ROOT / "shims" / "python" / "bootstrap"
SHIM_PKG = REPO_ROOT / "shims" / "python"
PATH_SEP = ";" if sys.platform == "win32" else ":"

START_TIMEOUT = 60.0

# One connect attempt, to something on this machine that is either listening or
# is not. Long enough to survive a laptop that has just started swapping.
PROBE_TIMEOUT = 2.0

__all__ = [
    "NO_ENTRYPOINT",
    "Running",
    "StartupFailed",
    "adopt",
    "alive",
    "apps",
    "generations",
    "kill",
    "read",
    "reap",
    "start",
    "stop",
    "watcher",
]


@dataclass(frozen=True)
class _Record:
    """What is written to disk: a `Running` plus how it was started.

    The ports and pids are this module's business and nobody else's, which is
    why they are here and not on `Running`. A caller that wanted a port would
    be a caller that could not work against the containerised runtime.
    """

    app_id: str
    endpoint: str
    upstream: str
    app_port: int
    sidecar_port: int
    app_pid: int
    sidecar_pid: int
    generation: int = 0
    deployment_id: str = ""

    def running(self) -> Running:
        return Running(
            app_id=self.app_id,
            endpoint=self.endpoint,
            upstream=self.upstream,
            generation=self.generation,
            deployment_id=self.deployment_id,
        )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _records(config: ControlPlaneConfig, app_id: str) -> Path:
    return config.state_root / "running" / app_id


def _record_path(config: ControlPlaneConfig, app_id: str, generation: int) -> Path:
    """One file per generation, not one per app.

    One per app would mean a second deployment's record overwriting the first
    one's, and then nothing on this machine knows which pids belong to the
    version that is still serving traffic. Two versions of an app running at
    once is the normal case from here on — it is what a health-gated cutover
    needs — so the generation is part of the name.
    """
    return _records(config, app_id) / f"{generation}.json"


def _read_record(
    config: ControlPlaneConfig, app_id: str, generation: int
) -> _Record | None:
    try:
        data = json.loads(
            _record_path(config, app_id, generation).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    try:
        return _Record(**data)
    except TypeError:
        # A record written by an older shape of this module. Treated as absent
        # rather than guessed at: the caller starts a replacement, which
        # overwrites it.
        return None


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


async def apps(config: ControlPlaneConfig) -> list[str]:
    """Every app this machine has processes recorded for.

    Enumerated from disk rather than from the `apps` table, because the whole
    point of a reconciler is to find what the database has stopped mentioning:
    an app deleted from the dashboard while its process was running would never
    appear in a sweep driven by the database, and its uvicorn would go on
    serving and holding connections for as long as the laptop was up.
    """
    try:
        return sorted(
            path.name
            for path in (config.state_root / "running").iterdir()
            if path.is_dir()
        )
    except OSError:
        return []


async def generations(config: ControlPlaneConfig, app_id: str) -> list[int]:
    """Every generation of this app with processes recorded on this machine.

    Read off the filesystem rather than out of the database on purpose: this is
    the "actual" half of a desired-versus-actual comparison, and asking the
    database what is running would make that comparison vacuous.
    """
    try:
        names = sorted(_records(config, app_id).glob("*.json"))
    except OSError:
        return []
    found = []
    for name in names:
        try:
            found.append(int(name.stem))
        except ValueError:
            continue
    return sorted(found)


async def read(
    config: ControlPlaneConfig, app_id: str, generation: int
) -> Running | None:
    """Where this generation of the app is actually listening, or `None`.

    The other half of a desired-versus-actual pair: the database says *which*
    generation has the traffic, and this says *where* that generation is. The
    proxy needs both, and taking both from the same place would make the
    comparison a reconciler does vacuous.
    """
    record = _read_record(config, app_id, generation)
    return None if record is None else record.running()


def listening(port: int) -> bool:
    """Is anything accepting connections there?

    The only liveness signal worth having locally. A pid is not one: once a
    worker has restarted, a pid out of a record is all that is left of a process
    it started, the number may since have been reused, and on Windows asking
    whether it is alive with `os.kill(pid, 0)` *terminates* it. "It is still
    serving" is both checkable and the thing anybody actually cares about.
    """
    with socket.socket() as sock:
        sock.settimeout(PROBE_TIMEOUT)
        return sock.connect_ex(("127.0.0.1", port)) == 0


async def alive(config: ControlPlaneConfig, running: Running) -> bool:
    """Is the app itself — not its gate — still there?

    The second half of the health gate. Separate from the gate's own health
    endpoint because the two fail differently: a gate with no app behind it
    passes its own check and turns every request into a 502 that looks like the
    platform's fault.
    """
    record = _read_record(config, running.app_id, running.generation)
    if record is None:
        return False
    return await asyncio.to_thread(listening, record.app_port)


async def adopt(
    config: ControlPlaneConfig, app_id: str, generation: int
) -> Running | None:
    """The processes already serving this generation, if they still are.

    What a resumed deployment calls before it starts anything. A worker that
    stopped existing between starting a version and publishing it leaves two
    live processes and a record of them, and the worker that picks the job up
    has to find them rather than start a second pair: two app processes for one
    generation would both answer, the record would name one of them, and the
    other would be unreachable, unkillable through `reap`, and still holding
    connections to the app's database.

    Both halves have to be answering, because a half-adopted version is worse
    than none: a sidecar with no app behind it passes its own health check and
    turns every request into a 502 that looks like the platform's fault.
    """
    record = _read_record(config, app_id, generation)
    if record is None:
        return None
    both = await asyncio.to_thread(
        lambda: listening(record.sidecar_port) and listening(record.app_port)
    )
    if not both:
        # Left alone rather than deleted. The caller starts a replacement and
        # `start` overwrites the record, so removing it here would only create
        # a window in which the app has no record at all.
        return None
    return record.running()


async def stop(
    config: ControlPlaneConfig, app_id: str, generation: int | None = None
) -> None:
    """Stop one generation of this app, or all of them.

    Local only, and it trusts a recorded pid, which is fine on a laptop and is
    one of the reasons this module does not exist in the cloud.
    """
    wanted = (
        await generations(config, app_id) if generation is None else [generation]
    )
    for one in wanted:
        await _stop_one(_record_path(config, app_id, one))


async def reap(
    config: ControlPlaneConfig, app_id: str, *, keep: Collection[int]
) -> list[int]:
    """Stop every generation of this app except the ones still wanted.

    This is the second half of the claim in `control_plane/generations.py`. The
    first half — a stale worker cannot publish a route — makes its process
    harmless; this is what makes it temporary. `keep` comes from the database,
    so a process whose generation nothing in the database points at goes,
    whether it is a replaced version or a stale worker's orphan.

    A *set*, because there are two reasons a generation is wanted and the
    reconciler has to honour both: it has the traffic, or some worker is still
    working on it. A reaper that only knew the first would kill a candidate
    version out from under the cutover that was watching it. An empty `keep`
    means nothing here is wanted, so all of it goes.
    """
    reaped = [one for one in await generations(config, app_id) if one not in keep]
    for one in reaped:
        await _stop_one(_record_path(config, app_id, one))
    return reaped


async def kill(config: ControlPlaneConfig, app_id: str, generation: int) -> None:
    """Stop both halves without asking, and without tidying up afterwards.

    Nothing in a deploy ever wants this: `stop` and `reap` ask politely,
    because uvicorn answers SIGTERM by finishing the requests it is holding and
    that is half of what makes a cutover lossless. This exists for the two
    places that need a failure nobody arranged politely — the measurement
    harness's control case, and the chaos suite — and it lives here because
    only the runtime knows what there is to kill.

    The record is deliberately left behind. An app killed out of band *is* a
    generation the database still calls live with nothing listening, which is
    the state the reconciler reports as `lost`; deleting the record here would
    be tidying away the very thing under test.
    """
    record = _read_record(config, app_id, generation)
    if record is None:
        return
    # On Windows `os.kill` calls TerminateProcess whatever signal it is handed,
    # so SIGTERM there is already the hard kill this wants.
    hard = getattr(signal, "SIGKILL", signal.SIGTERM)
    for pid in (record.sidecar_pid, record.app_pid):
        try:
            os.kill(int(pid), hard)
        except (OSError, ValueError):
            pass


async def _stop_one(record: Path) -> None:
    """Signal both halves, forget them, and drop the packages they imported.

    The volume goes here because this is the only code that knows both that
    the generation is finished and which deployment's packages it was
    importing. Nowhere earlier may drop it: a deployment that reached `live`
    is terminal as a *job* and is at the same time the one whose packages are
    being imported right now, so they outlive the verdict and go with the
    generation instead.
    """
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
    deployment_id = running.get("deployment_id")
    if deployment_id:
        await container.remove_volume(build.deps_volume(Path(deployment_id)))


async def watcher(
    config: ControlPlaneConfig, app_id: str, generation: int
) -> Watcher:
    """A question about this generation that outlives the record describing it.

    Taken *before* the stop, because `reap` unlinks the record and after that
    there is nothing on this machine that knows which two ports this generation
    held. A port refusing connections is the evidence, not an exit code: once a
    worker has restarted a pid is all that is left of a process it started, and
    on Windows asking whether a pid is alive with `os.kill(pid, 0)` *terminates*
    it. "It has stopped serving" is both checkable and the thing anybody
    actually cares about.

    Both halves, so a sidecar that obeyed its signal cannot vouch for an app
    process that ignored its own.
    """
    record = _read_record(config, app_id, generation)
    ports = [] if record is None else [record.sidecar_port, record.app_port]

    async def answering() -> bool:
        for port in ports:
            if await asyncio.to_thread(listening, port):
                return True
        return False

    return answering


async def start(
    config: ControlPlaneConfig,
    *,
    app_id: str,
    generation: int,
    deployment_id: uuid.UUID | str,
    checkout: Path,
    entrypoint: str | None,
    logs: Path,
) -> Running:
    """Start this generation of the app. Deliberately does not stop the last one.

    Starting is not publishing. Whatever was serving traffic keeps serving it
    until a fence-protected write says otherwise, so this cannot be the thing
    that stops it: a worker that has been declared dead would otherwise take the
    live version down on its way to being refused. `reap` stops the old one,
    after the database has said which one is old.
    """
    if not entrypoint:
        raise StartupFailed(NO_ENTRYPOINT)

    store = secret_store.store_for(config)
    runtime = store.get(secret_store.secret_name(app_id, secret_store.RUNTIME))
    gate = store.get(secret_store.secret_name(app_id, secret_store.SIDECAR))

    app_port, sidecar_port = _free_port(), _free_port()
    app_url = f"http://127.0.0.1:{app_port}"
    endpoint = f"http://127.0.0.1:{sidecar_port}"

    base = dict(os.environ)
    base["PYTHONUNBUFFERED"] = "1"

    app_log = logs / "app.log"
    app_env = {
        **base,
        # The app's own settings, which stand in for the `.env` we refuse to
        # copy. Deliberately first: everything below is ours and wins, so a
        # stored name that should never have been accepted cannot take effect.
        # `appenv.safe` has already dropped those, which makes this belt and
        # braces rather than the only guard.
        **appenv.safe(runtime.get("env")),
        # Bootstrap first: `sitecustomize` has to be found before anything the
        # app itself might ship. The repo root is absent on purpose.
        "PYTHONPATH": PATH_SEP.join([str(BOOTSTRAP), str(SHIM_PKG), str(checkout)]),
        # Both spellings of the one DSN, which is the runtime role and nothing
        # else. `DATABASE_URL` is what an app built by an AI will read without
        # being told to, and handing it over is safe because the shim hooks
        # SQLAlchemy's `Engine` class: an engine the app builds itself still
        # sets the identity on every transaction.
        "DATABASE_URL": runtime["dsn"],
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
        # Follows the scheme the console is reached over. A `Secure` cookie
        # over plain HTTP is never sent back, so nobody could stay logged in;
        # not marking it where there *is* TLS would be the other mistake.
        "VD_COOKIE_SECURE": "1" if config.cookies_secure else "0",
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

    record = _Record(
        app_id=app_id,
        endpoint=endpoint,
        upstream=f"127.0.0.1:{sidecar_port}",
        app_port=app_port,
        sidecar_port=sidecar_port,
        app_pid=app_process.pid,
        sidecar_pid=sidecar_process.pid,
        generation=generation,
        deployment_id=str(deployment_id),
    )
    path = _record_path(config, app_id, generation)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(record)), encoding="utf-8")
    return record.running()
