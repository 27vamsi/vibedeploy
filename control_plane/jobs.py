"""The job queue. Readme.md section 5: a table, not a broker.

`FOR UPDATE SKIP LOCKED` is the first half of the trick. A worker takes the
oldest runnable row, and any other worker looking at the same instant steps over
the locked row instead of blocking on it, so two workers never *claim* the same
deployment.

It is not what stops two workers *running* the same deployment. The row lock
lives for exactly as long as the claiming transaction, and the worker commits
that transaction before any work starts, deliberately: a deploy takes minutes,
and holding a row lock across one would turn a single slow build into a stuck
queue. From the moment the claim commits, the queue has no lock on anything.

So the rest of the run is held by two columns instead.

  - **a lease.** `lease_expires_at` is a deadline the holder renews by saying
    "still here" (`heartbeat`). Miss it and the job is claimable again, which is
    how a crashed worker's deployment stops being stalled. The lease is short
    because it is renewed; a fixed cutoff long enough never to interrupt the
    slowest possible build would be far too long to wait out a crash.
  - **a fence.** The dangerous case is not a worker that died, it is a worker
    that was *declared* dead and wasn't — paused, or partitioned, or stuck on a
    disk for two minutes. Its lease expired, somebody else took the job, and now
    it wakes up and reports a verdict on work that is no longer its own. It
    cannot detect this by checking, because it has no idea how long it was gone.
    So `fence` is bumped by every claim, handed to the claiming worker, and
    named in the `WHERE` clause of every write that worker later makes. A worker
    holding a stale number does not write; it is told so by a `False`.

That is Kleppmann's fencing token, and it is the reason `finish` and `park`
return a boolean rather than nothing. Ignoring that boolean is the bug.

The state that matters for the pause is `waiting`. A pipeline that hits a
question it cannot answer parks the job there and stops, with no lease on it at
all, so nothing times it out into a default. It moves only when a builder
answers. That is section 3 rule 5 expressed as a queue state.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models import Job, JobStatus

KIND_DEPLOY = "deploy"
KIND_ADD_USER = "add_user"
ALL_KINDS = (KIND_DEPLOY, KIND_ADD_USER)

# How long a claim is good for without being renewed. Short, because it is
# renewed: this is the delay between a worker being killed and its deployment
# being picked up by another, not a guess at how long a build takes.
LEASE = timedelta(seconds=60)

# Renewed comfortably inside the lease, so one slow round trip to the database
# does not hand a live worker's job to somebody else.
HEARTBEAT = LEASE / 3

# A job that takes its worker down with it would otherwise be reclaimed, kill
# the next worker, be reclaimed again, and keep the whole fleet busy dying.
# After this many claims it is left `failed` for a person to read.
MAX_ATTEMPTS = 3

ABANDONED = (
    "This job was claimed {attempts} times and never finished, so something"
    " about it is taking its worker down. It will not be retried again until"
    " somebody has looked at it."
)


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

    Runnable is two things: queued, or running with a lease that has run out.
    Reclaiming here rather than in a sweeper of its own is what makes it safe —
    the expired row is taken under the same `FOR UPDATE SKIP LOCKED` as a fresh
    one, so there is no window in which a sweeper has requeued a job and two
    workers can both find it waiting.

    The returned job carries the `fence` the caller must quote in every later
    write. The row stays locked until the caller commits, so the claim and the
    status change are one atomic step even though they are two statements.
    """
    now = _now()
    stmt = (
        select(Job)
        .where(
            Job.kind.in_(kinds),
            Job.run_after <= now,
            or_(
                Job.status == JobStatus.queued.value,
                and_(
                    Job.status == JobStatus.running.value,
                    Job.lease_expires_at < now,
                ),
            ),
        )
        .order_by(Job.run_after, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        return None

    if job.attempts >= MAX_ATTEMPTS:
        return await _give_up(session, job, now)

    job.status = JobStatus.running.value
    job.locked_by = worker_id
    job.locked_at = now
    job.lease_expires_at = now + LEASE
    # Before the work, not after: the whole point is that the number the caller
    # leaves with is one no previous holder has.
    job.fence += 1
    job.attempts += 1
    job.updated_at = now
    await session.flush()
    return job


async def _give_up(session: AsyncSession, job: Job, now: datetime) -> None:
    """Stop reclaiming a job that keeps losing its worker.

    The fence is bumped on the way out even though nobody is being given it,
    because the worker that was lost is still out there holding the old one and
    must not be able to overwrite this verdict if it ever comes back.
    """
    job.status = JobStatus.failed.value
    job.last_error = ABANDONED.format(attempts=job.attempts)
    job.locked_by = None
    job.locked_at = None
    job.lease_expires_at = None
    job.fence += 1
    job.updated_at = now
    await session.flush()
    return None


async def heartbeat(session: AsyncSession, job_id: uuid.UUID, fence: int) -> bool:
    """Renew the lease. False means the job is no longer ours; stop working.

    The only way a worker can learn it was replaced, and the reason it is worth
    calling: a worker that keeps going after this returns False is doing work
    nobody will accept, with the app's database open in front of it.
    """
    now = _now()
    result = await session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.fence == fence,
            Job.status == JobStatus.running.value,
        )
        .values(lease_expires_at=now + LEASE, updated_at=now)
    )
    return bool(result.rowcount)


async def park(session: AsyncSession, job_id: uuid.UUID, fence: int) -> bool:
    """Stop, and wait for a person. The pause M7 is named after.

    The lease is given up rather than left to expire: nothing is working on a
    parked job, so a lease on one would eventually run out and put it back on
    the queue, which is exactly the guess the pause exists to refuse.
    """
    return await _settle(session, job_id, fence, status=JobStatus.waiting)


async def finish(
    session: AsyncSession,
    job_id: uuid.UUID,
    fence: int,
    *,
    status: JobStatus,
    error: str | None = None,
) -> bool:
    """Write down what happened, if this worker is still the one it happened to."""
    return await _settle(session, job_id, fence, status=status, error=error)


async def _settle(
    session: AsyncSession,
    job_id: uuid.UUID,
    fence: int,
    *,
    status: JobStatus,
    error: str | None = None,
) -> bool:
    now = _now()
    result = await session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.fence == fence,
            # Still `running` as well as still ours, so a verdict cannot be
            # written twice: a retried settle arrives with the right fence.
            Job.status == JobStatus.running.value,
        )
        .values(
            status=status.value,
            last_error=error,
            locked_by=None,
            locked_at=None,
            lease_expires_at=None,
            updated_at=now,
        )
    )
    return bool(result.rowcount)


async def wake(session: AsyncSession, deployment_id: uuid.UUID) -> int:
    """A builder answered: make the parked job runnable again.

    Everything about the claim is reset except `fence`, which only ever goes
    up. A pause can last days, and if answering a question put the fence back
    then every worker that ever held this job would be licensed to write to it.
    """
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
