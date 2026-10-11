"""How long an app's own packages live, which is as long as the app does.

The build job installs the app's `requirements.txt` into a volume named after
the deployment, and — once the app runs in a container rather than on the
worker's interpreter — the serving process *imports from that volume for as
long as it is serving*. That makes the volume's lifetime a property of the
running app, not of the deploy that created it, and those two differ by exactly
one status: `live` is terminal for the **job** and the beginning for the **app**.

So there are two claims here, and they are opposite:

  - a deployment that ended in a verdict nobody is serving has its packages
    thrown away, because a volume per deploy is a disk leak;
  - a deployment that ended *live* keeps them, because discarding them would be
    the tidying-up taking down the app whose deploy just succeeded.

No Docker and no deploy: `discard_deps` is replaced by a recorder, so what is
under test is the decision rather than `docker volume rm`. The decision is the
part that was wrong.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import update

from control_plane.models import Deployment, DeploymentStatus
from worker.pipeline import build, deploy

pytestmark = pytest.mark.asyncio


async def _set_status(queue, deployment_id: uuid.UUID, status: str) -> None:
    async with queue.sessions() as session, session.begin():
        await session.execute(
            update(Deployment)
            .where(Deployment.id == deployment_id)
            .values(status=status)
        )


async def _discarded(queue, monkeypatch, status: str) -> list[str]:
    """Which volumes the end-of-job tidy-up would drop, at this status."""
    _, deployment_id = await queue.enqueue_for_a_deployment()
    await _set_status(queue, deployment_id, status)

    dropped: list[str] = []
    monkeypatch.setattr(
        deploy, "discard_deps", lambda workdir: _record(dropped, workdir)
    )
    await deploy._discard_deps_if_finished(
        queue.config, queue.sessions, deployment_id
    )
    return dropped


async def _record(dropped: list[str], workdir) -> None:
    dropped.append(build.deps_volume(workdir).name)


async def test_a_live_deployments_packages_are_not_discarded(queue, monkeypatch):
    """The bug this file exists for.

    `live` is in `TERMINAL_DEPLOYMENT_STATUSES` — correctly, since the job is
    finished — and the discard was keyed on terminality alone. It was harmless
    only for as long as the app ran on the worker's interpreter and never read
    the volume at all. The moment the app imports from it, this one line is the
    difference between a successful deploy and a successful deploy that kills
    itself a second later.
    """
    assert await _discarded(queue, monkeypatch, DeploymentStatus.live.value) == []


@pytest.mark.parametrize(
    "status",
    [
        DeploymentStatus.blocked.value,
        DeploymentStatus.error.value,
        DeploymentStatus.superseded.value,
    ],
)
async def test_a_finished_deployment_nobody_serves_drops_its_packages(
    queue, monkeypatch, status
):
    """The other half, so the fix above cannot be "never discard anything".

    Each of these is an end nobody is serving: blocked at the gate, failed
    outright, or replaced by a later version. Twenty thousand files per deploy
    is not a cache, it is a leak.
    """
    assert len(await _discarded(queue, monkeypatch, status)) == 1


@pytest.mark.parametrize(
    "status",
    [
        DeploymentStatus.queued.value,
        DeploymentStatus.building.value,
        DeploymentStatus.awaiting_confirmation.value,
    ],
)
async def test_an_unfinished_deployment_keeps_its_packages(
    queue, monkeypatch, status
):
    """A pause is not an ending.

    `awaiting_confirmation` is the one that matters: the job is done, the
    deployment is not, and the pass after the builder answers installs into the
    same volume. Discarding here would make a confirmed deploy pay for the same
    packages twice.
    """
    assert await _discarded(queue, monkeypatch, status) == []


async def test_the_volume_is_named_after_the_deployment_and_nothing_else(queue):
    """Which is what makes the lifetime above expressible at all.

    One volume per deployment means all three passes of one deploy install
    once, and means two apps' builds share nothing — a wheel cache both could
    write to would be a channel from one customer's migrations into another's.
    """
    one, two = uuid.uuid4(), uuid.uuid4()
    assert build.deps_volume(deploy._workdir(queue.config, one)).name == (
        f"vd-deps-{one}"
    )
    assert build.deps_volume(deploy._workdir(queue.config, two)).name != (
        build.deps_volume(deploy._workdir(queue.config, one)).name
    )
