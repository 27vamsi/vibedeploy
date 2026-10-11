"""The controller loop: close the gap between what should be and what is.

Every checkpoint before this one made a deployment correct while a worker was
alive to run it. This one is about the worker not being alive. A deploy is a
sequence of steps against four things that do not roll back together — a
database, processes on a laptop, Docker, and a proxy's config — so a worker
that stops existing part way through does not leave a mess that is wrong, it
leaves one that is *stale*: the right answer is in the control plane database,
and the world has not caught up with it.

So this does not try to undo anything. It states the answer again:

  - **processes.** For each app, the database says which generation has the
    traffic. Anything else running for that app is stopped — a replaced version
    whose drain never happened, or a candidate a worker started and then lost
    the right to publish. Except for generations some *live* worker is holding,
    which is the one thing a reaper must not take: a candidate being watched by
    a health gate is not a leftover, and killing it would make this loop the
    cause of the outage it exists to prevent.
  - **routes.** The whole route table is written again from the database, every
    pass. Not diffed — stated. That is what makes a hand-edited proxy, or a
    stale worker's write, or a Caddy that was restarted empty, all the same
    problem with the same fix.
  - **stranded deployments.** A deployment in `building`, `verifying` or
    `deploying` with no worker holding its job is going nowhere by itself. Some
    of those can be resumed by the queue and some cannot, and an unspent build
    token is what tells them apart — see `_unstick`.

**Why the comparison is not vacuous.** Desired comes from the control plane
database. Actual comes from the runtime (`serving.apps`, `serving.generations`)
and from the proxy's own config (`proxy.published`). Nothing here asks the
database what is running, which would make every pass agree with itself.

**Why it is safe to run next to the worker.** Nothing in here writes to a job
row, grants traffic, or starts an app. The only writes are: stopping a process
that nothing claims, restating a route table the database already decided, and
recording a verdict on a deployment nobody is working on. Two of those are
idempotent by construction and the third is guarded by the same "a verdict is
written once" check the pipeline uses.

**What it does not do.** It will not start an app. If the database says a
generation is live and nothing is listening for it, that is reported as `lost`
and left alone: restarting it means re-running the app's migrations and
re-applying its policies, which is a deployment, and a deployment is a thing a
person asks for. Reviving apps after the machine they ran on rebooted is
therefore out of scope for V0.5, and the reconciler says so out loud rather
than looking like it covers it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from control_plane import tokens
from control_plane.config import ControlPlaneConfig
from control_plane.models import (
    App,
    Deployment,
    DeploymentStatus,
    Job,
    JobStatus,
    TERMINAL_DEPLOYMENT_STATUSES,
)
from worker.runtimes import proxy, serving

log = logging.getLogger("vibedeploy_worker")

# Statuses a deployment only passes through while a worker is working on it. A
# deployment sitting in one of these with nobody on it is the thing this loop
# is looking for.
TRANSIENT = (
    DeploymentStatus.building.value,
    DeploymentStatus.verifying.value,
    DeploymentStatus.deploying.value,
)

_TERMINAL = frozenset(status.value for status in TERMINAL_DEPLOYMENT_STATUSES)

# A job in one of these could still move its deployment: `queued` will be
# claimed, and `running` will either be heartbeat or have its lease expire and
# be reclaimed. `waiting` is not here on purpose — a parked job moves when a
# person answers, and a person cannot answer a question about a build that is
# no longer running.
_RUNNABLE = frozenset({JobStatus.queued.value, JobStatus.running.value})

STRANDED = (
    "A build job was proving this deployment and stopped without ever"
    " reporting, so nothing was deployed and nothing about your database was"
    " changed. Deploy again and it will be proved from the beginning. The"
    " version that was already live is still serving."
)

NO_WORKER = (
    "This deployment stopped part way through and there is no job left to"
    " finish it, so nothing was deployed. Deploy again. The version that was"
    " already live is still serving."
)


@dataclass(frozen=True)
class Drift:
    """What one pass found, and what it did about it.

    Returned rather than only logged so that the thing a reconciler is hardest
    to trust about — that a second pass over a healthy system does nothing — is
    a value a test can compare.
    """

    # app_id -> the generation that is live and was found already serving. The
    # orphan case: left exactly alone, never restarted.
    adopted: dict[str, int] = field(default_factory=dict)
    # app_id -> generations stopped because nothing claimed them.
    reaped: dict[str, list[int]] = field(default_factory=dict)
    # app_id -> the generation the database calls live, with nothing listening.
    lost: dict[str, int] = field(default_factory=dict)
    # The hosts published, in the order the proxy now holds them.
    routes: list[str] = field(default_factory=list)
    # Deployments given the verdict nobody was left to give them.
    unstuck: list[str] = field(default_factory=list)
    # The proxy could not be reached, so the route table is whatever it was.
    # Reported, not raised: a laptop with Caddy stopped still wants its
    # leftover processes reaped.
    proxy_unavailable: bool = False

    @property
    def changed(self) -> bool:
        """Did this pass alter anything?

        The route table is restated every pass by design, so writing it is not
        a change; writing something *different* would be, and the only way that
        happens is if one of the other fields moved.
        """
        return bool(self.reaped or self.unstuck)


async def once(config: ControlPlaneConfig, sessions: async_sessionmaker) -> Drift:
    """One pass. Everything in it is safe to repeat."""
    live, held = await _wanted(sessions)

    adopted: dict[str, int] = {}
    reaped: dict[str, list[int]] = {}
    lost: dict[str, int] = {}

    for app_id in sorted(set(await serving.apps(config)) | set(live)):
        wanted = live.get(app_id)
        keep = set(held.get(app_id, set()))
        if wanted is not None:
            keep.add(wanted)

        stopped = await serving.reap(config, app_id, keep=keep)
        if stopped:
            reaped[app_id] = stopped

        if wanted is None:
            continue
        if await serving.adopt(config, app_id, wanted) is not None:
            adopted[app_id] = wanted
        else:
            lost[app_id] = wanted

    unstuck = await _unstick(sessions)

    # Last, because it reads the run records the reaping above has just
    # corrected. Reasserting first would publish a route to a process this
    # same pass was about to stop.
    routes: list[str] = []
    unavailable = False
    try:
        routes = [route.host for route in await proxy.reassert(config, sessions)]
    except proxy.ProxyUnavailable:
        unavailable = True

    return Drift(
        adopted=adopted,
        reaped=reaped,
        lost=lost,
        routes=routes,
        unstuck=unstuck,
        proxy_unavailable=unavailable,
    )


async def _wanted(
    sessions: async_sessionmaker,
) -> tuple[dict[str, int], dict[str, set[int]]]:
    """The two reasons a generation is allowed to be running.

    `live` is the one the database has given the traffic to. `held` is every
    generation some worker is at this moment working on, which is not in the
    desired state of anything and still must not be touched — it is the
    difference between a controller and a race.
    """
    now = datetime.now(timezone.utc)
    async with sessions() as session:
        rows = (
            await session.execute(
                select(App.app_id, Deployment.generation).join(
                    Deployment, Deployment.id == App.live_deployment_id
                )
            )
        ).all()
        live = {app_id: generation for app_id, generation in rows}

        busy = (
            await session.execute(
                select(App.app_id, Deployment.generation)
                .join(Deployment, Deployment.app_id == App.id)
                .join(Job, Job.deployment_id == Deployment.id)
                .where(
                    Job.status == JobStatus.running.value,
                    Job.lease_expires_at > now,
                    Deployment.status.not_in(_TERMINAL),
                )
            )
        ).all()

    held: dict[str, set[int]] = {}
    for app_id, generation in busy:
        held.setdefault(app_id, set()).add(generation)
    return live, held


async def _unstick(sessions: async_sessionmaker) -> list[str]:
    """Give a verdict to deployments that nobody is left to give one to.

    Three states look the same in a `deployments` row and are not the same
    thing, so each gets asked for separately:

      - **a worker is on it.** Its job is `running` with a lease that has not
        expired. Left completely alone; this loop is not entitled to an opinion
        about work in progress.
      - **a build is in flight and its worker is gone.** There is an unspent
        build token, which means a build job was handed a one-shot callback and
        the only process waiting for it has stopped existing. Nothing will ever
        spend that token: not the queue, because a second `plan` cannot be run
        over the first one's saved access model, and not a person. So this is
        **blocked**, which is a verdict a builder can act on, and the next
        deploy proves it from the beginning.
      - **nothing was in flight.** The deployment is somewhere the pipeline can
        pick up again — `verifying` straight after a confirmation, or
        `deploying` part way through a release — so it is left for the queue,
        which resumes it when the lease expires. Unless there is no job left in
        a state that can be claimed, in which case waiting would be waiting for
        ever.
    """
    moved = []
    async with sessions() as session, session.begin():
        stranded = (
            (
                await session.execute(
                    select(Deployment, Job)
                    .outerjoin(Job, Job.deployment_id == Deployment.id)
                    .where(Deployment.status.in_(TRANSIENT))
                )
            )
            .all()
        )
        now = datetime.now(timezone.utc)
        for deployment, job in stranded:
            if _busy(job, now):
                continue
            if await tokens.outstanding(session, deployment.id):
                reason = STRANDED
            elif job is None or job.status not in _RUNNABLE:
                reason = NO_WORKER
            else:
                continue
            deployment.status = DeploymentStatus.blocked.value
            deployment.blocked_reason = reason
            moved.append(str(deployment.id))
    return moved


def _busy(job: Job | None, now: datetime) -> bool:
    """Is a worker holding this job right now?

    The same question `jobs.claim` asks before reclaiming an expired lease, and
    deliberately the same answer: a lease is the only thing that says a worker
    exists, because a worker that has stopped existing cannot tell anybody.
    """
    if job is None or job.status != JobStatus.running.value:
        return False
    return job.lease_expires_at is not None and job.lease_expires_at > now


async def serve(
    config: ControlPlaneConfig,
    sessions: async_sessionmaker,
    *,
    interval: float | None = None,
) -> None:
    """Run `once` for ever. Never raises; a reconciler that gave up is a lie.

    One pass failing is normal — the proxy is restarting, the database is busy,
    a record is half written — and none of those are reasons to stop converging
    on the next pass. The exception is logged rather than swallowed silently,
    because a loop that fails every pass and says nothing is the worst of the
    available behaviours.
    """
    wait = config.reconcile_interval if interval is None else interval
    while True:
        try:
            drift = await once(config, sessions)
        except Exception:  # noqa: BLE001 - see above
            log.exception("a reconcile pass failed")
        else:
            if drift.changed:
                log.info(
                    "reconciled: reaped %s, unstuck %s",
                    drift.reaped,
                    drift.unstuck,
                )
        await asyncio.sleep(wait)
