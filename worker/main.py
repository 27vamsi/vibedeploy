"""The worker loop. Readme.md section 5.

Claim one job, do it, write down what happened. The claim is its own short
transaction that ends before any work starts, because a deployment takes minutes
and holding a row lock across a subprocess would turn one slow build into a
stuck queue.

That commit is also where the only lock on the job goes away, so two things run
alongside the handler for as long as it takes:

  - a **heartbeat**, renewing the lease, so that this worker being alive is a
    fact in the database and not an assumption;
  - the **fence** the claim handed out, carried through to the write at the end.
    If another worker has taken the job over in the meantime, that write lands
    nowhere and this worker's verdict is discarded — which is correct, because
    it is a verdict about work somebody else is now doing.
    `control_plane/jobs.py` is where that argument is made in full.

Three outcomes, and the difference between them is the point of the milestone:

  - **done** — the pipeline reached a verdict. That verdict may well be
    "blocked". A blocked deploy is a working deploy pipeline.
  - **park** — the pipeline stopped because it does not know something only a
    builder can tell it. Nothing retries this; nothing times it out into a
    default. It moves when somebody answers.
  - **failed** — we broke. The deployment is left in whatever safe state it was
    in, which is never `live`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import socket
import traceback
import uuid
from dataclasses import dataclass

from control_plane import jobs
from control_plane.config import ControlPlaneConfig
from control_plane.db import make_engine, make_sessionmaker
from control_plane.models import JobStatus
from worker import reconcile
from worker.pipeline import Sessions, Verdict, deploy, teardown, users

IDLE = 0.5

HANDLERS = {
    jobs.KIND_DEPLOY: deploy.handle,
    jobs.KIND_ADD_USER: users.handle,
    jobs.KIND_TEARDOWN: teardown.handle,
}


@dataclass(frozen=True)
class Claim:
    """What a worker leaves the claim transaction holding.

    The fence is the half that is easy to drop and is the half that matters:
    every write back to this job has to quote it.
    """

    job_id: uuid.UUID
    kind: str
    fence: int


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def _claim(sessions: Sessions, me: str) -> Claim | None:
    async with sessions() as session, session.begin():
        job = await jobs.claim(session, me)
        if job is None:
            return None
        return Claim(job_id=job.id, kind=job.kind, fence=job.fence)


async def _beat(sessions: Sessions, claim: Claim) -> None:
    """Say "still here" until cancelled, or until nobody is listening.

    Returning on a lost fence rather than raising: the handler is still running
    at that point, and tearing it down from here would leave whatever it was
    doing half done with no record of why. The settle at the end will be
    refused, which is the outcome either way.
    """
    while True:
        await asyncio.sleep(jobs.HEARTBEAT.total_seconds())
        async with sessions() as session, session.begin():
            if not await jobs.heartbeat(session, claim.job_id, claim.fence):
                return


async def _settle(sessions: Sessions, claim: Claim, verdict: Verdict) -> bool:
    """Record the verdict, if this worker is still the job's holder.

    False means it is not, and the verdict is dropped on purpose.
    """
    async with sessions() as session, session.begin():
        if verdict.action == "park":
            return await jobs.park(session, claim.job_id, claim.fence)
        status = (
            JobStatus.failed if verdict.action == "failed" else JobStatus.done
        )
        return await jobs.finish(
            session, claim.job_id, claim.fence, status=status, error=verdict.error
        )


async def run_once(config: ControlPlaneConfig, sessions: Sessions, me: str) -> bool:
    """Claim and run at most one job. False means the queue was empty."""
    claim = await _claim(sessions, me)
    if claim is None:
        return False

    handler = HANDLERS.get(claim.kind)
    if handler is None:
        await _settle(
            sessions, claim, Verdict("failed", error=f"unknown job {claim.kind}")
        )
        return True

    beat = asyncio.create_task(_beat(sessions, claim))
    try:
        verdict = await handler(config, sessions, claim.job_id)
    except Exception:  # noqa: BLE001 - one bad job must not stop the worker
        verdict = Verdict("failed", error=traceback.format_exc()[-4000:])
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
    await _settle(sessions, claim, verdict)
    return True


async def _jobs(config: ControlPlaneConfig, sessions: Sessions) -> None:
    """Claim, run, repeat. Expired leases are reclaimed by `claim` itself.

    No sweeper of its own: a sweeper that requeued a job and a worker that
    claimed it would be two statements racing over the same row, and `claim`
    taking the expired row under the same lock it takes a fresh one closes that
    by construction.
    """
    me = worker_id()
    while True:
        if not await run_once(config, sessions, me):
            await asyncio.sleep(IDLE)


async def serve(config: ControlPlaneConfig, sessions: Sessions) -> None:
    """The queue and the reconciler, together, for as long as this runs.

    Two loops rather than one pass folded into the other, because they answer
    different questions and must not be able to starve each other: the queue
    asks "is there work?", and the reconciler asks "is the world still what the
    database says?" — which has to keep being asked while a deploy that takes
    minutes is in the middle of being wrong.

    Safe to run side by side because of what the reconciler will not touch: it
    never writes to a job row, never grants traffic, and never stops a
    generation some live lease is holding. `worker/reconcile.py` makes that
    argument in full.
    """
    reconciler = asyncio.create_task(reconcile.serve(config, sessions))
    try:
        await _jobs(config, sessions)
    finally:
        # Same shape as the heartbeat above, for the same reason: a background
        # task left running after its owner has gone is a task holding database
        # connections nobody will ever close.
        reconciler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reconciler


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="vibedeploy worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single job and exit. Used by the tests.",
    )
    args = parser.parse_args(argv)

    config = ControlPlaneConfig.from_env()
    engine = make_engine(config)
    sessions = make_sessionmaker(engine)
    try:
        if args.once:
            await run_once(config, sessions, worker_id())
        else:
            with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
                await serve(config, sessions)
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
