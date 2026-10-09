"""The job queue. Readme.md section 5: a table, not a broker.

`FOR UPDATE SKIP LOCKED` is the whole trick. A worker takes the oldest runnable
row, and any other worker looking at the same instant steps over the locked row
instead of blocking on it, so two workers never run the same deployment.

The state that matters for M7 is `waiting`. A pipeline that hits a question it
cannot answer parks the job there and stops. Nothing retries it, nothing times
it out into a default: it moves only when a builder answers. That is section 3
rule 5 expressed as a queue state.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models import Job, JobStatus

KIND_DEPLOY = "deploy"
KIND_ADD_USER = "add_user"
ALL_KINDS = (KIND_DEPLOY, KIND_ADD_USER)

# A job whose worker died mid-flight is not lost; it becomes claimable again.
# Long enough that a slow build is never stolen from a worker still working on
# it, short enough that a crash is not a permanent stall.
LEASE = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def enqueue(
    session: AsyncSession,
    *,
    kind: str = KIND_DEPLOY,
    app_id: uuid.UUID | None = None,
    deployment_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> Job:
    job = Job(
        kind=kind,
        app_id=app_id,
        deployment_id=deployment_id,
        payload=payload or {},
        status=JobStatus.queued.value,
        run_after=_now(),
    )
    session.add(job)
    await session.flush()
    return job


async def claim(
    session: AsyncSession, worker_id: str, kinds: Sequence[str] = ALL_KINDS
) -> Job | None:
    """Take one runnable job, or None. Must be called inside a transaction.

    The row stays locked until the caller commits, so the claim and the status
    change are one atomic step even though they are two statements.
    """
    now = _now()
    stmt = (
        select(Job)
        .where(
            Job.kind.in_(kinds),
            Job.status == JobStatus.queued.value,
            Job.run_after <= now,
        )
        .order_by(Job.run_after, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        return None

    job.status = JobStatus.running.value
    job.locked_by = worker_id
    job.locked_at = now
    job.attempts += 1
    job.updated_at = now
    await session.flush()
    return job


async def reclaim_expired(session: AsyncSession) -> int:
    """Put jobs whose worker vanished back on the queue."""
    cutoff = _now() - LEASE
    result = await session.execute(
        update(Job)
        .where(Job.status == JobStatus.running.value, Job.locked_at < cutoff)
        .values(
            status=JobStatus.queued.value,
            locked_by=None,
            locked_at=None,
            updated_at=_now(),
        )
    )
    return result.rowcount or 0


async def park(session: AsyncSession, job: Job) -> None:
    """Stop, and wait for a person. The pause M7 is named after."""
    job.status = JobStatus.waiting.value
    job.locked_by = None
    job.locked_at = None
    job.updated_at = _now()
    await session.flush()


async def wake(session: AsyncSession, deployment_id: uuid.UUID) -> int:
    """A builder answered: make the parked job runnable again."""
    result = await session.execute(
        update(Job)
        .where(
            Job.deployment_id == deployment_id,
            Job.status == JobStatus.waiting.value,
        )
        .values(
            status=JobStatus.queued.value,
            run_after=_now(),
            updated_at=_now(),
        )
    )
    return result.rowcount or 0


async def finish(
    session: AsyncSession, job: Job, *, status: JobStatus, error: str | None = None
) -> None:
    job.status = status.value
    job.last_error = error
    job.locked_by = None
    job.locked_at = None
    job.updated_at = _now()
    await session.flush()
