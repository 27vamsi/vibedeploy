"""Starting the untrusted half, and waiting for it. Readme.md section 3 rule 11.

The worker holds the customer database's admin credentials. An app's own
migrations must therefore never execute in it, not even behind a `try`, so
everything that touches app code is started in a **container** and the worker's
only contact with it is an exit code and a log file.

Two things are rewritten on the way in, and both are why this module exists
rather than the caller doing it:

  - **paths.** The container does not share the worker's filesystem, so the
    checkout, our build-job code and the spec are mounted at fixed places and
    the spec is edited to name those instead;
  - **addresses.** Inside a container `127.0.0.1` is the container. The
    throwaway database and the way home both have to be rewritten or the build
    job reaches neither.

The caller says *what* to build. This file decides *where*, which is the one
thing M8 changes when it swaps `docker run` for CodeBuild and a one-off ECS
task.

Secrets go in a 0600 spec file, never in `argv`, because `argv` is readable by
every process on the box.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from control_plane.config import REPO_ROOT, ControlPlaneConfig
from worker.pipeline import container
from worker.pipeline.container import DEFAULT_TIMEOUT, Outcome

BUILD_DOCKERFILE = REPO_ROOT / "infra" / "local" / "buildjob" / "Dockerfile"

# Fixed, because the spec has to name them and nothing else about the container
# is the caller's business.
REPO_IN_CONTAINER = "/vd/repo"
SPEC_IN_CONTAINER = "/vd/spec.json"
DEPS_IN_CONTAINER = "/vd/deps"

IMAGE_FAILED = (
    "The image an app's migrations run in could not be built, so they were not"
    " run and nothing was proved."
)

# The network the build job gets. The default bridge is right on a laptop: the
# throwaway database is published on the host's loopback and Docker Desktop's
# `host.docker.internal` reaches it, which is what the address rewriting above
# is for.
#
# It is wrong on a Linux box, where that rewriting has nothing to rewrite *to*
# — a loopback-bound port is not reachable from the bridge gateway. There the
# throwaway database is a compose service with a name, so the build job is put
# on the network where that name resolves and the DSN names it directly.
#
# Deliberately a separate variable from `VD_APP_NETWORK`: a deployed app's
# network decides who can reach the app, and this one decides what an app's
# own untrusted migrations can reach. They should be able to differ.
BUILD_NETWORK = os.environ.get("VD_BUILD_NETWORK") or "bridge"

__all__ = [
    "DEFAULT_TIMEOUT",
    "Outcome",
    "deps_volume",
    "discard_deps",
    "ensure_build_image",
    "run_build_job",
    "write_spec",
]


def deps_volume(workdir: Path) -> container.Volume:
    """Where the app's own packages go: a volume of its own, and nothing else.

    Of its own rather than anywhere near the workdir, because the workdir holds
    `plan-spec.json` and `verify-spec.json` — and those carry the build job's
    one-time token. `pip install` runs the app's own setup code, so the one
    writable thing it is given has to contain nothing of ours.

    Named after the deployment, which is what the workdir is named after, so all
    three passes of one deploy install into one place and pay for it once. A
    volume rather than a directory on the host for a reason worth measuring:
    `container.Volume` has the numbers.
    """
    return container.Volume(f"vd-deps-{Path(workdir).name}")


async def discard_deps(workdir: Path) -> None:
    """Throw away the packages a finished deployment installed.

    Not kept as a cache between deploys. Two deploys of two different apps
    sharing a directory that one app's `pip install` could write to would be a
    channel from one customer's build into another's, and a cache shared by one
    app's own deploys would make a build depend on what the last one left.
    """
    await container.remove_volume(deps_volume(workdir))


def write_spec(path: Path, spec: Mapping[str, Any]) -> Path:
    """The build job's instructions, including its one-time token, at 0640.

    Group-readable and not world-readable, because the two processes are two
    containers running as two different users: the worker writes this as uid
    10000 and the build job reads it as uid 10001, whose supplementary group is
    the worker's. At 0600 — which is what this was — no build could read its own
    spec, and the only reason that was not obvious on a laptop is that Docker
    Desktop's bind mounts have no Unix modes to honour.

    The group is the narrow part: it says the build job may read this file, not
    that it may read anything else of ours. Everything in the state root that is
    not meant for it, app secrets included, stays 0600 (`control_plane.secrets`).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(dict(spec), handle)
    return path


async def ensure_build_image(config: ControlPlaneConfig, *, log: Path) -> None:
    """The image an app's migrations run in, built if it is not current."""
    await container.ensure_image(
        tag=config.build_image,
        dockerfile=BUILD_DOCKERFILE,
        log=log,
        failed=IMAGE_FAILED,
    )


async def run_build_job(
    config: ControlPlaneConfig,
    spec: Mapping[str, Any],
    *,
    workdir: Path,
    labels: Mapping[str, str] | None = None,
) -> Outcome:
    """Run one phase of `buildjob.run`, in a container, and wait for it.

    Its verdict does not come back through this function. The build job reports
    home itself, over HTTP, with its one-time token (contract 7.4), because in
    the cloud it runs somewhere the worker cannot reach into. The exit code here
    only says whether it got that far.

    `labels` go on both Docker resources this creates, the container and the
    packages volume, so that a build killed halfway through leaves something
    that can be traced to an app and a generation rather than two anonymous
    objects in `docker ps -a`.
    """
    inside = {
        **spec,
        "repo_dir": REPO_IN_CONTAINER,
        "deps_dir": DEPS_IN_CONTAINER,
        "scratch_dsn": container.reachable_from_container(spec["scratch_dsn"]),
        "callback_url": container.reachable_from_container(spec["callback_url"]),
    }
    spec_path = write_spec(workdir / f"{spec['phase']}-spec.json", inside)

    await ensure_build_image(config, log=workdir / "image.log")
    await container.ensure_volume(deps_volume(workdir), labels)
    return await container.run(
        image=config.build_image,
        command=["python", "-m", "buildjob.run", "--spec", SPEC_IN_CONTAINER],
        mounts={
            # Our own code, mounted rather than copied in, so that editing the
            # build job does not mean rebuilding the image. Read-only either
            # way: the container has no business changing it.
            REPO_ROOT / "buildjob": "/vd/src/buildjob",
            REPO_ROOT / "kernel": "/vd/src/kernel",
            # The app's checkout, also read-only, so app code cannot alter what
            # the second pass is going to read.
            Path(spec["repo_dir"]): REPO_IN_CONTAINER,
            spec_path: SPEC_IN_CONTAINER,
            # The one writable mount, holding nothing but the app's packages.
            deps_volume(workdir): DEPS_IN_CONTAINER,
        },
        writable=[DEPS_IN_CONTAINER],
        network=BUILD_NETWORK,
        labels=labels,
        log=workdir / f"{spec['phase']}.log",
        timeout=DEFAULT_TIMEOUT,
    )
