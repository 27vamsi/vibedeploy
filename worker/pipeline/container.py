"""Running code we have never read, somewhere it cannot reach us.

Readme.md section 3 rule 11: an app's own migrations run in CodeBuild or a
one-off ECS task, never in the worker. Locally that is `docker run --rm`, which
is the weakest member of that family and is honest about it — it is a container
on the same kernel, not a VM in another account — but it is the difference
between app code sharing the worker's filesystem, environment and secrets and
app code sharing none of them.

Three rules the argv builder exists to keep:

  - **no secret in `argv`.** `argv` is readable by every other process on the
    box, so a value is passed by handing `docker` the *name* of a variable
    already in our own environment, or in a 0600 file we mount;
  - **mounts are read-only** unless the caller says otherwise, so app code
    cannot edit the checkout it was built from and change what the second pass
    sees;
  - **never `--network host`.** Host networking would put the container on the
    loopback interface the control plane, the gateway and the customer database
    all listen on.

`host.docker.internal` is the one concession to locality. Inside a container
`127.0.0.1` is the container, so an address that means "this laptop" has to be
rewritten or the build job cannot report home and cannot reach its throwaway
database. In AWS both are real hostnames and `reachable_from_container` leaves
them alone.

One local caveat: on Docker Desktop that alias reaches a server bound to the
host's `127.0.0.1`, which is how everything in `infra/local/RUNBOOK.md` is
started. On a Linux host `--add-host ...:host-gateway` gives the alias the
bridge gateway instead, which does not, so a Linux box has to bind those
services on `0.0.0.0`.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

DOCKER = os.environ.get("VD_DOCKER", "docker")

# Generous: `verify` migrates, seeds and then attacks a whole schema twice. A
# build still running after this is killed and treated as a failure, because a
# build job that never answered proved nothing.
DEFAULT_TIMEOUT = 15 * 60

# Docker Desktop resolves this to the host. `--add-host` below makes it resolve
# on Linux too, so the same argv works on a developer's laptop and in CI.
DEFAULT_HOST_ALIAS = "host.docker.internal"

# What "this laptop" is rewritten to. The default only works where the host's
# loopback-bound services are reachable from the bridge gateway, which is a
# Docker Desktop property and not a Docker one: on a Linux box the stack's
# services have names of their own on the compose network, and the gateway
# address reaches nothing that is listening. There, `VD_CONTAINER_HOST` names
# the address that does — and when it is set, the alias is a real name that
# already resolves, so no `--add-host` is wanted either.
HOST_ALIAS = os.environ.get("VD_CONTAINER_HOST") or DEFAULT_HOST_ALIAS

LOOPBACK = ("127.0.0.1", "localhost", "[::1]", "0.0.0.0")


@dataclass(frozen=True)
class Outcome:
    returncode: int
    log: Path

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self, limit: int = 2000) -> str:
        """The end of the log, for `jobs.last_error`.

        Never shown to a builder: this is an operator's breadcrumb, and it can
        contain anything the app's migrations decided to print.
        """
        try:
            text = self.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-limit:]


@dataclass(frozen=True)
class Volume:
    """A mount source `docker` manages itself, named instead of a host path.

    One mount needs this and the reason is measurable rather than tidy. The
    app's own packages are a writable mount, and on Docker Desktop a writable
    bind mount is a filesystem in another VM reached one file at a time: the
    same `pip install` takes **six minutes** into a bind mount on the host and
    **two** into a volume, and the six-minute one has already run a build job
    into its timeout. Twenty thousand small files is the worst possible shape
    for that boundary.

    Nothing outside the container ever reads these packages — the worker does
    not import them, and must not — so there was never a reason for them to be
    on the host at all.

    A fresh volume takes its ownership from the directory the image has at that
    path, which is why the Dockerfile creates `/vd/deps` as the build user: a
    volume initialised from nothing would be root's and the container is not.
    """

    name: str


class DockerMissing(RuntimeError):
    """There is nowhere safe to run the app's code, so it does not run."""


