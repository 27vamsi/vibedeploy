"""The one-off migration task. Readme.md section 18.2 step 3.

Step 1 already ran these same migrations, but in a database the build job was
free to destroy. This is the run that touches the database an app actually lives
in, and it is still not allowed to happen inside the worker: untrusted code gets
its own process, always (section 3 rule 11). Locally that is a container; in the
cloud it is a one-off ECS task, and this file is what that task runs.

It is given the migrator DSN in the **environment**, never in `argv`, and it
writes its verdict to a file rather than printing it, so that whatever the app's
own migrations decide to print cannot be mistaken for the result.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg

from buildjob import deps, migrate_alembic
from buildjob.migrate_scratch import MigrationError, apply_sql_migrations

UNSUPPORTED = (
    "This migration task can run a folder of plain `.sql` migrations or an"
    " Alembic project, and found neither. It was told the migrations are"
    " `{kind}`."
)
NO_DEPS_DIR = (
    "These migrations need the app's own dependencies installed, and this task"
    " was not given anywhere to put them, so nothing was run."
)
PARTWAY = (
    "The app's migrations stopped partway through, so the database is not in a"
    " known state: {reason}"
)


def _write(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result), encoding="utf-8")


async def _sql(repo_dir: Path, migrations_path: str, dsn: str) -> None:
    """Plain `.sql` files, against a database that may have seen them before.

    Unlike the build job's scratch pass, this database persists between
    deployments, so the ledger is what stops the second deploy of an app from
    re-running `001_init.sql`. Its schema is passed in rather than derived here:
    this process is given one DSN and no idea what the app is called.
    """
    conn = await asyncpg.connect(dsn)
    try:
        await apply_sql_migrations(
            conn, repo_dir / migrations_path, ledger=os.environ["VD_LEDGER_SCHEMA"]
        )
    finally:
        await conn.close()


async def _alembic(repo_dir: Path, migrations_path: str, dsn: str) -> None:
    """Same two steps as the build job's `plan` and `verify` passes.

    The install is a no-op the third time: `deps.install` stamps the target with
    what it installed, and all three passes of one deployment share it.
    """
    deps_dir = os.environ.get("VD_DEPS_DIR")
    if not deps_dir:
        raise MigrationError(repo_dir, RuntimeError(NO_DEPS_DIR))

    await asyncio.to_thread(deps.install, repo_dir, Path(deps_dir))
    await asyncio.to_thread(
        migrate_alembic.upgrade,
        dsn=dsn,
        repo_dir=repo_dir,
        migrations_path=migrations_path,
    )


async def main() -> int:
    result_path = Path(os.environ["VD_RESULT_PATH"])
    kind = os.environ["VD_MIGRATIONS_KIND"]
    repo_dir = Path(os.environ["VD_REPO_DIR"])
    migrations_path = os.environ["VD_MIGRATIONS_PATH"]
    dsn = os.environ["VD_MIGRATOR_DSN"]

    runners = {"sql": _sql, "alembic": _alembic}
    if kind not in runners:
        _write(result_path, {"status": "failed", "reason": UNSUPPORTED.format(kind=kind)})
        return 1

    try:
        await runners[kind](repo_dir, migrations_path, dsn)
    except (
        MigrationError,
        deps.DependencyError,
        migrate_alembic.AlembicError,
    ) as exc:
        _write(
            result_path,
            {"status": "failed", "reason": f"The app's migrations did not run: {exc}"},
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - a half-migrated schema blocks
        _write(result_path, {"status": "failed", "reason": PARTWAY.format(reason=exc)})
        return 1

    _write(result_path, {"status": "ok"})
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
