"""A worker dying, and a worker only *thought* to have died.

`FOR UPDATE SKIP LOCKED` is what stops two workers claiming the same row. It is
not what stops two workers *running* the same job: the lock ends when the
claiming transaction commits, and `worker/main.py` commits it before any work
starts on purpose — a deployment takes minutes, and holding a row lock across
one would turn a single slow build into a stuck queue.

So from the instant a claim commits, the queue is not protected by a lock. It is
protected by a lease: a promise to come back and say "still here" before a
deadline. Two things can go wrong with a promise like that, and only one of them
is the obvious one.

  - **the worker dies.** Nothing renews the lease, it runs out, and somebody
    else has to be able to pick the job up. Without this a crash is a permanent
    stall; this is the easy half.
  - **the worker is declared dead and isn't.** It was paused, or its network
    went away, or its disk hung for two minutes. Its lease expired, another
    worker took the job and is halfway through it — and then the first worker
    wakes up and finishes what it thought it was doing. It writes a verdict
    about work that is now somebody else's. *That* is the half that corrupts
    state rather than merely delaying it, and no timeout can fix it, because
    the first worker has no way of knowing how long it was gone.

The fix is a fencing token (Kleppmann): a counter on the row, bumped by every
claim, carried by the claiming worker, and named in the `WHERE` clause of every
write it later makes. A worker holding fence 1 cannot write a row that has moved
on to fence 2 — not because it checked, but because its `UPDATE` matches no
rows. It finds out it was replaced by being unable to do anything.

Every test below is one sentence from that argument.
"""

from __future__ import annotations

import asyncio

import pytest

from control_plane import jobs
from control_plane.models import Job, JobStatus
from tests.orchestration.conftest import now
from worker import main as worker_main
from worker.pipeline import Verdict


# ---------------------------------------------------------------------------
# The claim itself
# ---------------------------------------------------------------------------


async def test_two_workers_never_claim_the_same_job(queue):
    """What SKIP LOCKED does do, asserted so the rest has a baseline."""
    job_id = await queue.enqueue()

    first, second = await asyncio.gather(queue.claim("a"), queue.claim("b"))

    claimed = [c for c in (first, second) if c is not None]
    assert len(claimed) == 1
    assert claimed[0].id == job_id


async def test_a_claim_hands_out_a_lease_and_a_fence(queue):
    """Both halves arrive together, because neither works without the other."""
    job_id = await queue.enqueue()

    claimed = await queue.claim("a")

    assert claimed is not None
    assert claimed.fence == 1
    stored = await queue.read(job_id)
    assert stored.status == JobStatus.running.value
    assert stored.locked_by == "a"
    assert stored.lease_expires_at > now()


# ---------------------------------------------------------------------------
# The worker died
# ---------------------------------------------------------------------------


async def test_a_running_job_is_not_claimable_while_its_lease_holds(queue):
    await queue.enqueue()
    assert await queue.claim("a") is not None

    assert await queue.claim("b") is None


async def test_a_crashed_workers_job_is_reclaimed_when_its_lease_expires(queue):
    """The easy half: a stall has to end by itself."""
    job_id = await queue.enqueue()
    first = await queue.claim("a")
    assert first is not None
    await queue.expire(job_id)

    second = await queue.claim("b")

    assert second is not None
    assert second.id == job_id
    assert second.fence == first.fence + 1
    assert (await queue.read(job_id)).locked_by == "b"


async def test_a_working_worker_keeps_its_job(queue):
    """A lease that is being renewed is not an invitation.

    The reason the lease is short enough to be useful: a worker that is alive
    says so every `HEARTBEAT`, so `LEASE` can be tens of seconds rather than
    the half hour it takes to be sure the longest possible build has finished.
    """
    job_id = await queue.enqueue()
    first = await queue.claim("a")
    assert first is not None
    await queue.expire(job_id)

    async with queue.sessions() as session, session.begin():
        assert await jobs.heartbeat(session, job_id, first.fence) is True

    assert await queue.claim("b") is None
    assert (await queue.read(job_id)).lease_expires_at > now()


# ---------------------------------------------------------------------------
# The worker did not die. The fence.
# ---------------------------------------------------------------------------


