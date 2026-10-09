"""Getting to the control plane's own database, and nothing else's.

The engine here is only ever pointed at `vibedeploy_control`. App data is
reached with raw asyncpg by the worker and the build job, never through this
session, so there is no code path where a dashboard query and a customer's rows
can end up in the same transaction.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import asyncpg
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from control_plane.config import ControlPlaneConfig

Sessionmaker = async_sessionmaker[AsyncSession]


def make_engine(config: ControlPlaneConfig) -> AsyncEngine:
    return create_async_engine(config.database_url, pool_pre_ping=True, future=True)


def make_sessionmaker(engine: AsyncEngine) -> Sessionmaker:
    # `expire_on_commit=False` so a handler can still read the row it just
    # wrote without a second round trip.
    return async_sessionmaker(engine, expire_on_commit=False)


def _split_database(url: str) -> tuple[str, str]:
    """('postgresql://host/name', 'name') from a SQLAlchemy or libpq URL."""
    parts = urlsplit(url.replace("+asyncpg", ""))
    name = parts.path.lstrip("/")
    if not name:
        raise ValueError(f"{url!r} names no database")
    return urlunsplit(parts._replace(path="/postgres")), name


async def ensure_database(config: ControlPlaneConfig) -> None:
    """Create the control plane database if this is a fresh machine.

    `CREATE DATABASE` cannot run inside a transaction and cannot be
    parameterised, so the name is taken from our own configured URL and quoted,
    never from anything a request carried in.
    """
    maintenance_url, name = _split_database(config.database_url)
    conn = await asyncpg.connect(maintenance_url)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", name
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