NO_DOCKER = (
    "Docker is not running, and an app's own migrations are only ever run"
    " inside a container. Start Docker Desktop and deploy again."
)


def available() -> bool:
    return shutil.which(DOCKER) is not None


def host_path(path: Path | str) -> str:
    """A path in the form `docker` wants it.

    On Windows `docker` takes `C:\\Users\\...` happily, but a path that reached
    us through a shell can arrive as `/c/Users/...`, which it does not. Resolving
    settles it either way.
    """
    return str(Path(path).resolve())


def reachable_from_container(address: str) -> str:
    """Rewrite "this laptop" so that it still means this laptop inside.

    Three shapes, because three kinds of address need it: a DSN's `@host`, a
    URL's `//host`, and a bare `host:port` — which is what a proxy upstream is.
    Caddy runs in a container too, so a route dialling `127.0.0.1:54321` would
    dial Caddy itself and get nothing.
    """
    for loopback in LOOPBACK:
        if f"@{loopback}" in address:
            return address.replace(f"@{loopback}", f"@{HOST_ALIAS}", 1)
        if f"//{loopback}" in address:
            return address.replace(f"//{loopback}", f"//{HOST_ALIAS}", 1)
        if address.startswith(f"{loopback}:"):
            return HOST_ALIAS + address[len(loopback) :]
    return address


def mount_source(source: Path | str | Volume) -> str:
    """What goes on the left of a `--volume`: a host path, or a volume's name."""
    return source.name if isinstance(source, Volume) else host_path(source)


def docker_run_argv(
    *,
    image: str,
    command: Sequence[str],
    mounts: Mapping[Path | str | Volume, str],
    env: Mapping[str, str],
    network: str = "bridge",
    writable: Sequence[str] = (),
    labels: Mapping[str, str] | None = None,
    rm: bool = True,
    name: str | None = None,
    publish: Sequence[str] = (),
    detach: bool = False,
) -> list[str]:
    """The exact command line, as a value, so a test can read it.

    `env` maps the name the container should see to the name it is already under
    in our environment, and only the names travel here. The values are handed to
    `docker` by `run`, in its environment, where no other process can read them.

    `labels` is how a container is traced back to the deployment that started
    it. For a build job `--rm` means an ordinary run leaves nothing to trace,
    but the runs worth finding are the ones that did not end ordinarily — a
    worker killed mid-build leaves its container behind, and without labels the
    only way to know whose it is would be to ask the worker that is gone.

    `rm` is a keyword and not a constant because a *served* app must not
    evaporate when it crashes. "A generation that is live and has nothing
    listening" is the whole of what `lost` means, and a container that removed
    itself on exit makes that indistinguishable from "a generation nobody ever
    started". So a build job keeps `--rm` and an app does not.

    `name` is the second enforcer of non-duplication: `docker run --name` fails
    outright on a name that already exists, so two workers racing to start the
    same generation cannot both win, whatever either of them believed about the
    other.
    """
    argv = [DOCKER, "run", "--network", network]
    if rm:
        argv.append("--rm")
    if detach:
        argv.append("--detach")
    if name:
        argv += ["--name", name]

    for label, value in (labels or {}).items():
        argv += ["--label", f"{label}={value}"]

    for spec in publish:
        argv += ["--publish", spec]

    # So that `host.docker.internal` resolves on a Linux host as well, where
    # Docker does not provide it. Two reasons to leave it out:
    #
    #   - a container joining another's network namespace has no resolver of
    #     its own to add to, and `docker` rejects the combination outright
    #     rather than ignoring it;
    #   - an overridden alias is already a real name on a real network, so
    #     pointing it at the bridge gateway would break what it names.
    if not network.startswith("container:") and HOST_ALIAS == DEFAULT_HOST_ALIAS:
        argv += ["--add-host", f"{HOST_ALIAS}:host-gateway"]

    for source, target in mounts.items():
        suffix = "" if target in writable else ":ro"
        argv += ["--volume", f"{mount_source(source)}:{target}{suffix}"]

    for variable in env:
        # Name only. `docker` reads the value out of our environment itself.
        argv += ["--env", variable]

    return argv + [image, *command]


