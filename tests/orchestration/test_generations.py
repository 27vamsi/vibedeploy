"""A stale worker and the one thing it must not be able to do.

`tests/orchestration/test_leases.py` proves the fence over the job row: a worker
that was declared dead and wasn't cannot write a verdict. That is the whole
story for state that lives in the control plane's database, and none of the
story for state that does not.

A deployment starts processes. It pulls an image, writes a volume, binds a port.
A failed `UPDATE` does not undo any of that, and the worker cannot avoid it by
checking first — between the check and the action it may have been paused again,
which is how it got into this position to begin with. So "a stale worker does
nothing" is not a claim anybody can keep, and the tests below do not try to
prove it.

What is proved here is the narrower claim, which is the one that matters:

  - a stale worker **cannot publish a route**, so its process runs and nothing
    is ever sent to it;
  - it cannot take the live version down on its way to being refused;
  - what it leaves behind carries enough labelling to be found and reaped,
    without asking the worker that made it.

The mechanism is `apps.desired_generation`: bumped once per deployment queued,
carried by the deployment, and named in the `WHERE` of the single write in the
system that grants traffic. `control_plane/generations.py` is that write.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import uuid

import pytest

from control_plane import generations, service
from control_plane.models import App, Deployment, DeploymentStatus
from worker.pipeline import container
from worker.runtimes import local

REPO = "octocat/todo"
ENDPOINT = "http://127.0.0.1:9999"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


async def queue_one(queue, app: App, ref: str = "main") -> Deployment:
    """Queue a deployment the way the dashboard and the webhook both do."""
    async with queue.sessions() as session, session.begin():
        fresh = await session.get(App, app.id)
        return await service.start_deployment(session, app=fresh, ref=ref)


async def publish(
    queue, *, app: App, deployment: Deployment, generation: int | None = None
) -> bool:
    async with queue.sessions() as session, session.begin():
        return await generations.publish(
            session,
            app_pk=app.id,
            deployment_id=deployment.id,
            generation=deployment.generation if generation is None else generation,
            endpoint=ENDPOINT,
        )


async def read_app(queue, app: App) -> App:
    async with queue.sessions() as session:
        return await session.get(App, app.id)


async def read_deployment(queue, deployment: Deployment) -> Deployment:
    async with queue.sessions() as session:
        return await session.get(Deployment, deployment.id)


# ---------------------------------------------------------------------------
# The generation itself
# ---------------------------------------------------------------------------


async def test_queueing_a_deployment_bumps_the_generation(queue):
    """Bumped on the way in, which is the only ordering that fences anybody.

    Bumping at cutover would be too late: the worker being fenced out is
    already running, and may reach its own cutover first.
    """
    app = await queue.make_app(repo=REPO)
    assert (await read_app(queue, app)).desired_generation == 0

    first = await queue_one(queue, app)
    second = await queue_one(queue, app)

    assert first.generation == 1
    assert second.generation == 2
    assert (await read_app(queue, app)).desired_generation == 2


async def test_a_deployment_remembers_the_generation_it_was_queued_under(queue):
    """Carried, not re-read. A worker re-reading it would always agree."""
    app = await queue.make_app(repo=REPO)
    deployment = await queue_one(queue, app)
    await queue_one(queue, app)

    assert (await read_deployment(queue, deployment)).generation == 1
    assert (await read_app(queue, app)).desired_generation == 2


async def test_generations_are_not_shared_between_apps(queue):
    """One answer to "where does this app's traffic go", per app."""
    one = await queue.make_app(repo=REPO)
    other = await queue.make_app(repo="octocat/other")

    await queue_one(queue, one)
    await queue_one(queue, one)
    await queue_one(queue, other)

    assert (await read_app(queue, one)).desired_generation == 2
    assert (await read_app(queue, other)).desired_generation == 1


# ---------------------------------------------------------------------------
# Publishing: the one write that grants traffic
# ---------------------------------------------------------------------------


async def test_publishing_points_the_app_at_the_deployment(queue):
    """The baseline. Without this the rest would pass by doing nothing."""
    app = await queue.make_app(repo=REPO)
    deployment = await queue_one(queue, app)

    assert await publish(queue, app=app, deployment=deployment) is True

    stored = await read_app(queue, app)
    assert stored.live_deployment_id == deployment.id
    assert stored.status == DeploymentStatus.live.value
    live = await read_deployment(queue, deployment)
    assert live.status == DeploymentStatus.live.value
    assert live.endpoint == ENDPOINT


async def test_publishing_supersedes_whatever_was_live(queue):
    """Two versions of one app may run. Only one may be live."""
    app = await queue.make_app(repo=REPO)
    old = await queue_one(queue, app)
    assert await publish(queue, app=app, deployment=old) is True
    new = await queue_one(queue, app)

    assert await publish(queue, app=app, deployment=new) is True

    assert (await read_deployment(queue, old)).status == (
        DeploymentStatus.superseded.value
    )
    assert (await read_deployment(queue, new)).status == DeploymentStatus.live.value
    assert (await read_app(queue, app)).live_deployment_id == new.id


async def test_a_stale_generation_cannot_publish(queue):
    """The one that matters.

    Worker `a` is slow, not dead. While it was proving its deployment safe, a
    second push queued another one, so the app has moved on. `a` then finishes
    and tries to send traffic to code nobody asked to be live any more.
    """
    app = await queue.make_app(repo=REPO)
    slow = await queue_one(queue, app)
    await queue_one(queue, app)

    assert await publish(queue, app=app, deployment=slow) is False

    stored = await read_app(queue, app)
    assert stored.live_deployment_id is None
    assert (await read_deployment(queue, slow)).endpoint is None


async def test_a_stale_publish_leaves_the_live_version_alone(queue):
    """Being refused must be inert, not destructive.

    The dangerous failure is not a stale worker that fails to publish, it is one
    that fails to publish *and* takes the serving version with it on the way
    out. That is a self-inflicted outage caused by the safety mechanism.
    """
    app = await queue.make_app(repo=REPO)
    serving = await queue_one(queue, app)
    assert await publish(queue, app=app, deployment=serving) is True
    # A third deployment is queued and abandoned, which moves the app past both
    # the serving one and the one the zombie holds.
    zombie = await queue_one(queue, app)
    await queue_one(queue, app)

    assert await publish(queue, app=app, deployment=zombie) is False

    stored = await read_app(queue, app)
    assert stored.live_deployment_id == serving.id
    assert stored.status == DeploymentStatus.live.value
    assert (await read_deployment(queue, serving)).status == (
        DeploymentStatus.live.value
    )
    assert (await read_deployment(queue, serving)).endpoint == ENDPOINT


async def test_two_workers_publishing_at_once_leave_one_live_deployment(queue):
    """Sequential tests cannot see this one, and it is the realistic timing.

    Two deployments reaching their cutover in the same instant is exactly what a
    builder pushing twice in a minute produces.
    """
    app = await queue.make_app(repo=REPO)
    first = await queue_one(queue, app)
    second = await queue_one(queue, app)

    results = await asyncio.gather(
        publish(queue, app=app, deployment=first),
        publish(queue, app=app, deployment=second),
    )

    assert sorted(results) == [False, True]
    assert (await read_app(queue, app)).live_deployment_id == second.id
    assert (await read_deployment(queue, first)).status != DeploymentStatus.live.value


async def test_republishing_the_current_generation_is_allowed(queue):
    """A retried cutover is not a stale one, and must not be refused.

    The generation is a fence, not a one-shot token: a worker whose connection
    dropped between the `UPDATE` and the commit has to be able to try again. It
    is `jobs.fence` that makes the *job* settle once.
    """
    app = await queue.make_app(repo=REPO)
    deployment = await queue_one(queue, app)

    assert await publish(queue, app=app, deployment=deployment) is True
    assert await publish(queue, app=app, deployment=deployment) is True

    assert (await read_app(queue, app)).live_deployment_id == deployment.id
    assert (await read_deployment(queue, deployment)).status == (
        DeploymentStatus.live.value
    )


async def test_live_generation_reports_what_holds_the_traffic(queue):
    """What a reaper compares the machine against."""
    app = await queue.make_app(repo=REPO)
    async with queue.sessions() as session:
        assert await generations.live_generation(session, app.id) is None

    deployment = await queue_one(queue, app)
    await publish(queue, app=app, deployment=deployment)
    await queue_one(queue, app)

    async with queue.sessions() as session:
        # The app's *desired* generation has moved to 2, and the answer is still
        # 1, because that is the one requests are going to.
        assert await generations.live_generation(session, app.id) == 1


@pytest.mark.negative
async def test_the_generation_is_what_rejects_the_publish(queue):
    """Proof the fence is doing the work, not some other check.

    `publish` could pass every test above by refusing anything that is not the
    newest deployment, or anything already superseded — neither of which is a
    fence, and neither of which would survive a worker being paused at the wrong
    moment. So: same row, same status, only the generation wrong, and then the
    same row again with it right.
    """
    app = await queue.make_app(repo=REPO)
    deployment = await queue_one(queue, app)
    assert deployment.generation == 1

    assert await publish(queue, app=app, deployment=deployment, generation=2) is False
    assert await publish(queue, app=app, deployment=deployment, generation=0) is False
    assert await publish(queue, app=app, deployment=deployment, generation=1) is True


# ---------------------------------------------------------------------------
# Labels: what makes a leftover findable
# ---------------------------------------------------------------------------


def test_a_label_names_the_app_the_generation_and_the_deployment():
    """All three. Any two of them cannot identify a leftover.

    The app alone cannot tell a replacement from the thing it replaced; the
    generation alone means nothing across apps; and the deployment id is the one
    an operator already has in front of them.
    """
    deployment_id = uuid.uuid4()
    stamped = generations.labels(
        app_id="app_b1", generation=7, deployment_id=deployment_id
    )

    assert stamped == {
        "vd.app": "app_b1",
        "vd.generation": "7",
        "vd.deployment": str(deployment_id),
    }


def test_a_build_container_carries_the_three_labels():
    """Read off the argv, because the argv is the whole of the guarantee."""
    stamped = generations.labels(
        app_id="app_b1", generation=7, deployment_id="d-1"
    )
    argv = container.docker_run_argv(
        image="vibedeploy/buildjob:local",
        command=["python", "-m", "buildjob.run"],
        mounts={},
        env={},
        labels=stamped,
    )

    pairs = [argv[i + 1] for i, word in enumerate(argv) if word == "--label"]
    assert sorted(pairs) == [
        "vd.app=app_b1",
        "vd.deployment=d-1",
        "vd.generation=7",
    ]


# ---------------------------------------------------------------------------
# Reaping: what makes a leftover temporary
# ---------------------------------------------------------------------------


def _sleeper() -> subprocess.Popen:
    """A real process to kill, because killing a made-up pid proves nothing."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _record(config, app_id: str, generation: int, process: subprocess.Popen) -> None:
    """Exactly what the local runtime's `start` writes, without a real app.

    An app and a login gate would add a minute per test and nothing to the
    argument: what is under test is whether the right generation's pids are
    signalled, and a sleeping process has pids too.
    """
    path = config.state_root / "running" / app_id / f"{generation}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "app_id": app_id,
                "endpoint": ENDPOINT,
                "upstream": "127.0.0.1:2",
                "app_port": 1,
                "sidecar_port": 2,
                "app_pid": process.pid,
                "sidecar_pid": process.pid,
                "generation": generation,
                "deployment_id": "d-1",
            }
        ),
        encoding="utf-8",
    )


