"""Installing the app's own dependencies. Readme.md section 3 rule 11.

A folder of `.sql` migrations can be read and executed. An Alembic project
cannot: its `env.py` is the app's own Python and it almost always imports the
app's models, so running the migrations means importing the app, which means
having what the app imports.

So this installs packages the builder chose, from an index we do not control,
by running setup code we have never read. That is three good reasons it only
ever happens inside the build container, and two rules about where it writes:

  - **into a directory that holds nothing of ours.** The checkout is mounted
    read-only and the spec file — which carries the build job's one-time token —
    is mounted on its own, so the install target is a third mount with nothing
    in it but packages;
  - **no cache shared between apps.** A wheel cache that two apps' builds both
    wrote to would be a channel from one app's migrations into another app's
    build. Paying the download again per deployment is the cheaper mistake.

`fingerprint` is why a deployment pays once rather than three times: the plan
pass, the verify pass and the real migration task all read the same target, and
the first one to arrive does the work.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

# Only the one file. An app that declares its dependencies another way is not
# refused — nothing is installed, and if `env.py` then needs something, the
# ImportError names it, which is more use than a guess from us would be.
REQUIREMENTS = "requirements.txt"

# Written next to the packages, so "are these the right packages already?" is a
# question about the target rather than about anything we have to remember.
STAMP = ".vd-requirements"

INSTALL_FAILED = (
    "The app's dependencies could not be installed, so its migrations were"
    " never run and nothing was proved. The build log has what the installer"
    " said."
)

# Generous but finite. A build that is still resolving after this is wedged, and
# a wedged build has to end in a sentence rather than in the deploy timeout.
TIMEOUT = 10 * 60


class DependencyError(RuntimeError):
    """The app's dependencies are not installable, so nothing can be proved."""


def requirements(repo_dir: Path) -> Path | None:
    path = Path(repo_dir) / REQUIREMENTS
    return path if path.is_file() else None


def fingerprint(path: Path) -> str:
    """What was asked for, so a second caller can tell it is already there."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def argv(requirements_file: Path, target: Path) -> list[str]:
    """The installer command line, as a value, so a test can read it."""
    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-input",
        "--disable-pip-version-check",
        "--no-cache-dir",
        # Not into our own site-packages: the app's packages go somewhere we can
        # put on the path deliberately, and nowhere else.
        "--target",
        str(target),
        "--requirement",
        str(requirements_file),
    ]


def _pip(command: Sequence[str]) -> None:
    result = subprocess.run(
        list(command),
        stdout=sys.stderr,
        stderr=subprocess.STDOUT,
        timeout=TIMEOUT,
        check=False,
    )
    if result.returncode != 0:
        raise DependencyError(INSTALL_FAILED)


def install(
    repo_dir: Path,
    target: Path,
    *,
    _run: Callable[[Sequence[str]], None] = _pip,
) -> Path | None:
    """Install the app's dependencies into `target` and put it on the path.

    Returns the target, or `None` if the app declares no dependencies. Both are
    success: an app with no `requirements.txt` has nothing to install, and the
    caller treats that the same way.
    """
    found = requirements(repo_dir)
    if found is None:
        return None

    target = Path(target)
    stamp = target / STAMP
    wanted = fingerprint(found)
    already = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else None

    if already != wanted:
        target.mkdir(parents=True, exist_ok=True)
        _run(argv(found, target))
        stamp.write_text(wanted, encoding="utf-8")

    activate(target)
    return target


def activate(target: Path) -> None:
    """Make an already-installed target importable, without installing again.

    Ahead of our own packages, because these are the versions the app's
    migrations were written against. If that shadows something the build job
    needs, the build fails loudly here rather than migrating against a library
    the app has never seen.
    """
    entry = str(Path(target))
    if entry not in sys.path:
        sys.path.insert(0, entry)
