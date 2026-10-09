"""The one-off migration task. Readme.md section 18.2 step 3.

Step 1 already ran these same migrations, but in a database the build job was
free to destroy. This is the run that touches the database an app actually lives
in, and it is still not allowed to happen inside the worker: untrusted code gets
its own process, always (section 3 rule 11). Locally that is a subprocess; in
the cloud it is a one-off ECS task, and this file is what that task runs.

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

from buildjob.migrate_scratch import MigrationError, apply_sql_migrations

UNSUPPORTED = (
    "Only a folder of plain `.sql` migrations can be run by this build job."
    " Alembic support arrives with the containerised build."
)


def _write(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result), encoding="utf-8")


async def main() -> int:
    result_path = Path(os.environ["VD_RESULT_PATH"])
    kind = os.environ["VD_MIGRATIONS_KIND"]
    source = Path(os.environ["VD_REPO_DIR"]) / os.environ["VD_MIGRATIONS_PATH"]

    if kind != "sql":
        _write(result_path, {"status": "failed", "reason": UNSUPPORTED})
        return 1

    conn = await asyncpg.connect(os.environ["VD_MIGRATOR_DSN"])
    try:
        await apply_sql_migrations(conn, source)
    except MigrationError as exc:
        _write(
            result_path,
            {"status": "failed", "reason": f"The app's migrations did not run: {exc}"},
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - a half-migrated schema blocks
        _write(
            result_path,
            {
                "status": "failed",
                "reason": (
                    "The app's migrations stopped partway through, so the"
                    f" database is not in a known state: {exc}"
                ),
            },
        )
        return 1
    finally:
        await conn.close()

    _write(result_path, {"status": "ok"})
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
