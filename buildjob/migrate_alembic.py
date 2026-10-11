"""Running the app's Alembic migrations. Readme.md section 15, line 657.

A folder of `.sql` files is data: we read it and execute it. An Alembic project
is not. It is the app's own Python — an `env.py` that imports the app's models
and opens its own connection — so the only honest way to apply it is to run it,
in the container, as the migrator.

Running somebody else's code means deciding what it is allowed to decide, and
there is exactly one thing it is not: **which database it touches**. A committed
`alembic.ini` almost always names one, usually a developer's laptop and
occasionally a real one. So the URL is forced in both of the two places an
`env.py` might look, because real ones are split about evenly between them:

  - `sqlalchemy.url` on the Config object, for `engine_from_config`;
  - `DATABASE_URL` in the environment, for the ones that read it themselves.

Everything else is deliberately the app's own: its `env.py`, its
`script_location`, its revisions. The working directory and `sys.path` are set
to the checkout so that `from models import Base` finds the app and not us,
which is also why `prepend_sys_path = .` in a real `alembic.ini` works here.

What this cannot do is force an `env.py` that hardcodes its own driver — one
that builds `postgresql+asyncpg://...` out of its own settings object will use
its own settings object. That fails with a connection error naming the host it
chose, which is a better outcome than quietly migrating the wrong database.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Iterator

INI = "alembic.ini"
DEFAULT_SCRIPTS = "alembic"

NO_INI = (
    "This app's migrations look like Alembic, but there is no `alembic.ini`"
    " next to them, so there is no way to run them."
)
NO_SCRIPTS = (
    "`alembic.ini` points at a migrations folder that is not in the repository,"
    " so there is nothing to run."
)
FAILED = "The app's Alembic migrations did not run: {reason}"


class AlembicError(RuntimeError):
    """The migrations could not be run, in words a builder can act on."""


def configure(*, dsn: str, repo_dir: Path, migrations_path: str):
    """The app's own Alembic config, with the database decided by us.

    Returned rather than used so that a test can read what was forced. Imports
    Alembic lazily: the worker imports this module's name through
    `buildjob.run`, and the worker has no business having Alembic loaded.
    """
    from alembic.config import Config

    repo_dir = Path(repo_dir)
    ini = repo_dir / INI
    if not ini.is_file():
        raise AlembicError(NO_INI)

    scripts = repo_dir / (migrations_path or DEFAULT_SCRIPTS)
    if not scripts.is_dir():
        raise AlembicError(NO_SCRIPTS)

    config = Config(str(ini))
    config.set_main_option("script_location", _literal(str(scripts)))
    config.set_main_option("sqlalchemy.url", _literal(dsn))
    return config


def _literal(value: str) -> str:
    """`%` starts an interpolation in an ini file, so a real one is doubled.

    A generated password containing `%` would otherwise arrive at Postgres with
    a character missing, or raise, depending on what followed it.
    """
    return value.replace("%", "%%")


@contextlib.contextmanager
def pointed_at(dsn: str) -> Iterator[None]:
    """`DATABASE_URL`, for the half of `env.py` files that read it themselves.

    Put back afterwards: the build job introspects, seeds and attacks after
    migrating, and none of that should find a migrator DSN lying around in the
    environment.
    """
    before = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = dsn
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = before


@contextlib.contextmanager
def _as_the_app(repo_dir: Path) -> Iterator[None]:
    """Run from inside the checkout, with the checkout importable."""
    repo_dir = str(Path(repo_dir))
    cwd = os.getcwd()
    added = repo_dir not in sys.path
    if added:
        sys.path.insert(0, repo_dir)
    os.chdir(repo_dir)
    try:
        yield
    finally:
        os.chdir(cwd)
        if added:
            sys.path.remove(repo_dir)


def upgrade(
    *,
    dsn: str,
    repo_dir: Path,
    migrations_path: str,
    revision: str = "head",
) -> None:
    """Apply the app's migrations to `dsn`. Blocking; call it in a thread."""
    from alembic import command

    config = configure(dsn=dsn, repo_dir=repo_dir, migrations_path=migrations_path)
    with _as_the_app(repo_dir), pointed_at(dsn):
        try:
            command.upgrade(config, revision)
        except Exception as exc:  # noqa: BLE001 - any failure here blocks
            raise AlembicError(FAILED.format(reason=exc)) from exc
