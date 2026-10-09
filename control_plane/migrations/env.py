"""Alembic environment for the control plane.

Two things here are not boilerplate:

  - the URL comes from `ControlPlaneConfig`, never from alembic.ini, so there
    is one source of truth for which database this is;
  - the schema is created before the version table, because every table
    including `alembic_version` lives in `vd_control` rather than `public`.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from control_plane.config import ControlPlaneConfig
from control_plane.models import SCHEMA, Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _config() -> ControlPlaneConfig:
    return config.attributes.get("control_plane_config") or ControlPlaneConfig.from_env()


def run_migrations_offline() -> None:
    context.configure(
        url=_config().sync_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=SCHEMA,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Async so that asyncpg stays the only Postgres driver in the project."""
    engine = create_async_engine(_config().database_url)
    async with engine.connect() as connection:
        await connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
        await connection.commit()
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
