"""The fence, extended past the database. Readme.md section 5, commitment 1.

`control_plane/jobs.py` fences writes to a job row: a worker that was declared
dead and wasn't cannot record a verdict, because its `UPDATE` quotes a fencing
token the row has moved past. That is enough for anything that lives in this
database and nothing that does not.

A deployment does not live only in this database. It starts processes, pulls
images, writes volumes, and — the one that matters — becomes the thing requests
are sent to. None of those can be rolled back by a failed `UPDATE`, and a stale
worker has no way to check first: between its check and its action it may have
been paused again.

So the honest claim is not "a stale worker does nothing". It is this:

    A stale worker can start a process. It cannot make that process the one
    traffic goes to, and what it leaves behind is labelled well enough to be
    found and reaped.

Both halves are here. `publish` is the only write in the system that grants
traffic, and it names the generation it was queued under, so a worker whose app
has moved on gets `False` and has granted nothing. `labels` is what goes on
every external resource a deployment creates, so that a reaper can tell *this*
app's generation 7 from its generation 6 without asking the worker that made it.

The generation is per app, not per deployment, because what is being fenced is a
single-valued thing: there is one answer to "where does traffic for this app
go?", and queueing a new deployment is the act of invalidating the old one.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models import App, Deployment, DeploymentStatus

# A deployment in one of these has had its answer written down, and `publish`
# will not overturn it. `live` is absent on purpose: a worker re-running the
# release it already finished must be able to arrive at the same answer twice.
_ABANDONED = frozenset(
    {
        DeploymentStatus.blocked.value,
        DeploymentStatus.superseded.value,
        DeploymentStatus.error.value,
    }
)

# The three labels every resource a deployment creates carries. Names rather
# than a prefix plus guesswork, because a reaper matches on them exactly.
APP_LABEL = "vd.app"
GENERATION_LABEL = "vd.generation"
DEPLOYMENT_LABEL = "vd.deployment"


def labels(*, app_id: str, generation: int, deployment_id: uuid.UUID | str) -> dict[str, str]:
    """What identifies a resource as belonging to one pass of one deployment.

    All three, always. The app alone cannot tell a replacement from the thing it
    replaces; the generation alone is meaningless across apps; and the
    deployment id is what an operator has in front of them in the dashboard.
    """
    return {
        APP_LABEL: app_id,
        GENERATION_LABEL: str(generation),
        DEPLOYMENT_LABEL: str(deployment_id),
    }


async def claim_generation(session: AsyncSession, app: App) -> int:
    """Invalidate whatever is live and return the number that replaces it.

    Called when a deployment is queued rather than when it goes live, which is
    the only ordering that works: the point is to fence out a worker that is
    *already running*, and by the time the replacement is ready to publish that
    worker may have published already.

    The cost of bumping early is that a deployment which then blocks has
    consumed a generation and left the app on a number nothing holds. That is
    harmless — nothing reads the generation except the comparison in `publish`,
    and the previous version keeps serving because nothing revoked its route.
    """
    app.desired_generation = (app.desired_generation or 0) + 1
    await session.flush()
    return app.desired_generation


async def publish(
    session: AsyncSession,
    *,
    app_pk: uuid.UUID,
    deployment_id: uuid.UUID,
    generation: int,
    endpoint: str,
    schema_graph: dict | None = None,
) -> bool:
    """Point this app's traffic at this deployment. `False` means: not yours.

    The only place in the system that grants traffic, which is what makes the
    claim above checkable: there is one write to audit, and it is fenced.

    The guard is an `UPDATE ... WHERE desired_generation = :generation`, not a
    read of the column followed by a write. A read and a write have a gap
    between them, and the gap is exactly how long a stale worker needs.

    Returning `False` rather than raising, for the same reason `jobs.finish`
    does: being replaced is not an error, it is a thing that happens to slow
    workers, and the caller's job is to tidy up rather than to report a failure
    nobody can act on.
    """
    # A verdict already written is not reopened. The generation fence catches a
    # worker whose app has moved on, and this catches the other way a worker
    # can be out of date: nothing newer was queued, but somebody — a person, or
    # the reconciler — has already said this attempt is over. Without it a
    # worker that was gone long enough to be given up on could come back and
    # put a version live that the dashboard says was blocked.
    deployment = await session.get(Deployment, deployment_id)
    if deployment is None or deployment.status in _ABANDONED:
        return False

    granted = await session.execute(
        update(App)
        .where(App.id == app_pk, App.desired_generation == generation)
        .values(
            live_deployment_id=deployment_id,
            status=DeploymentStatus.live.value,
        )
    )
    if not granted.rowcount:
        return False

    deployment.status = DeploymentStatus.live.value
    deployment.blocked_reason = None
    deployment.endpoint = endpoint
    # Only now, when the live schema has matched the hash the rules were proved
    # against. An agent's allowlist is read from here, so it must never describe
    # a schema that was rejected.
    if schema_graph is not None:
        deployment.schema_graph = schema_graph

    # Whatever was live is now not. Done inside the same transaction as the
    # grant, so there is no instant at which two deployments of one app both
    # claim to be live.
    superseded = (
        (
            await session.execute(
                select(Deployment).where(
                    Deployment.app_id == app_pk,
                    Deployment.status == DeploymentStatus.live.value,
                    Deployment.id != deployment_id,
                )
            )
        )
        .scalars()
        .all()
    )
    for old in superseded:
        old.status = DeploymentStatus.superseded.value
    return True


async def live_generation(session: AsyncSession, app_pk: uuid.UUID) -> int | None:
    """The generation of whatever currently has this app's traffic.

    What a reconciler compares the world against: anything labelled with a
    different number is a leftover, whether it is older (a replaced version) or
    newer (a worker that started something and then lost the right to publish
    it).
    """
    app = await session.get(App, app_pk)
    if app is None or app.live_deployment_id is None:
        return None
    live = await session.get(Deployment, app.live_deployment_id)
    return None if live is None else live.generation
