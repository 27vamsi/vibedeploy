"""Getting the code. Readme.md section 15.

Three kinds of "repository", and the differences are deliberate rather than
transitional:

  - a **git repository**, named by URL or by a path to one. It is cloned with
    `git`, which is a separate process by construction, and the commit it
    resolved to is handed back so the deployment records what was built instead
    of what was asked for. A branch name is not a build identity: the branch
    moves.
  - a **folder**, deployed exactly as it is on disk, asked for by the ref
    `working-tree`. Plenty of builders have an app and no repository, and "go
    and commit it first" is not an answer. A folder that happens to be a git
    repository is still a folder: its working tree is not its `HEAD`, and
    deploying the last commit when somebody asked for the folder would run code
    they are not looking at.
  - a **fixture tree**, a directory with `base/` and `branches/<name>/`
    overlays. This is how the planted-bug tests work, and it is the point of
    them: a planted-bug branch differs from the clean one by exactly the files
    it overlays, so when a deploy is blocked there is no doubt what blocked it.

Three rules the callers depend on:

  - the checkout is always a fresh directory the worker owns, so a build cannot
    leave anything behind in the source;
  - `.git` is removed before anything else sees the tree. The app's own process
    runs out of that directory, and git's config can carry a credential helper
    and a token;
  - nothing in a failure message comes from git's stderr. It can contain a URL
    with credentials in it, and a builder is owed a sentence rather than a
    transport error. The detail is kept on the exception for an operator.

V0.5 clones public repositories only. Short-lived GitHub App installation
tokens, and doing this inside the build job rather than the worker, arrive with
the containerised build (M8).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The ref that means "the folder, as it is on disk, right now".
WORKING_TREE = "working-tree"

# Never copied into a checkout.
#
# `.env` is the one that matters. On a laptop it holds a `DATABASE_URL`, and an
# app that loads its own `.env` and builds its own engine out of it is talking
# to a database none of this reaches: no identity, no policies, nothing proved.
# The runtime DSN is handed to the app by the platform (`VD_DATABASE_URL`) and
# an app's own copy does not travel. If an app genuinely needs a setting, it
# belongs in the deployment's environment, not in a file next to the code.
#
# The rest is noise at best, and a stale `__pycache__` can shadow a file an
# overlay was supposed to replace.
_NEVER_COPIED = (
    ".env",
    ".env.*",
    "*.env",
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "*.pyc",
    ".venv",
    "venv",
    "node_modules",
    ".vd-state",
    ".pytest_cache",
    ".mypy_cache",
    ".DS_Store",
)
IGNORED = shutil.ignore_patterns(*_NEVER_COPIED)

CLONE_TIMEOUT = 300.0

# A full-ish clone and then a detached checkout, because that one path resolves
# a branch, a tag and a bare commit alike. `--depth 1` cannot: a server will
# not normally serve an arbitrary commit to a shallow fetch, and `verify` has
# to be able to ask for the exact commit `plan` resolved.
_CLONE = ("clone", "--quiet", "--no-tags", "--no-checkout")

NEEDS_CREDENTIALS = (
    "This repository is private, or it does not exist. V0.5 can only deploy a"
    " public repository."
)
UNREACHABLE = (
    "We could not fetch this repository. Check the address and that it is"
    " reachable from here."
)

_CREDENTIALS_IN_URL = re.compile(r"(?<=//)[^/@\s]+(?=@)")
_AUTH_FAILURE = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "permission denied",
    "repository not found",
)

# git, with none of this machine's own configuration. A developer's global
# config can hold a credential helper, a hooks path or a URL rewrite, and none
# of those may change what a deployment builds.
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    # Never block a build waiting for a password nobody will type.
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "GCM_INTERACTIVE": "never",
}


class CheckoutError(RuntimeError):
    """The code could not be fetched, in words a builder can act on."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        # For the job row, never for the dashboard.
        self.detail = _redact(detail)


@dataclass(frozen=True)
class Checkout:
    path: Path
    # What was built, which is not always what was asked for: a folder has no
    # branches, so any ref somebody typed resolves to `working-tree`. The
    # deployment records this one, so the dashboard never names something that
    # was not consulted.
    ref: str
    # The commit that was built. `None` for a folder or a fixture tree, neither
    # of which has an identity beyond the directory it came from.
    commit_sha: str | None


def _redact(text: str) -> str:
    return _CREDENTIALS_IN_URL.sub("***", text)


