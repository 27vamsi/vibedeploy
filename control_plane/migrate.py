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

from control_plane.config import ControlPlaneConfig
from control_plane.db import ensure_database

HERE = Path(__file__).resolve().parent


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
    """Create the database if needed, then migrate it."""
    config = config or ControlPlaneConfig.from_env()
    await ensure_database(config)
    await asyncio.to_thread(upgrade, "head", config)


if __name__ == "__main__":
    asyncio.run(prepare())
