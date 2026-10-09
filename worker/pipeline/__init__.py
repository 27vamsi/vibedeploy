"""The steps of one deployment, in the order Readme.md section 18.2 fixes.

`Verdict` lives here because every handler produces one and none of them is
allowed to write the job row itself. Job bookkeeping happens in exactly one
place, the worker loop, so that "blocked" can never be confused with "failed":
a blocked deployment is a job that did precisely what it exists to do.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Sessions = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class Verdict:
    action: str  # "park" | "done" | "failed"
    error: str | None = None


PARK = Verdict("park")
DONE = Verdict("done")