def _died(process: subprocess.Popen, within: float = 10.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return True
        time.sleep(0.05)
    return False


async def test_the_machine_reports_which_generations_it_is_running(queue):
    """The "actual" half of desired-versus-actual, read off the filesystem.

    Off the filesystem rather than out of the database on purpose: asking the
    database what is running would make the comparison agree with itself.
    """
    app_id = f"app_{uuid.uuid4().hex[:8]}"
    processes = [_sleeper(), _sleeper()]
    try:
        _record(queue.config, app_id, 6, processes[0])
        _record(queue.config, app_id, 7, processes[1])

        assert await local.generations(queue.config, app_id) == [6, 7]
    finally:
        for process in processes:
            process.kill()


async def test_an_app_that_has_never_run_here_reports_nothing(queue):
    assert await local.generations(queue.config, f"app_{uuid.uuid4().hex[:8]}") == []


async def test_reaping_stops_every_generation_but_the_live_one(queue):
    """A stale worker's process is temporary, and the live one is not touched."""
    app_id = f"app_{uuid.uuid4().hex[:8]}"
    old, live, orphan = _sleeper(), _sleeper(), _sleeper()
    try:
        _record(queue.config, app_id, 5, old)
        _record(queue.config, app_id, 6, live)
        # Started by a worker that then found out it could not publish, so its
        # generation is *newer* than the live one and still has to go.
        _record(queue.config, app_id, 7, orphan)

        reaped = await local.reap(queue.config, app_id, keep={6})

        assert reaped == [5, 7]
        assert _died(old)
        assert _died(orphan)
        assert live.poll() is None
        assert await local.generations(queue.config, app_id) == [6]
    finally:
        for process in (old, live, orphan):
            process.kill()


async def test_reaping_with_nothing_live_stops_everything(queue):
    """No live deployment means no process on this machine is wanted.

    The state after a deploy that blocked on its first ever attempt: something
    may have been started, and nothing in the database points at it.
    """
    app_id = f"app_{uuid.uuid4().hex[:8]}"
    process = _sleeper()
    try:
        _record(queue.config, app_id, 1, process)

        assert await local.reap(queue.config, app_id, keep=set()) == [1]

        assert _died(process)
        assert await local.generations(queue.config, app_id) == []
    finally:
        process.kill()


async def test_stopping_one_generation_leaves_the_other_running(queue):
    """Two versions of an app running at once is the normal case from here on.

    It is what a health-gated cutover needs, and it is only possible because the
    record is per generation: one record per app would have the second start
    overwrite the first, and then nothing on this machine knows the pids of the
    version that is still serving.
    """
    app_id = f"app_{uuid.uuid4().hex[:8]}"
    first, second = _sleeper(), _sleeper()
    try:
        _record(queue.config, app_id, 1, first)
        _record(queue.config, app_id, 2, second)

        await local.stop(queue.config, app_id, 1)

        assert _died(first)
        assert second.poll() is None
        assert await local.generations(queue.config, app_id) == [2]
    finally:
        for process in (first, second):
            process.kill()


async def test_stopping_an_app_with_no_generation_named_stops_all_of_them(queue):
    """What the test harness and an operator both want: everything, gone."""
    app_id = f"app_{uuid.uuid4().hex[:8]}"
    first, second = _sleeper(), _sleeper()
    try:
        _record(queue.config, app_id, 1, first)
        _record(queue.config, app_id, 2, second)

        await local.stop(queue.config, app_id)

        assert _died(first)
        assert _died(second)
        assert await local.generations(queue.config, app_id) == []
    finally:
        for process in (first, second):
            process.kill()
