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


async def apply_sql_migrations(
    conn: asyncpg.Connection, source: Path
) -> list[str]:
    """Apply each file in its own transaction. Returns the names applied."""
    applied = []
    for path in migration_files(source):
        sql = path.read_text(encoding="utf-8")
        try:
            async with conn.transaction():
                await conn.execute(sql)
        except asyncpg.PostgresError as exc:
            raise MigrationError(path, exc) from exc
        applied.append(path.name)
    return applied