async def run(
    *,
    image: str,
    command: Sequence[str],
    log: Path,
    mounts: Mapping[Path | str | Volume, str] | None = None,
    env: Mapping[str, str] | None = None,
    network: str = "bridge",
    writable: Sequence[str] = (),
    labels: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> Outcome:
    """Start the container and wait for it.

    An exit code and a log file is all any caller above this ever wanted, which
    is what makes swapping this for a one-off ECS task in M8 a change to one
    module.
    """
    if not available():
        raise DockerMissing(NO_DOCKER)

    env = dict(env or {})
    argv = docker_run_argv(
        image=image,
        command=command,
        mounts=mounts or {},
        env=env,
        network=network,
        writable=writable,
        labels=labels,
    )

    # The values, in our own environment, where they are not in anybody's
    # `argv`. `docker run --env NAME` with no `=` copies it across.
    child = {**os.environ, **env}

    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as handle:
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=child,
            stdout=handle,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout)
        except asyncio.TimeoutError:
            # `--rm` cleans the container up once the client is gone, so killing
            # the client is enough to leave nothing behind.
            process.kill()
            await process.wait()
            returncode = -1
    return Outcome(returncode=returncode, log=log)


async def build_image(
    *,
    tag: str,
    dockerfile: Path,
    context: Path,
    log: Path,
    labels: Mapping[str, str] | None = None,
) -> Outcome:
    """Build the image the build job runs in. Cached by `docker` between runs."""
    if not available():
        raise DockerMissing(NO_DOCKER)

    argv = [DOCKER, "build", "--tag", tag]
    for name, value in (labels or {}).items():
        argv += ["--label", f"{name}={value}"]
    argv += ["--file", host_path(dockerfile), host_path(context)]

    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as handle:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=handle, stderr=asyncio.subprocess.STDOUT
        )
        returncode = await process.wait()
    return Outcome(returncode=returncode, log=log)


