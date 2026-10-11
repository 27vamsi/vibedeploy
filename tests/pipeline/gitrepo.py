"""Building a real git repository for a test to deploy from.

Everything is pinned explicitly: identity, branch names, and the absence of any
of the developer's own git configuration. `init.defaultBranch` is whatever the
machine running the suite happens to have set, so no test may assume `main`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping

# A clean room. Without this, a developer's `~/.gitconfig` (signing keys, hooks
# paths, a credential helper) decides whether the suite passes.
ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "vibedeploy tests",
    "GIT_AUTHOR_EMAIL": "tests@localhost",
    "GIT_COMMITTER_NAME": "vibedeploy tests",
    "GIT_COMMITTER_EMAIL": "tests@localhost",
}


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(ENV)
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write(root: Path, files: Mapping[str, str]) -> None:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def make_repo(root: Path, branches: Mapping[str, Mapping[str, str]]) -> Path:
    """A repository with one commit per branch, each branch off the first.

    The first branch listed is the trunk; the rest branch from its commit, which
    is what a repository with feature branches looks like.
    """
    root.mkdir(parents=True)
    git(root, "init", "--quiet")

    names = list(branches)
    trunk = names[0]
    git(root, "checkout", "--quiet", "-b", trunk)
    _write(root, branches[trunk])
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", f"{trunk}")

    for name in names[1:]:
        git(root, "checkout", "--quiet", "-b", name, trunk)
        _write(root, branches[name])
        git(root, "add", "--all")
        git(root, "commit", "--quiet", "-m", f"{name}")

    git(root, "checkout", "--quiet", trunk)
    return root
