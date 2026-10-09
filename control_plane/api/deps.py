"""Shared request plumbing.

One transaction per request, and a handler that writes ends with `commit`.
Nothing in the control plane is allowed to half-happen: a deployment that
records a verdict but not the runs behind it would be a lie about what was
proven. Closing the session rolls back anything a handler did not commit, so
the failure mode is "it did not happen" rather than "half of it did".

Committing on the way out of this generator would look tidier and would be
wrong. FastAPI runs the exit half of a `yield` dependency **after** the response
has been sent, and both of our callers act on that response the instant they
have it: the build job exits and the worker immediately reads the verdict back
out of the database, and a browser follows our redirect to a page that has to
show the row we just made. Either would race the commit and lose.
"""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.config import ControlPlaneConfig


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_config(request: Request) -> ControlPlaneConfig:
    return request.app.state.config