async def test_a_fenced_out_worker_cannot_finish_the_job(queue):
    """The one that matters. A zombie's verdict is not written down.

    Worker `a` is slow, not dead. By the time it has an answer the job belongs
    to `b` and is halfway through a second attempt. `a` reporting "done" here
    would mark a half-finished deployment complete and release it.
    """
    job_id = await queue.enqueue()
    zombie = await queue.claim("a")
    assert zombie is not None
    await queue.expire(job_id)
    live = await queue.claim("b")
    assert live is not None

    async with queue.sessions() as session, session.begin():
        wrote = await jobs.finish(
            session, job_id, zombie.fence, status=JobStatus.done
        )

    assert wrote is False
    stored = await queue.read(job_id)
    assert stored.status == JobStatus.running.value
    assert stored.locked_by == "b"

    # And the worker that does hold the job can still finish it.
    async with queue.sessions() as session, session.begin():
        assert await jobs.finish(
            session, job_id, live.fence, status=JobStatus.done
        ) is True
    assert (await queue.read(job_id)).status == JobStatus.done.value


async def test_a_fenced_out_worker_cannot_park_the_job(queue):
    """`waiting` is reachable by the same path, so it is fenced by the same test.

    Parking is the worse of the two to get wrong: it is the state nothing times
    out of, so a zombie parking a job somebody else is running would stop that
    deployment until a person noticed.
    """
    job_id = await queue.enqueue()
    zombie = await queue.claim("a")
    assert zombie is not None
    await queue.expire(job_id)
    assert await queue.claim("b") is not None

    async with queue.sessions() as session, session.begin():
        assert await jobs.park(session, job_id, zombie.fence) is False

    assert (await queue.read(job_id)).status == JobStatus.running.value


async def test_a_fenced_out_worker_learns_it_from_its_heartbeat(queue):
    """So it can stop, rather than spend ten more minutes on nobody's work."""
    job_id = await queue.enqueue()
    zombie = await queue.claim("a")
    assert zombie is not None
    await queue.expire(job_id)
    assert await queue.claim("b") is not None

    async with queue.sessions() as session, session.begin():
        assert await jobs.heartbeat(session, job_id, zombie.fence) is False


async def test_a_finished_job_cannot_be_reopened_by_its_own_fence(queue):
    """Once settled, the fence that settled it is spent.

    Otherwise a retried `_settle` — the same worker, the same fence, a dropped
    connection in between — could turn a `done` job back into something else.
    """
    job_id = await queue.enqueue()
    claimed = await queue.claim("a")
    assert claimed is not None

    async with queue.sessions() as session, session.begin():
        assert await jobs.finish(
            session, job_id, claimed.fence, status=JobStatus.done
        ) is True

    async with queue.sessions() as session, session.begin():
        assert await jobs.finish(
            session, job_id, claimed.fence, status=JobStatus.failed, error="no"
        ) is False

    assert (await queue.read(job_id)).status == JobStatus.done.value


# ---------------------------------------------------------------------------
# A job that is not survivable
# ---------------------------------------------------------------------------


async def test_a_job_that_kills_its_worker_every_time_eventually_fails(queue):
    """Reclaiming forever is a worse bug than giving up.

    A job that takes its worker down with it would otherwise be reclaimed, kill
    the next worker, be reclaimed again, and keep every worker in the fleet busy
    dying. It is given `MAX_ATTEMPTS` and then it is somebody's problem to read.
    """
    job_id = await queue.enqueue()
    for attempt in range(jobs.MAX_ATTEMPTS):
        claimed = await queue.claim(f"doomed-{attempt}")
        assert claimed is not None, f"gave up after {attempt} attempts"
        await queue.expire(job_id)

    assert await queue.claim("doomed-last") is None

    stored = await queue.read(job_id)
    assert stored.status == JobStatus.failed.value
    assert str(jobs.MAX_ATTEMPTS) in (stored.last_error or "")


async def test_giving_up_also_fences_out_the_worker_that_was_lost(queue):
    """The last worker to hold it is still out there and still has a fence."""
    job_id = await queue.enqueue()
    last = None
    for attempt in range(jobs.MAX_ATTEMPTS):
        last = await queue.claim(f"doomed-{attempt}")
        await queue.expire(job_id)
    assert last is not None
    assert await queue.claim("nobody") is None

    async with queue.sessions() as session, session.begin():
        assert await jobs.finish(
            session, job_id, last.fence, status=JobStatus.done
        ) is False


