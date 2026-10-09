"""Starting the untrusted half, and waiting for it. Readme.md section 3 rule 11.

The worker holds the customer database's admin credentials. An app's own
migrations must therefore never execute in it, not even behind a `try`. So
everything that touches app code is started here as a **separate process** and
the worker's only contact with it is an exit code and a log file.

Locally that is a subprocess against a throwaway schema, which is the weakest
isolation in the family and is honest about it: M8 replaces `launch` with
CodeBuild and a one-off ECS task, and nothing above this file changes.

Two rules the callers depend on:

  - secrets go in a 0600 spec file or the environment, never in `argv`, because
    `argv` is readable by every process on the box;
  - a build that is still running after `timeout` is killed and treated as a
    failure. A build job that never answered proved nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from control_plane.config import REPO_ROOT

# Generous: `verify` migrates, seeds and then attacks a whole schema twice.
DEFAULT_TIMEOUT = 15 * 60

TIMED_OUT = (
    "The build did not finish in time, so nothing about this deployment was"
    " proved and nothing was deployed."
)


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


def child_env(**extra: str) -> dict[str, str]:
    """The environment for a child process, with the repo importable.

    `PYTHONPATH` is set rather than relying on the working directory so that the
    child is importable the same way whether it is started from a checkout or
    from an installed package.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    env.update(extra)
    return env


async def launch(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    log: Path,
    cwd: Path = REPO_ROOT,
    timeout: float = DEFAULT_TIMEOUT,
) -> Outcome:
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as handle:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=dict(env),
            stdout=handle,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            returncode = await asyncio.wait_for(process.wait(), timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            returncode = -1
    return Outcome(returncode=returncode, log=log)


def write_spec(path: Path, spec: Mapping[str, Any]) -> Path:
    """The build job's instructions, including its one-time token, at 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(dict(spec), handle)
    return path


async def run_build_job(spec: Mapping[str, Any], *, workdir: Path) -> Outcome:
    """Run one phase of `buildjob.run` and wait for it.

    Its verdict does not come back through this function. The build job reports
    home itself, over HTTP, with its one-time token (contract 7.4), because in
    the cloud it runs somewhere the worker cannot reach into. The exit code here
    only says whether it got that far.
    """
    spec_path = write_spec(workdir / f"{spec['phase']}-spec.json", spec)
    return await launch(
        [sys.executable, "-m", "buildjob.run", "--spec", str(spec_path)],
        env=child_env(),
        log=workdir / f"{spec['phase']}.log",
    )
