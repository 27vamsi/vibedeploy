"""Getting the code. Readme.md section 15, locally.

The real thing is a short-lived GitHub installation token and a clone inside the
build job (M8). Until then the "repository" is a directory on disk, and a
"branch" is an overlay directory applied on top of a shared base:

    fixtures/repos/invoices/
        base/                    everything the branches have in common
        branches/clean/          the branch that should go live
        branches/permissive/     one planted bug, and nothing else different

That shape is not a shortcut, it is the point: a planted-bug branch differs from
the clean one by exactly the files it overlays, so when the deploy is blocked
there is no doubt about what blocked it.

The checkout is always into a fresh directory the worker owns. Nothing is ever
run from the fixture tree itself, so a build cannot leave anything behind in it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

# Never copied into a checkout: noise at best, and a stale `__pycache__` can
# shadow a file the branch overlay was supposed to replace.
IGNORED = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv", ".vd-state")


class CheckoutError(RuntimeError):
    """The code could not be fetched, in words a builder can act on."""


def branches(repo: Path) -> list[str]:
    directory = repo / "branches"
    if not directory.is_dir():
        return []
    return sorted(child.name for child in directory.iterdir() if child.is_dir())


def checkout(repo: str | Path, ref: str, dest: Path) -> Path:
    """Materialise `ref` of `repo` into `dest`, which must not exist yet."""
    source = Path(repo)
    if not source.is_dir():
        raise CheckoutError(f"There is no repository at `{source}`.")

    base = source / "base"
    if not base.is_dir():
        # A plain directory is its own only branch. The ref is still recorded on
        # the deployment, so the dashboard never shows a commit nobody built.
        shutil.copytree(source, dest, ignore=IGNORED)
        return dest

    overlay = source / "branches" / ref
    if not overlay.is_dir():
        available = ", ".join(f"`{name}`" for name in branches(source)) or "none"
        raise CheckoutError(
            f"There is no branch `{ref}` in this repository. Branches: {available}."
        )

    shutil.copytree(base, dest, ignore=IGNORED)
    shutil.copytree(overlay, dest, ignore=IGNORED, dirs_exist_ok=True)
    return dest