def _force_remove(path: Path) -> None:
    """Delete a tree even if git left its objects read-only (Windows).

    `ignore_errors` would be worse than useless here: it would leave half a
    previous checkout in place and the next build would run against a mixture
    of two commits.
    """

    def unwritable(func, target, _exc):  # pragma: no cover - platform specific
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=unwritable)


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(_GIT_ENV)
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=CLONE_TIMEOUT,
    )


def branches(repo: Path) -> list[str]:
    directory = repo / "branches"
    if not directory.is_dir():
        return []
    return sorted(child.name for child in directory.iterdir() if child.is_dir())


def _resolve(dest: Path, ref: str) -> str | None:
    """The commit `ref` names, as a branch of the remote or as a commit-ish.

    A clone leaves branches as `origin/<name>` and only the default branch as a
    local one, so the remote-tracking name is tried first: that is what a
    builder means by a branch. A bare commit resolves on the second attempt.
    """
    for candidate in (f"origin/{ref}", ref):
        resolved = _git(
            "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}", cwd=dest
        )
        if resolved.returncode == 0 and resolved.stdout.strip():
            return resolved.stdout.strip()
    return None


def _clone(repo: str, ref: str, dest: Path) -> Checkout:
    _force_remove(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    cloned = _git(*_CLONE, repo, str(dest))
    if cloned.returncode != 0:
        _force_remove(dest)
        stderr = cloned.stderr.lower()
        message = (
            NEEDS_CREDENTIALS
            if any(needle in stderr for needle in _AUTH_FAILURE)
            else UNREACHABLE
        )
        raise CheckoutError(message, cloned.stderr)

    commit_sha = _resolve(dest, ref)
    if commit_sha is None:
        _force_remove(dest)
        raise CheckoutError(
            f"There is no branch or commit `{ref}` in this repository."
        )

    checked_out = _git("checkout", "--quiet", "--detach", commit_sha, cwd=dest)
    if checked_out.returncode != 0:
        _force_remove(dest)
        raise CheckoutError(UNREACHABLE, checked_out.stderr)

    # Before anything runs out of this directory, and before the worker hands
    # the path to the build job.
    _force_remove(dest / ".git")
    return Checkout(path=dest, ref=ref, commit_sha=commit_sha)


def _snapshot(source: Path, dest: Path) -> Checkout:
    """The folder, as it is on disk, minus the things that must not travel."""
    if dest.resolve().is_relative_to(source.resolve()):
        raise CheckoutError(
            "The build directory cannot be inside the folder being deployed."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Symlinks are copied as links rather than followed: following one could
    # walk straight out of the folder and into the rest of the disk.
    shutil.copytree(source, dest, ignore=IGNORED, symlinks=True)
    return Checkout(path=dest, ref=WORKING_TREE, commit_sha=None)


def _overlay(source: Path, ref: str, dest: Path) -> Checkout:
    overlay = source / "branches" / ref
    if not overlay.is_dir():
        available = ", ".join(f"`{name}`" for name in branches(source)) or "none"
        raise CheckoutError(
            f"There is no branch `{ref}` in this repository. Branches: {available}."
        )

    shutil.copytree(source / "base", dest, ignore=IGNORED)
    shutil.copytree(overlay, dest, ignore=IGNORED, dirs_exist_ok=True)
    return Checkout(path=dest, ref=ref, commit_sha=None)


def checkout(repo: str | Path, ref: str, dest: Path) -> Checkout:
    """Materialise `ref` of `repo` into `dest`, which is replaced if it exists.

    The order of these questions is the whole behaviour:

      1. a URL can only be git, and nothing local is going to answer for it;
      2. `base/` + `branches/` is a fixture tree, whatever else it contains;
      3. `working-tree` means the folder itself, even if it is a git repository.
         Asking for the folder and getting its last commit would be a lie;
      4. a git repository, asked for by ref, builds that ref and not the dirty
         files sitting next to it;
      5. anything else is a plain folder, and a folder has only one version.
    """
    source = Path(repo)
    remote = "://" in str(repo) or str(repo).startswith("git@")

    if remote:
        return _clone(str(repo), ref, dest)
    if not source.is_dir():
        raise CheckoutError(f"There is no repository at `{source}`.")

    _force_remove(dest)
    if (source / "base").is_dir():
        return _overlay(source, ref, dest)
    if ref == WORKING_TREE:
        return _snapshot(source, dest)
    if (source / ".git").exists():
        return _clone(str(source), ref, dest)
    return _snapshot(source, dest)