# ---------------------------------------------------------------------------
# The fence only ever goes up
# ---------------------------------------------------------------------------


async def test_parking_a_job_gives_up_its_lease(queue):
    """Nothing times a parked job out, so nothing may be holding a lease on it.

    A lease left behind on a `waiting` job would eventually expire and make the
    job claimable again, which is precisely the guess the pause exists to
    refuse.
    """
    job_id = await queue.enqueue()
    first = await queue.claim("a")
    assert first is not None

    async with queue.sessions() as session, session.begin():
        assert await jobs.park(session, job_id, first.fence) is True

    parked = await queue.read(job_id)
    assert parked.status == JobStatus.waiting.value
    assert parked.lease_expires_at is None
    assert parked.locked_by is None
    assert await queue.claim("b") is None


async def test_waking_a_parked_job_does_not_reset_the_fence(queue):
    """A builder answering a question must not revive a stale worker's licence.

    The pause is minutes or days long, and the worker that parked the job is
    long gone — but if `wake` put the fence back, anything that ever held this
    job would be able to write to it again.
    """
    job_id, deployment_id = await queue.enqueue_for_a_deployment()
    first = await queue.claim("a")
    assert first is not None
    async with queue.sessions() as session, session.begin():
        assert await jobs.park(session, job_id, first.fence) is True

    async with queue.sessions() as session, session.begin():
        assert await jobs.wake(session, deployment_id) == 1

    woken = await queue.read(job_id)
    assert woken.status == JobStatus.queued.value
    assert woken.fence == first.fence

    second = await queue.claim("b")
    assert second is not None
    assert second.fence == first.fence + 1

    # And the worker that parked it cannot report on it now that it has moved.
    async with queue.sessions() as session, session.begin():
        assert await jobs.finish(
            session, job_id, first.fence, status=JobStatus.done
        ) is False


# ---------------------------------------------------------------------------
# The same thing, through the loop a real worker runs
# ---------------------------------------------------------------------------


async def test_the_worker_loop_does_not_settle_a_job_it_has_lost(queue, monkeypatch):
    """End to end: `run_once` is where a zombie would actually do the damage.

    The handler here stands in for a worker that was paused long enough to be
    declared dead: while it is "working", the job is reclaimed by somebody else.
    It then returns a perfectly ordinary verdict, and the row must ignore it.
    """
    job_id = await queue.enqueue()

    async def steal(config, sessions, claimed_id):
        await queue.expire(claimed_id)
        stolen = await queue.claim("worker-b")
        assert stolen is not None and stolen.id == claimed_id
        return Verdict("done")

    monkeypatch.setitem(worker_main.HANDLERS, jobs.KIND_DEPLOY, steal)

    assert await worker_main.run_once(queue.config, queue.sessions, "worker-a")

    stored = await queue.read(job_id)
    assert stored.status == JobStatus.running.value
    assert stored.locked_by == "worker-b"


async def test_the_worker_loop_settles_a_job_it_still_holds(queue, monkeypatch):
    """The control for the test above: nothing clever, and it must still work."""
    job_id = await queue.enqueue()

    async def ordinary(config, sessions, claimed_id):
        return Verdict("done")

    monkeypatch.setitem(worker_main.HANDLERS, jobs.KIND_DEPLOY, ordinary)

    assert await worker_main.run_once(queue.config, queue.sessions, "worker-a")

    assert (await queue.read(job_id)).status == JobStatus.done.value


@pytest.mark.negative
async def test_the_fence_is_what_rejects_the_write(queue):
    """Proof the fence is doing the work and not the status check.

    `finish` could pass every test above by refusing to touch anything that is
    not `running` — which would be no protection at all, because a zombie's job
    *is* running, just for somebody else. So: same row, same `running` status,
    only the fence wrong.
    """
    job_id = await queue.enqueue()
    zombie = await queue.claim("a")
    assert zombie is not None
    await queue.expire(job_id)
    live = await queue.claim("b")
    assert live is not None
    assert (await queue.read(job_id)).status == JobStatus.running.value

    async with queue.sessions() as session, session.begin():
        assert await jobs.finish(
            session, job_id, zombie.fence, status=JobStatus.done
        ) is False
        assert await jobs.finish(
            session, job_id, live.fence, status=JobStatus.done
        ) is True
