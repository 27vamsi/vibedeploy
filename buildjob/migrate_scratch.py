"""Applies the app's own migrations to the scratch database. Readme.md M3.

This is untrusted code by definition, which is why it only ever runs here, in
the isolated build job, never in the worker or the control plane (Readme.md
section 3 rule 11).

It runs as the **migrator** role: the one role with BYPASSRLS, so data backfills
reach every row. The migrator is never handed to the app or the gateway, and
everything it creates is reassigned to the owner role afterwards
(`kernel.provision.reassign_objects_to_owner`) so that FORCE cannot be
sidestepped later by a role that has a password.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg


class MigrationError(RuntimeError):
    """One migration file failed. Carries the file so the reason can be shown."""

    def __init__(self, path: Path, cause: Exception):
        super().__init__(f"{path.name}: {cause}")
        self.path = path
        self.cause = cause


def migration_files(source: Path) -> list[Path]:
    """A single .sql file, or every .sql in a directory in filename order."""
    if source.is_dir():
        return sorted(source.glob("*.sql"))
    return [source]


LEDGER = "vd_applied_migrations"


async def _already_applied(conn: asyncpg.Connection, ledger: str) -> set[str]:
    """The files this database has already seen, creating the ledger if needed.

    A folder of plain `.sql` files has no version table of its own — that is the
    difference between it and Alembic — so without this, the second deployment
    of an app re-runs `001_init.sql` against a schema that already has those
    tables and the deploy is blocked by `relation "users" already exists`. Which
    is to say: no app with plain SQL migrations could be deployed twice.

    The ledger lives in the app's platform schema, never in the app's own, for
    the reasons in `AppRoles.ledger`.
    """
    await conn.execute(
        f'CREATE TABLE IF NOT EXISTS {ledger}."{LEDGER}" ('
        " filename text PRIMARY KEY,"
        " applied_at timestamptz NOT NULL DEFAULT now())"
    )
    rows = await conn.fetch(f'SELECT filename FROM {ledger}."{LEDGER}"')
    return {row["filename"] for row in rows}


async def apply_sql_migrations(
    conn: asyncpg.Connection, source: Path, ledger: str | None = None
) -> list[str]:
    """Apply each file in its own transaction. Returns the names applied.

    `ledger` is a quoted schema name to record applied filenames in, and skip
    anything already there. `None` means apply everything, which is right for
    the build job's throwaway database: it is empty every time, and a scratch
    run that skipped a file would make the schema hash it reports a hash of
    something the real database was never compared against.
    """
    seen = await _already_applied(conn, ledger) if ledger else set()
    applied = []
    for path in migration_files(source):
        if path.name in seen:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            # One transaction per file, and the ledger row is written inside it:
            # a file that fails leaves neither its tables nor a note saying it
            # ran, so the next attempt starts from the same place this one did.
            async with conn.transaction():
                await conn.execute(sql)
                if ledger:
                    await conn.execute(
                        f'INSERT INTO {ledger}."{LEDGER}" (filename) VALUES ($1)',
                        path.name,
                    )
        except asyncpg.PostgresError as exc:
            raise MigrationError(path, exc) from exc
        applied.append(path.name)
    return applied