async def ensure_volume(volume: Volume, labels: Mapping[str, str] | None = None) -> None:
    """Create the volume up front, so that it can be labelled.

    `docker run --volume name:/path` creates a missing volume by itself, and
    the one it creates has no labels on it, which is exactly the resource that
    is hardest to account for later: a volume outlives the container that made
    it, by design, and `docker volume ls` on a laptop that has been deploying
    all week is otherwise a list of anonymous disk usage.

    Idempotent — `volume create` on a name that exists is a no-op that returns
    the name — so this is safe to call on every pass of a deployment.
    """
    if not available():
        return
    argv = [DOCKER, "volume", "create"]
    for name, value in (labels or {}).items():
        argv += ["--label", f"{name}={value}"]
    process = await asyncio.create_subprocess_exec(
        *argv,
        volume.name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.wait()


async def remove_volume(volume: Volume) -> None:
    """Throw away a deployment's packages. Never raises.

    Best effort on purpose: a volume left behind costs disk and nothing else,
    and a deploy that reached a verdict must not be reported as failed because
    the tidying up did not work.
    """
    if not available():
        return
    process = await asyncio.create_subprocess_exec(
        DOCKER,
        "volume",
        "rm",
        "--force",
        volume.name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.wait()


# ---------------------------------------------------------------------------
# Containers that outlive the call that started them
#
# Everything above runs app code to completion and reports an exit code. A
# served app is the other shape: it is started and then left, and from then on
# the question is not "what did it return" but "is it still there".
#
# Which is the reason these exist at all, rather than the worker keeping a file
# that says what it started. `docker ps --filter label=` is a register that
# cannot go stale relative to what it describes, survives a wiped state root,
# and answers for containers this worker never started — which is exactly the
# case reconciliation is for.
# ---------------------------------------------------------------------------


class ContainerFailed(RuntimeError):
    """`docker` refused to start it, and said why.

    Carries docker's own words because the two failures that matter here are
    "that name is taken" and "no such image", and both are unambiguous in the
    client's output and guesswork without it.
    """


async def _capture(*argv: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await process.communicate()
    return process.returncode or 0, out.decode(errors="replace").strip()


async def start_detached(
    *,
    image: str,
    command: Sequence[str],
    name: str,
    mounts: Mapping[Path | str | Volume, str] | None = None,
    env: Mapping[str, str] | None = None,
    network: str = "bridge",
    writable: Sequence[str] = (),
    labels: Mapping[str, str] | None = None,
    publish: Sequence[str] = (),
) -> str:
    """Start a long-lived container and return its id.

    Not `--rm`: see `docker_run_argv`. Raises rather than returning a flag,
    because every caller's next line assumes there is something running, and
    the two likely failures — a name already taken, a missing image — are both
    things the deploy must stop on rather than work around.
    """
    if not available():
        raise DockerMissing(NO_DOCKER)

    env = dict(env or {})
    argv = docker_run_argv(
        image=image,
        command=command,
        mounts=mounts or {},
        env=env,
        network=network,
        writable=writable,
        labels=labels,
        rm=False,
        name=name,
        publish=publish,
        detach=True,
    )
    process = await asyncio.create_subprocess_exec(
        *argv,
        env={**os.environ, **env},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await process.communicate()
    said = out.decode(errors="replace").strip()
    if process.returncode != 0:
        raise ContainerFailed(f"{name}: {said}")
    return said.splitlines()[-1] if said else name


async def inspect(name: str, template: str) -> str | None:
    """One field off a container, or `None` if there is no such container.

    `None` is "not there", which is a different answer from every value the
    template could produce, and the distinction is load-bearing: a generation
    whose container is gone is not the same as one whose container is stopped.
    """
    if not available():
        return None
    code, said = await _capture(DOCKER, "inspect", "--format", template, name)
    return None if code != 0 else said


async def is_running(name: str) -> bool:
    """Whether the container exists *and* its main process is alive.

    This is the evidence the health gate's second half rests on, and it is
    strictly better than the port check it replaces: a port can be answered by
    something else that bound it after a crash, and `.State.Running` cannot.
    """
    return (await inspect(name, "{{.State.Running}}")) == "true"


async def ps(
    labels: Mapping[str, str],
    *,
    running_only: bool = False,
    show: str = "{{.Names}}",
) -> list[str]:
    """One field off every container carrying all of these labels.

    The register. `--filter` is repeated rather than joined because docker ANDs
    repeated filters of the same kind, which is the semantics wanted: an
    unlabelled container, or one labelled for another app, must not appear.

    A label whose wanted value is empty filters on the label *existing*, which
    is how "every app with anything running here" is asked — the question a
    reconciler starts from, since the whole point of it is to find what the
    database has stopped mentioning.

    `show` exists so that the answer can be a label rather than a name. Reading
    `vd.app` back out of docker is better than parsing it out of a container
    name: the name is a convention of one module and the label is the thing
    every filter here already agrees on.
    """
    if not available():
        return []
    argv = [DOCKER, "ps", "--format", show]
    if not running_only:
        argv.append("--all")
    for label, value in labels.items():
        argv += ["--filter", f"label={label}={value}" if value else f"label={label}"]
    code, said = await _capture(*argv)
    if code != 0:
        return []
    return [line.strip() for line in said.splitlines() if line.strip()]


async def stop(name: str, *, timeout: float = 10.0) -> bool:
    """Ask it to stop, and say whether it did.

    `docker stop` sends SIGTERM, waits, then sends SIGKILL, which is the drain
    the cutover wants and the reason the worker no longer has to implement one.
    Returns False when docker could not stop it — a container that is still
    there after this has to be *reported*, not assumed gone.
    """
    if not available():
        return False
    code, _ = await _capture(DOCKER, "stop", "--time", str(int(timeout)), name)
    return code == 0


async def kill(name: str) -> None:
    """Stop it without asking. Never raises.

    Separate from `stop` because the difference is the whole of what a drain
    is: `docker stop` gives the process its SIGTERM and time to finish the
    requests it is holding, and this does not. Nothing in a deploy wants it —
    it is here for the measurement harness's control case and for the chaos
    suite, both of which need a failure nobody arranged politely.
    """
    if not available():
        return
    await _capture(DOCKER, "kill", name)


async def exec_in(name: str, command: Sequence[str]) -> tuple[int, str]:
    """Run something inside a container that is already running.

    Which is not a thing a deploy would normally want, and is unavoidable for
    exactly one question: whether the app half is listening yet. The app shares
    the gate's network namespace and publishes nothing, so its port exists only
    inside — and publishing it in order to watch it start would give away the
    property the arrangement is for. So the question is asked from in there.
    """
    if not available():
        return 1, NO_DOCKER
    return await _capture(DOCKER, "exec", name, *command)


async def remove(name: str) -> None:
    """Throw the container away. Never raises.

    Best effort for the same reason `remove_volume` is: tidying up that failed
    costs disk, and must not turn a finished deploy into a failed one.
    """
    if not available():
        return
    await _capture(DOCKER, "rm", "--force", name)


async def logs(name: str, *, tail: int = 200) -> str:
    """The end of a container's output, for `jobs.last_error` and for a person.

    Never shown to a builder: it is whatever the app decided to print.
    """
    if not available():
        return ""
    _, said = await _capture(DOCKER, "logs", "--tail", str(tail), name)
    return said


DOCKERFILE_LABEL = "vd.dockerfile"


async def ensure_image(*, tag: str, dockerfile: Path, log: Path, failed: str) -> None:
    """Build an image of ours once, and reuse it until it is out of date.

    Here rather than in the runbook, because a deploy must not fail on a step
    somebody was supposed to have remembered. Up to date is decided by a label
    carrying the Dockerfile's digest, not by the tag existing: adding a package
    an app needs would otherwise take effect only on machines that happened not
    to have the old image, which is the worst of both. `docker build` is
    layer-cached, so an unchanged Dockerfile costs one inspect.

    `failed` is the caller's words for what did not happen as a result, because
    "docker build failed" is true of both of our images and tells a builder
    nothing about which half of their deploy stopped.
    """
    wanted = hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:16]
    if await image_label(tag, DOCKERFILE_LABEL) == wanted:
        return
    outcome = await build_image(
        tag=tag,
        dockerfile=dockerfile,
        context=dockerfile.parent,
        log=log,
        labels={DOCKERFILE_LABEL: wanted},
    )
    if not outcome.ok:
        raise DockerMissing(failed)


async def image_label(tag: str, label: str) -> str | None:
    """One label off a local image, or `None` if there is no such image.

    How the caller tells "already built" from "built from something else".
    Checked for `docker` here as well, because this is the first of these a
    deploy reaches: without it a missing `docker` arrives as a bare
    `FileNotFoundError` and the builder is told "something went wrong on our
    side" instead of which program to start.

    The quotes are double deliberately. A Go template's string literal is
    double-quoted and `'...'` is a *rune* literal, so Python's `!r` produced
    `malformed character constant` — docker exited 64, this returned `None`,
    and `ensure_image` therefore rebuilt on every single call. Cheap, because
    `docker build` is layer-cached, and quietly untrue: "up to date is decided
    by a label" was decided by nothing at all.
    """
    if not available():
        raise DockerMissing(NO_DOCKER)

    process = await asyncio.create_subprocess_exec(
        DOCKER,
        "image",
        "inspect",
        "--format",
        '{{index .Config.Labels "%s"}}' % label,
        tag,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await process.communicate()
    if process.returncode != 0:
        return None
    return out.decode(errors="replace").strip()
