"""Bring the control plane's schema up to date.

Exposed as a function as well as a command because the worker, the API and the
tests all need the same guarantee before they touch a table, and shelling out
to `alembic` from three places would be three chances to point at the wrong
database.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

import asyncpg

from control_plane.config import ControlPlaneConfig
from control_plane.db import ensure_database

HERE = Path(__file__).resolve().parent

# One arbitrary, fixed number, so every process that migrates this database
# contends for the same lock. Session-scoped rather than transactional because
# Alembic runs many transactions and the lock has to span all of them.
#
# This exists because `prepare` is now called from the control plane's own
# startup rather than by hand: on a box with a restart policy, two containers
# coming up together would otherwise both run `alembic upgrade head` against an
# empty database and the loser would fail on a table that already exists —
# turning a routine restart into a crash loop.
MIGRATION_LOCK = 0x7664_0001


def alembic_config(config: ControlPlaneConfig | None = None) -> Config:
    """Alembic's config, carrying ours.

    `env.py` reads the database URL out of `attributes` when it is there, so a
    caller that has already decided which database this is (the tests, mainly)
    cannot be overruled by whatever happens to be in the environment.
    """
    alembic = Config(str(HERE / "alembic.ini"))
    if config is not None:
        alembic.attributes["control_plane_config"] = config
    return alembic


def upgrade(
    revision: str = "head", config: ControlPlaneConfig | None = None
) -> None:
    command.upgrade(alembic_config(config), revision)


async def prepare(config: ControlPlaneConfig | None = None) -> None:
    """Create the database if needed, then migrate it, once at a time.

    The lock is held for the whole upgrade and released when the connection
    closes, so a process that dies mid-migration does not leave the next one
    waiting forever.
    """
    config = config or ControlPlaneConfig.from_env()
    await ensure_database(config)

    conn = await asyncpg.connect(config.database_url.replace("+asyncpg", ""))
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK)
        await asyncio.to_thread(upgrade, "head", config)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(prepare())
