"""Live, in containers: the app and its gate, in one network namespace. Readme 16.

    browser -> Caddy -> gate :8080 -> app :3000 -> Postgres

The gate container owns the namespace and the app container **joins** it
(`--network container:<gate>`). That one flag is the whole difference between
this module and `local`, and it is what turns section 16's "the only route to
the app passes through its gate" from a property of configuration into a
property of the kernel:

  - the app has no interface of its own, so there is no address to reach it at;
  - it has no published port, so the host cannot reach it either;
  - it has no name on any network, so nothing can look it up.

`sidecar/config.py`'s documented `VD_UPSTREAM` default of `http://127.0.0.1:3000`
becomes literally true: inside that one namespace, loopback *is* the app. This
is also the Docker spelling of the two-container ECS task at Readme.md:724.

It closes the other two confessions at the top of `local.py` as well. The app
runs as `vdapp` in an image that contains none of our credentials and none of
our code except the shim, and it runs on the interpreter in that image with
**its own** packages ahead of them — the `vd-deps-<deployment_id>` volume the
build job filled and which, until this module existed, nothing ever read.

**Docker is the register.** There is no file on disk saying what is running.
The three labels of `control_plane/generations.py` go on both containers and
`docker ps --filter label=` answers every question `local`'s JSON record was
invented for — except that it survives a wiped state root, cannot go stale
relative to what it describes, and answers for containers this worker never
started. Which is exactly the case reconciliation exists for. `docker run
--name` is a second enforcer of non-duplication on top of that: two workers
racing to start one generation cannot both win, whatever either believed about
the other.

What this still is not: one kernel, one daemon, and a worker that can reach it.
The isolation here is defence in depth, not a sandbox — the enforcement is in
Postgres, as it has been throughout.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Collection, Mapping

import httpx

from control_plane import appenv
from control_plane import generations as gen
from control_plane import secrets as secret_store
from control_plane.config import REPO_ROOT, ControlPlaneConfig
from worker.pipeline import build, container
from worker.runtimes.base import NO_ENTRYPOINT, Running, StartupFailed, Watcher

# The network the gate joins. Caddy is on it, and reaches the gate by container
# name — which is why the upstream is a name and not an address: `127.0.0.1`
# inside Caddy's container is Caddy.
#
# Docker's *default* bridge would not do: it has no DNS, so a name there
# resolves to nothing. This is the compose project's network, which does — and
# `VD_APP_NETWORK` is here because the project name decides it, so a stack
# brought up under another name has another network.
NETWORK = os.environ.get("VD_APP_NETWORK") or "vibedeploy_default"

# Fixed ports, inside the namespace. They can be fixed precisely because the
# namespace is private to one generation — two generations of one app are two
# namespaces, so blue and green both using 8080 is not a collision.
GATE_PORT = 8080
APP_PORT = 3000

# Where the three mounts land. Our code and the app's checkout are read-only;
# so are the app's packages, because an app has no business editing the
# libraries it was proved with.
SHIM_IN_CONTAINER = "/vd/shim"
SIDECAR_IN_CONTAINER = "/vd/src/sidecar"
APP_IN_CONTAINER = "/vd/app"

APP_DOCKERFILE = REPO_ROOT / "infra" / "local" / "apprt" / "Dockerfile"

IMAGE_FAILED = (
    "The image a deployed app runs in could not be built, so the app was never"
    " started and the version that was already serving still is."
)

START_TIMEOUT = 60.0

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


def _name(app_id: str, generation: int, half: str) -> str:
    """The container name for one half of one generation.

    The generation is in the name because two versions of an app running at
    once is the normal case from here on — it is what a health-gated cutover
    needs — and `docker run --name` is one of the two things stopping a second
    copy of a generation from being started.
    """
    return f"vd-{app_id}-g{generation}-{half}"


def _gate(app_id: str, generation: int) -> str:
    return _name(app_id, generation, "gate")


def _app(app_id: str, generation: int) -> str:
    return _name(app_id, generation, "app")


async def _host_port(gate: str) -> str:
    """The port on this host that reaches the gate, or `""` if none does.

    The gate publishes one, and the app publishes nothing. That asymmetry is
    the design: the worker runs on the host and has to be able to probe the
    health gate, and nothing — not the worker, not a curious person on the box
    — is given a way to the app that does not pass through it.
    """
    port = await container.inspect(
        gate,
        '{{(index (index .NetworkSettings.Ports "%d/tcp") 0).HostPort}}' % GATE_PORT,
    )
    return port or ""


async def _endpoint(config: ControlPlaneConfig, gate: str) -> str:
    """The address *this worker* can probe the gate on, before it has a route.

    Two answers, because the worker is in two different places depending on how
    the platform is installed — see `ControlPlaneConfig.worker_in_container`.

    Containerised, this is the gate's container name on `NETWORK`, which needs
    no published port and so cannot be reached from the host at all. On a
    laptop it is the port the gate publishes on the host's loopback, and `""`
    when the gate is not running: there is then no port that reaches it, and
    inventing one would be a lie the health gate would spend thirty seconds
    discovering.
    """
    if config.worker_in_container:
        return f"http://{gate}:{GATE_PORT}"
    port = await _host_port(gate)
    return f"http://127.0.0.1:{port}" if port else ""


async def apps(config: ControlPlaneConfig) -> list[str]:
    """Every app this machine has containers for.

    Asked of docker rather than of the `apps` table, because the whole point of
    a reconciler is to find what the database has stopped mentioning: an app
    deleted from the dashboard while its containers were up would never appear
    in a sweep driven by the database, and they would go on serving — and
    holding connections to a schema nobody is watching — for as long as the box
    was up.
    """
    del config
    found = await container.ps({gen.APP_LABEL: ""}, show=f'{{{{.Label "{gen.APP_LABEL}"}}}}')
    return sorted({one for one in found if one})


async def generations(config: ControlPlaneConfig, app_id: str) -> list[int]:
    """Every generation of this app with containers on this machine.

    The "actual" half of a desired-versus-actual comparison. Asking the
    database what is running would make that comparison vacuous, which is why
    this reads labels and not rows.
    """
    del config
    found = await container.ps(
        {gen.APP_LABEL: app_id}, show=f'{{{{.Label "{gen.GENERATION_LABEL}"}}}}'
    )
    seen = set()
    for one in found:
        try:
            seen.add(int(one))
        except ValueError:
            continue
    return sorted(seen)


async def read(
    config: ControlPlaneConfig, app_id: str, generation: int
) -> Running | None:
    """Where this generation is, or `None` if there is no such generation here.

    `None` means docker has never heard of it, which is a different answer from
    every other this can give — a generation whose containers are *stopped* is
    still reported, because "live in the database with nothing listening" is
    precisely the state the reconciler has to be able to name.
    """
    gate, app = _gate(app_id, generation), _app(app_id, generation)
    deployment_id = await container.inspect(
        gate, f'{{{{index .Config.Labels "{gen.DEPLOYMENT_LABEL}"}}}}'
    )
    if deployment_id is None:
        return None
    return Running(
        app_id=app_id,
        endpoint=await _endpoint(config, gate),
        upstream=f"{gate}:{GATE_PORT}",
        app_container=app,
        sidecar_container=gate,
        generation=generation,
        deployment_id=deployment_id,
    )


async def alive(config: ControlPlaneConfig, running: Running) -> bool:
    """Is the app itself — not its gate — still there?

    The second half of the health gate, separate because the two fail
    differently: a gate with no app behind it passes its own check and turns
    every request into a 502 that looks like the platform's fault.

    `.State.Running` rather than a port, and strictly better than the port it
    replaces: a port can be answered by whatever bound it after a crash, and a
    container's own run state cannot.
    """
    del config
    return await container.is_running(running.app_container)


async def adopt(
    config: ControlPlaneConfig, app_id: str, generation: int
) -> Running | None:
    """The containers already serving this generation, if they still are.

    What a resumed deployment calls before it starts anything. A worker that
    stopped existing between starting a version and publishing it leaves both
    containers up, and the worker that takes the job over has to find them
    rather than start a second pair.

    Both halves have to be running, because a half-adopted version is worse
    than none.
    """
    running = await read(config, app_id, generation)
    if running is None:
        return None
    both = await container.is_running(
        running.sidecar_container
    ) and await container.is_running(running.app_container)
    # Left in place rather than removed. The caller starts a replacement, and
    # `start` clears a dead container out of the way by name, so removing it
    # here would only widen the window in which nothing is recorded at all.
    return running if both else None


async def stop(
    config: ControlPlaneConfig, app_id: str, generation: int | None = None
) -> None:
    """Stop one generation of this app, or all of them."""
    wanted = await generations(config, app_id) if generation is None else [generation]
    for one in wanted:
        await _stop_one(config, app_id, one)


async def reap(
    config: ControlPlaneConfig, app_id: str, *, keep: Collection[int]
) -> list[int]:
    """Stop every generation of this app except the ones still wanted.

    The second half of the claim in `control_plane/generations.py`. The first
    half — a stale worker cannot publish a route — makes its containers
    harmless; this is what makes them temporary. `keep` comes from the
    database, so a generation nothing in the database points at goes, whether
    it is a replaced version or a stale worker's orphan.

    A *set*, because there are two reasons a generation is wanted and both have
    to be honoured: it has the traffic, or some worker still holds a lease on
    it. A reaper that knew only the first would kill a candidate version out
    from under the cutover that was watching it.
    """
    reaped = [one for one in await generations(config, app_id) if one not in keep]
    for one in reaped:
        await _stop_one(config, app_id, one)
    return reaped


async def kill(config: ControlPlaneConfig, app_id: str, generation: int) -> None:
    """Stop both halves without asking, and without tidying up afterwards.

    Nothing in a deploy wants this: `stop` and `reap` ask politely, because
    uvicorn answers SIGTERM by finishing the requests it is holding and that is
    half of what makes a cutover lossless. This is for the two places that need
    a failure nobody arranged politely — the measurement harness's control
    case, and the chaos suite.

    The containers are deliberately left behind, stopped. A generation the
    database still calls live with nothing listening *is* what `lost` means, so
    removing them here would tidy away the thing under test.
    """
    del config
    await container.kill(_app(app_id, generation))
    await container.kill(_gate(app_id, generation))


async def _keep_logs(folder: Path, names: Mapping[str, str]) -> None:
    """Copy what the containers said onto disk, before `docker rm` forgets it.

    `local` had this for free: a subprocess was started with its stderr already
    pointing at a file. A container's output lives in the daemon and is deleted
    with the container, so the one moment it can be kept is the moment before
    the removal — and the deploy whose app crashed is exactly the deploy whose
    log somebody will want afterwards.

    Best effort on purpose. A log that cannot be written must not be the reason
    a generation stays up.
    """
    for name, filename in names.items():
        try:
            text = await container.logs(name)
        except OSError:
            continue
        if not text:
            continue
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / filename).write_text(text, encoding="utf-8", errors="replace")
        except OSError:
            continue


async def _stop_one(config: ControlPlaneConfig, app_id: str, generation: int) -> None:
    """Drain, stop and remove both halves, and the packages they imported.

    The gate goes first and the app second: the other order would leave the
    gate proxying to nothing, so a request already past the door would end in
    a 502 rather than being refused at it.

    The deps volume goes here because this is the only code that knows both
    that the generation is finished and which deployment's packages it was
    importing — the deployment id is on the container's own label. Nothing else
    may drop it: those packages are being imported *right now* by a container
    that may still be serving traffic, which is the bug this arrangement
    replaced.
    """
    gate, app = _gate(app_id, generation), _app(app_id, generation)
    deployment_id = await container.inspect(
        gate, f'{{{{index .Config.Labels "{gen.DEPLOYMENT_LABEL}"}}}}'
    )
    await container.stop(gate)
    await container.stop(app)
    if deployment_id:
        await _keep_logs(
            config.state_root / "deployments" / deployment_id,
            {app: "app.log", gate: "sidecar.log"},
        )
    await container.remove(app)
    await container.remove(gate)
    if deployment_id:
        await container.remove_volume(build.deps_volume(Path(deployment_id)))


async def watcher(
    config: ControlPlaneConfig, app_id: str, generation: int
) -> Watcher:
    """A question about this generation that outlives the containers answering it.

    Taken *before* the stop, because `docker rm` takes the container's own
    answer away with it: afterwards `inspect` says "no such container", which
    is indistinguishable from a container that was never there. The names are
    captured here and the question is asked of them.

    Both halves, so a gate that obeyed its signal cannot vouch for an app that
    ignored its own.
    """
    del config
    names = [_gate(app_id, generation), _app(app_id, generation)]

    async def answering() -> bool:
        for name in names:
            if await container.is_running(name):
                return True
        return False

    return answering


async def ensure_app_image(config: ControlPlaneConfig, *, log: Path) -> None:
    """The image a deployed app runs in, built if it is not current.

    Here rather than in the runbook: a deploy must not fail on a step somebody
    was supposed to remember.
    """
    await container.ensure_image(
        tag=config.app_image,
        dockerfile=APP_DOCKERFILE,
        log=log,
        failed=IMAGE_FAILED,
    )


def _uvicorn(target: str, host: str, port: int) -> list[str]:
    return [
        "python",
        "-m",
        "uvicorn",
        target,
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]


async def _clear(name: str) -> None:
    """Make the name available if what holds it is dead.

    Not a weakening of the `--name` guard. A *running* container is left alone,
    so `docker run` still fails on it and a second copy of a live generation
    still cannot be started; this only removes the corpse a crashed generation
    left behind, which would otherwise make its own resumption impossible.
    """
    if await container.inspect(name, "{{.Id}}") is None:
        return
    if await container.is_running(name):
        return
    await container.remove(name)


async def _wait_for_app(gate: str, app: str) -> None:
    """Wait until the app is accepting connections, asked from inside.

    Listening is the readiness signal, because we do not own the app's routes
    and anything else would be a convention imposed on an app we did not write.
    The question is asked through the gate's namespace: the app's port exists
    only in there, and publishing it so that the worker could watch it start
    would give away the property the whole arrangement is for.
    """
    probe = [
        "python",
        "-c",
        "import socket,sys;s=socket.socket();s.settimeout(2)"
        f";sys.exit(s.connect_ex(('127.0.0.1',{APP_PORT})))",
    ]
    deadline = asyncio.get_running_loop().time() + START_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        if not await container.is_running(app):
            raise StartupFailed(
                f"The app stopped while starting up.\n{await container.logs(app)}"
            )
        code, _ = await container.exec_in(gate, probe)
        if code == 0:
            return
        await asyncio.sleep(0.2)
    raise StartupFailed(
        f"The app never started listening.\n{await container.logs(app)}"
    )


async def _wait_for_gate(gate: str, url: str) -> None:
    deadline = asyncio.get_running_loop().time() + START_TIMEOUT
    async with httpx.AsyncClient(timeout=2.0) as client:
        while asyncio.get_running_loop().time() < deadline:
            if not await container.is_running(gate):
                raise StartupFailed(
                    "The login gate stopped while starting up.\n"
                    f"{await container.logs(gate)}"
                )
            try:
                if (await client.get(url)).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    raise StartupFailed(
        f"The login gate never became healthy.\n{await container.logs(gate)}"
    )


def _app_env(
    app_id: str, runtime: Mapping[str, object], identity_key: str
) -> dict[str, str]:
    dsn = str(runtime["dsn"])
    return {
        # The app's own settings, which stand in for the `.env` we refuse to
        # copy. Deliberately first: everything below is ours and wins, so a
        # stored name that should never have been accepted cannot take effect.
        # `appenv.safe` has already dropped those, which makes this belt and
        # braces rather than the only guard — and it is why `DOCKER*` is
        # reserved, since this environment is handed to our own `docker`.
        **appenv.safe(runtime.get("env")),  # type: ignore[arg-type]
        # Bootstrap first, so `sitecustomize` is found before anything the app
        # itself might ship; then the shim package, so an app cannot shadow
        # `vibedeploy_shim` by installing something of that name; then the
        # app's own packages, which is what puts them ahead of the image's
        # site-packages; then the checkout. Nothing of ours but the shim is
        # mounted, so there is no control plane here to import.
        "PYTHONPATH": ":".join(
            [
                f"{SHIM_IN_CONTAINER}/bootstrap",
                SHIM_IN_CONTAINER,
                build.DEPS_IN_CONTAINER,
                APP_IN_CONTAINER,
            ]
        ),
        # Both spellings of the one DSN, which is the runtime role and nothing
        # else. `DATABASE_URL` is what an app built by an AI reads without
        # being told to, and handing it over is safe because the shim hooks
        # SQLAlchemy's `Engine` class: an engine the app builds itself still
        # sets the identity on every transaction.
        "DATABASE_URL": container.reachable_from_container(dsn),
        "VD_DATABASE_URL": container.reachable_from_container(
            dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
        ),
        "VD_IDENTITY_KEY": identity_key,
        "VD_APP_ID": app_id,
    }


def _gate_env(
    config: ControlPlaneConfig, app_id: str, gate: Mapping[str, str]
) -> dict[str, str]:
    return {
        # The gate signs the identity header with the same code the shim
        # verifies it with, so the shim package is on its path too. The
        # bootstrap directory is not: nothing of ours wants `sitecustomize`.
        "PYTHONPATH": f"{SHIM_IN_CONTAINER}:/vd/src",
        "VD_APP_ID": app_id,
        "VD_IDENTITY_KEY": gate["identity_key"],
        "VD_SESSION_KEY": gate["session_key"],
        "VD_CONTROL_PLANE_URL": container.reachable_from_container(config.public_url),
        "VD_SIDECAR_API_KEY": config.sidecar_api_key,
        # True rather than merely documented, now that the app is in this
        # container's own network namespace.
        "VD_UPSTREAM": f"http://127.0.0.1:{APP_PORT}",
        # Follows the scheme the console is reached over. A `Secure` cookie
        # over plain HTTP is never sent back, so nobody could stay logged in;
        # not marking it where there *is* TLS would be the other mistake.
        "VD_COOKIE_SECURE": "1" if config.cookies_secure else "0",
    }


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

    Starting is not publishing. Whatever is serving traffic keeps serving it
    until a fence-protected write says otherwise, so this cannot be the thing
    that stops it: a worker already declared dead would otherwise take the live
    version down on its way to being refused. `reap` stops the old one, after
    the database has said which one is old.

    The gate is started first because it owns the network namespace the app
    then joins, and it is torn down with the app if either fails to come up —
    an app with no gate in front of it must never be left listening, and a gate
    with nothing behind it must never be left answering.
    """
    if not entrypoint:
        raise StartupFailed(NO_ENTRYPOINT)

    await ensure_app_image(config, log=logs / "image.log")

    store = secret_store.store_for(config)
    runtime = store.get(secret_store.secret_name(app_id, secret_store.RUNTIME))
    gate_secret = store.get(secret_store.secret_name(app_id, secret_store.SIDECAR))

    gate, app = _gate(app_id, generation), _app(app_id, generation)
    labels = gen.labels(
        app_id=app_id, generation=generation, deployment_id=str(deployment_id)
    )
    await _clear(gate)
    await _clear(app)

    try:
        await container.start_detached(
            image=config.app_image,
            command=_uvicorn("sidecar.server:app", "0.0.0.0", GATE_PORT),
            name=gate,
            mounts={
                REPO_ROOT / "sidecar": SIDECAR_IN_CONTAINER,
                REPO_ROOT / "shims" / "python": SHIM_IN_CONTAINER,
            },
            env=_gate_env(config, app_id, gate_secret),
            network=NETWORK,
            labels=labels,
            # Loopback, and port 0 so the host chooses — the worker's only way
            # to probe the health gate when it is a process on this host. The
            # app half gets no such line, which is the point.
            #
            # Nothing at all when the worker is containerised: it is on
            # `NETWORK` too and probes by container name, so a published port
            # would be an address into the gate that exists for no reader.
            publish=(
                () if config.worker_in_container else [f"127.0.0.1:0:{GATE_PORT}"]
            ),
        )
        await container.start_detached(
            image=config.app_image,
            command=_uvicorn(entrypoint, "127.0.0.1", APP_PORT),
            name=app,
            mounts={
                REPO_ROOT / "shims" / "python": SHIM_IN_CONTAINER,
                checkout: APP_IN_CONTAINER,
                build.deps_volume(Path(str(deployment_id))): build.DEPS_IN_CONTAINER,
            },
            env=_app_env(app_id, runtime, gate_secret["identity_key"]),
            # The flag this module exists for. No interface, no published port,
            # no name on any network: `127.0.0.1` here is the gate.
            network=f"container:{gate}",
            labels=labels,
        )
        await _wait_for_app(gate, app)
        endpoint = await _endpoint(config, gate)
        if not endpoint:
            raise StartupFailed("The login gate published no port to reach it on.")
        await _wait_for_gate(gate, f"{endpoint}/__vd/health")
    except BaseException:
        await _keep_logs(logs, {app: "app.log", gate: "sidecar.log"})
        await container.remove(app)
        await container.remove(gate)
        raise

    return Running(
        app_id=app_id,
        endpoint=endpoint,
        upstream=f"{gate}:{GATE_PORT}",
        app_container=app,
        sidecar_container=gate,
        generation=generation,
        deployment_id=str(deployment_id),
    )
