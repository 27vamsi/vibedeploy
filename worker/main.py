"""The worker loop. Readme.md section 5.

Claim one job, do it, write down what happened. The claim is its own short
transaction that ends before any work starts, because a deployment takes minutes
and holding a row lock across a subprocess would turn one slow build into a
stuck queue.

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

from control_plane import jobs
from control_plane.config import ControlPlaneConfig
from control_plane.db import make_engine, make_sessionmaker
from control_plane.models import Job, JobStatus
from worker.pipeline import Sessions, Verdict, deploy, users

IDLE = 0.5

HANDLERS = {
    jobs.KIND_DEPLOY: deploy.handle,
    jobs.KIND_ADD_USER: users.handle,
}


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def _claim(sessions: Sessions, me: str) -> tuple[uuid.UUID, str] | None:
    async with sessions() as session, session.begin():
        job = await jobs.claim(session, me)
        if job is None:
            return None
        return job.id, job.kind


async def _settle(sessions: Sessions, job_id: uuid.UUID, verdict: Verdict) -> None:
    async with sessions() as session, session.begin():
        job = await session.get(Job, job_id)
        if verdict.action == "park":
            await jobs.park(session, job)
        elif verdict.action == "failed":
            await jobs.finish(
                session, job, status=JobStatus.failed, error=verdict.error
            )
        else:
            await jobs.finish(
                session, job, status=JobStatus.done, error=verdict.error
            )


async def run_once(config: ControlPlaneConfig, sessions: Sessions, me: str) -> bool:
    """Claim and run at most one job. False means the queue was empty."""
    claimed = await _claim(sessions, me)
    if claimed is None:
        return False
    job_id, kind = claimed

    handler = HANDLERS.get(kind)
    if handler is None:
        await _settle(sessions, job_id, Verdict("failed", error=f"unknown job {kind}"))
        return True

    try:
        verdict = await handler(config, sessions, job_id)
    except Exception:  # noqa: BLE001 - one bad job must not stop the worker
        verdict = Verdict("failed", error=traceback.format_exc()[-4000:])
    await _settle(sessions, job_id, verdict)
    return True


async def serve(config: ControlPlaneConfig, sessions: Sessions) -> None:
    me = worker_id()
    while True:
        async with sessions() as session, session.begin():
            await jobs.reclaim_expired(session)
        if not await run_once(config, sessions, me):
            await asyncio.sleep(IDLE)


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
