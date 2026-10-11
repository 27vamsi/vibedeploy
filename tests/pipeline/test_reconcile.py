"""A worker killed part way through leaves something a loop can fix. CP6.

Every checkpoint before this one made a deployment correct *while a worker was
alive to run it*. This file is about the worker not being alive, and the claim
it buys is narrow and worth stating exactly:

    A worker that stops existing at any point in a deploy leaves a system that
    returns to a correct state with nobody looking at it — except for one case
    the reconciler reports out loud instead of pretending to cover.

That exception is near the bottom of the file: an app whose live generation has
stopped listening is reported `lost` and **not** restarted, because restarting
it means re-running the app's migrations and re-applying its policies, and that
is a deployment. See `worker/reconcile.py`.

**How a kill is simulated.** `run_once` and `_dispatch` both catch `Exception`,
which is what makes a worker survive a bad job — so an ordinary exception here
would be *handled*, and a handled failure is the opposite of the thing being
tested. `Killed` is a `BaseException`, so it escapes both and the settle at the
end of `run_once` never happens: the job is left `running` with a lease, which
is precisely the row a killed worker leaves behind. The lease is then expired by
hand rather than waited out, because `jobs.LEASE` is 60 seconds and what is
being tested is not the clock.

**Why so few apps for so many stages.** Each deployment gets its own job row
with its own `attempts`, and `jobs.MAX_ATTEMPTS` is 3 — so one app cannot be
killed five times. The five stages are spread over two apps and six
deployments, each of which is killed once.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest
import pytest_asyncio
from sqlalchemy import select

from control_plane import generations, service, tokens
from control_plane.models import App, Deployment, DeploymentStatus, Job, JobStatus
from tests.pipeline.conftest import ANSWERS, REPO, _operator, settle, visit
from worker import reconcile
from worker.pipeline import cutover, deploy
from worker.runtimes import proxy, serving

# Fast, because every pass in this file is driven by hand. `serve` is the only
# thing that ever waits `reconcile_interval`, and the wait is not under test.
BRISK = 0.05


class Killed(BaseException):
    """What a worker being killed looks like from inside the pipeline.

    A `BaseException` on purpose: `_dispatch` and `run_once` both catch
    `Exception`, so anything less would be caught, given a verdict and
    recorded — which is a worker that *failed*, not one that stopped existing.
    """


# ---------------------------------------------------------------------------
# Queueing without running, and running until killed
# ---------------------------------------------------------------------------


async def queue(pipeline, *, name: str | None = None, after: uuid.UUID | None = None):
    """Queue a deployment and hand back its id, without running anything.

    `Harness.deploy` and `Harness.redeploy` both drain before returning, and a
    drain that is about to be killed never returns the id. Every stage here
    needs the id of the deployment it is about to interrupt.
    """
    async with pipeline.sessions() as session, session.begin():
        if after is None:
            app = await service.create_app(
                session,
                builder=await _operator(session),
                name=name,
                repo=str(REPO),
            )
            pipeline.app_ids.append(app.app_id)
        else:
            app = await session.get(App, (await session.get(Deployment, after)).app_id)
        deployment = await service.start_deployment(
            session, app=app, ref="clean", answers=ANSWERS
        )
        return deployment.id


def _killer(name: str, coroutine: bool) -> Callable[..., Any]:
    def boom(*args: object, **kwargs: object) -> None:
        raise Killed(name)

    async def aboom(*args: object, **kwargs: object) -> None:
        raise Killed(name)

    return aboom if coroutine else boom


async def killed_at(
    module: object, name: str, *, coroutine: bool, run: Callable[[], Any]
) -> None:
    """Run `run()` with `module.name` replaced by something that kills us.

    Patched by hand rather than with `monkeypatch`, because every stage below
    is a session fixture and `monkeypatch` is function-scoped.
    """
    real = getattr(module, name)
    setattr(module, name, _killer(name, coroutine))
    try:
        with pytest.raises(Killed):
            await run()
    finally:
        setattr(module, name, real)


async def expire_lease(pipeline, deployment_id: uuid.UUID) -> None:
    """Make the dead worker's lease run out, without waiting a minute for it.

    The lease is the only thing that says a worker exists, so this is the whole
    of "the rest of the system noticed". Nothing else about the job is touched
    — in particular not `attempts`, which is a real limit this file has to live
    inside rather than reset.
    """
    async with pipeline.sessions() as session, session.begin():
        job = await _job(session, deployment_id)
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)


async def _job(session, deployment_id: uuid.UUID) -> Job:
    return (
        await session.execute(select(Job).where(Job.deployment_id == deployment_id))
    ).scalar_one()


async def job_status(pipeline, deployment_id: uuid.UUID) -> str:
    async with pipeline.sessions() as session:
        return (await _job(session, deployment_id)).status


# ---------------------------------------------------------------------------
# Stage 1 and 2: killed in the planning half
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def after_clone(pipeline):
    """Stage 1. Killed with the code on disk and nothing written down yet."""
    deployment_id = await queue(pipeline, name="reconcile-plan")
    await killed_at(
        deploy, "detect", coroutine=False, run=lambda: pipeline.drain()
    )
    return deployment_id


async def test_a_worker_killed_after_the_clone_leaves_the_job_to_the_queue(
    pipeline, after_clone
):
    """Nothing to unstick: `queued` is a state the queue itself can finish.

    The reconciler has to tell this apart from a deployment that is genuinely
    stranded, and what tells them apart is the status. A loop that blocked
    everything it found with a dead worker on it would turn every worker
    restart into a deploy somebody has to trigger again by hand.
    """
    assert (await pipeline.deployment(after_clone)).status == (
        DeploymentStatus.queued.value
    )
    # The row a killed worker leaves: still held, by nobody.
    assert await job_status(pipeline, after_clone) == JobStatus.running.value

    await expire_lease(pipeline, after_clone)
    drift = await reconcile.once(pipeline.config, pipeline.sessions)

    assert str(after_clone) not in drift.unstuck


async def test_a_worker_killed_after_the_clone_recovers_to_a_live_app(
    pipeline, after_clone
):
    """The recovery itself: the next claim runs the plan phase again.

    Safe to re-run because nothing irreversible had happened — no token was
    minted, no access model was recorded — and the clone is pinned to a commit,
    so the second pass proves the same code the first one fetched.
    """
    await pipeline.drain()
    assert (await pipeline.deployment(after_clone)).status == (
        DeploymentStatus.awaiting_confirmation.value
    )

    await pipeline.confirm(after_clone)
    deployment = await pipeline.deployment(after_clone)
    assert deployment.status == DeploymentStatus.live.value, deployment.blocked_reason
    await settle()


@pytest_asyncio.fixture(scope="session")
async def after_build(pipeline, after_clone):
    """Stage 2. A build job was handed a one-shot token, and we vanished."""
    deployment_id = await queue(pipeline, after=after_clone)
    await killed_at(
        deploy, "run_build_job", coroutine=True, run=lambda: pipeline.drain()
    )
    return deployment_id


async def test_a_build_whose_worker_vanished_is_blocked_with_a_sentence(
    pipeline, after_build
):
    """Protected. The one transient state that can never resume itself.

    `building` with an unspent build token means a one-shot callback was handed
    out and the only process waiting for it has stopped existing. Nothing will
    ever spend that token — not the queue, because a second `plan` cannot be
    recorded over the first one's access model, and not a person. Left alone
    this deployment sits at `building` for ever, which is the exact shape of
    bug a reconciler exists to make impossible.
    """
    deployment = await pipeline.deployment(after_build)
    assert deployment.status == DeploymentStatus.building.value
    async with pipeline.sessions() as session:
        assert await tokens.outstanding(session, after_build)

    await expire_lease(pipeline, after_build)
    drift = await reconcile.once(pipeline.config, pipeline.sessions)

    assert str(after_build) in drift.unstuck
    deployment = await pipeline.deployment(after_build)
    assert deployment.status == DeploymentStatus.blocked.value
    assert deployment.blocked_reason == reconcile.STRANDED
    # And it says what did not happen, which is what stops a builder going to
    # look for damage there is none of.
    assert "already live is still serving" in deployment.blocked_reason


async def test_the_version_people_were_using_survives_the_stranding(
    pipeline, after_clone, after_build
):
    """A deploy that was given up on must not take the live one with it."""
    app = await pipeline.app_of(after_build)
    assert app.live_deployment_id == after_clone

    endpoint = (await pipeline.deployment(after_clone)).endpoint
    async with visit(endpoint) as browser:
        response = await browser.get("/todos")
    assert response.text != proxy.NO_SUCH_APP


async def test_the_queue_does_not_keep_picking_up_what_it_gave_up_on(
    pipeline, after_build
):
    """The verdict sticks. A blocked deployment is a finished job.

    Worth asserting because the job row is still `running` with an expired
    lease, so it *is* claimable. What must not happen is the pipeline starting
    another build over a verdict somebody has already been shown.
    """
    await pipeline.drain()
    assert (await pipeline.deployment(after_build)).status == (
        DeploymentStatus.blocked.value
    )


@pytest.mark.negative
async def test_a_worker_that_comes_back_cannot_publish_over_the_verdict(
    pipeline, after_build
):
    """Protected. The half of the fence CP2 left open.

    The generation fence catches a worker whose app has moved on. It does not
    catch this: nothing newer was queued, but the reconciler has already said
    this attempt is over. Without the status guard in `generations.publish`, a
    worker gone long enough to be given up on could come back and put a version
    live that the dashboard says was blocked.
    """
    deployment = await pipeline.deployment(after_build)
    app = await pipeline.app_of(after_build)

    async with pipeline.sessions() as session, session.begin():
        granted = await generations.publish(
            session,
            app_pk=app.id,
            deployment_id=after_build,
            generation=deployment.generation,
            endpoint="http://nowhere.invalid",
        )

    assert granted is False
    assert (await pipeline.app_of(after_build)).live_deployment_id != after_build


# ---------------------------------------------------------------------------
# Stages 3, 4 and 5: killed in the release half
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def after_migrate(pipeline):
    """Stage 3. Killed with the real database migrated and nothing started."""
    deployment_id = await pipeline.deploy("clean", name="reconcile-release")
    await killed_at(
        serving, "start", coroutine=True, run=lambda: pipeline.confirm(deployment_id)
    )
    return deployment_id


async def test_a_worker_killed_after_migrating_resumes_rather_than_restarting(
    pipeline, after_migrate
):
    """`deploying` is resumable, and this is the test that says so.

    By the time a deployment says `deploying`, the two things that cannot be
    done twice have both been done: the access model was proved in the
    throwaway database, and a person confirmed it. So the recovery is not a
    fresh deploy — it is `_release` stated again, over a schema that already
    has the migrations in it.
    """
    deployment = await pipeline.deployment(after_migrate)
    assert deployment.status == DeploymentStatus.deploying.value
    app = await pipeline.app_of(after_migrate)
    assert await serving.generations(pipeline.config, app.app_id) == []

    # The reconciler leaves it alone: there is a job in a state that can be
    # claimed, so waiting for it is not waiting for ever.
    await expire_lease(pipeline, after_migrate)
    drift = await reconcile.once(pipeline.config, pipeline.sessions)
    assert str(after_migrate) not in drift.unstuck

    await pipeline.drain()
    deployment = await pipeline.deployment(after_migrate)
    assert deployment.status == DeploymentStatus.live.value, deployment.blocked_reason
    assert await serving.generations(pipeline.config, app.app_id) == [deployment.generation]
    await settle()


@pytest_asyncio.fixture(scope="session")
async def mid_cutover(pipeline, after_migrate):
    """Stage 4. Killed with a candidate started and not yet given the traffic.

    What it left behind is read before anything recovers, because that record
    is the evidence for the only claim this stage makes.
    """
    second = await queue(pipeline, after=after_migrate)
    # The plan phase first, unpatched: `health_gate` is only reached in the
    # release, so the kill has to be armed around the confirmation.
    await pipeline.drain()
    await killed_at(
        cutover, "health_gate", coroutine=True, run=lambda: pipeline.confirm(second)
    )
    app = await pipeline.app_of(second)
    generation = (await pipeline.deployment(second)).generation
    return second, await serving.read(pipeline.config, app.app_id, generation)


async def test_an_orphan_of_the_generation_being_deployed_is_adopted(
    pipeline, mid_cutover
):
    """Protected. "Nothing is ever started twice", as the only checkable fact.

    A worker killed between starting a version and publishing it leaves two
    live processes nobody has heard of. The worker that picks the job up must
    find them: a second pair for the same generation would both answer, the run
    record would name only the newest, and the rest would be unreachable,
    unkillable through `reap`, and still holding the app's database
    connections. An identical `Running` before and after is what rules that
    out — a status of `live` would not, because starting a duplicate also ends
    up live. Identical rather than merely alive because every field on it moves
    when a replacement is started: a fresh port locally, a fresh container name
    under Docker.
    """
    second, before = mid_cutover
    assert before is not None

    deployment = await pipeline.deployment(second)
    assert deployment.status == DeploymentStatus.deploying.value
    # Both halves answering, through the same probe the gate uses, so this does
    # not have to know what either half is made of.
    assert (
        await cutover.probe(
            f"{before.endpoint}{cutover.HEALTH_PATH}",
            lambda: serving.alive(pipeline.config, before),
        )
        is None
    )

    await expire_lease(pipeline, second)
    await pipeline.drain()

    deployment = await pipeline.deployment(second)
    assert deployment.status == DeploymentStatus.live.value, deployment.blocked_reason

    app = await pipeline.app_of(second)
    after = await serving.read(pipeline.config, app.app_id, deployment.generation)
    assert after == before
    await settle()


async def test_the_resumed_release_still_replaced_the_old_version(
    pipeline, after_migrate, mid_cutover
):
    """Adoption is not a shortcut past the rest of the release.

    The resumed pass had to run the gate, take the fenced write, re-assert the
    proxy and retire the version it replaced — all of it, over a process it did
    not start. A resume that adopted and then stopped would leave two
    generations running and the old route still published.
    """
    second, _ = mid_cutover
    app = await pipeline.app_of(second)
    old = (await pipeline.deployment(after_migrate)).generation
    new = (await pipeline.deployment(second)).generation

    assert new > old
    assert await serving.generations(pipeline.config, app.app_id) == [new]
    assert app.live_deployment_id == second

    mine = [
        proxy.parse_id(route["@id"])
        for route in await proxy.published(pipeline.config)
        if proxy.parse_id(route["@id"])[0] == app.app_id
    ]
    assert mine == [(app.app_id, new, str(second))]


@pytest_asyncio.fixture(scope="session")
async def reconciled_mid_flight(pipeline, mid_cutover):
    """A whole real cutover with a reconcile pass running inside its health gate.

    The spy wraps `health_gate` rather than replacing it, so the deploy is the
    real one: the gate runs, the fenced publish happens, the proxy is
    re-asserted and the old version is retired. The passes it collected are
    what the next test reads.
    """
    previous, _ = mid_cutover
    seen: list[reconcile.Drift] = []
    real = cutover.health_gate

    async def spy(**kwargs: object):
        seen.append(await reconcile.once(pipeline.config, pipeline.sessions))
        return await real(**kwargs)

    cutover.health_gate = spy
    try:
        third = await queue(pipeline, after=previous)
        await pipeline.drain()
        await pipeline.confirm(third)
    finally:
        cutover.health_gate = real

    await settle()
    return third, seen


@pytest.mark.negative
async def test_a_reconcile_pass_during_a_live_cutover_changes_nothing(
    pipeline, reconciled_mid_flight
):
    """Protected. The failure mode that would make this loop cause outages.

    The pass runs *inside* the health gate of a real cutover, which is the one
    moment a candidate version is running and nothing in the desired state
    points at it. A reaper that knew only "which generation has the traffic"
    would stop it here — killing the version being watched, out from under the
    worker watching it — and the deploy would then fail for a reason the
    reconciler invented.

    What keeps it honest is `_wanted`'s second half: a generation some live
    lease is holding is kept, even though no desired state mentions it.
    """
    third, seen = reconciled_mid_flight

    deployment = await pipeline.deployment(third)
    assert deployment.status == DeploymentStatus.live.value, deployment.blocked_reason

    app = await pipeline.app_of(third)
    assert seen, "the gate never ran, so nothing was reconciled next to it"
    for drift in seen:
        # Both generations were wanted: the old one has the traffic, the new one
        # is held by this worker's lease.
        assert app.app_id not in drift.reaped
        assert str(third) not in drift.unstuck


@pytest_asyncio.fixture(scope="session")
async def after_flip(pipeline, reconciled_mid_flight):
    """Stage 5. Killed after the traffic moved and before the old one drained.

    The worst-looking of the five and the least harmful: the deployment is
    already `live` and already published, so what is left behind is purely a
    leftover — a replaced version still running, still holding connections, and
    pointed at by nothing.
    """
    previous, _ = reconciled_mid_flight
    fourth = await queue(pipeline, after=previous)
    await pipeline.drain()
    await killed_at(
        cutover, "retire", coroutine=True, run=lambda: pipeline.confirm(fourth)
    )
    return previous, fourth


async def test_a_replaced_version_nobody_drained_is_reaped_in_one_pass(
    pipeline, after_flip
):
    """The CP2 claim, completed: "reaped within one reconcile interval".

    CP2 proved a stale worker cannot route traffic, which makes its leftovers
    harmless. This is the half it left open — what makes them temporary. One
    pass, not eventually: the interval is the whole of the promise, so the
    claim is only worth anything if a single pass is enough.
    """
    previous, current = after_flip
    app = await pipeline.app_of(current)
    old = (await pipeline.deployment(previous)).generation
    new = (await pipeline.deployment(current)).generation

    assert (await pipeline.deployment(current)).status == DeploymentStatus.live.value
    assert sorted(await serving.generations(pipeline.config, app.app_id)) == sorted(
        [old, new]
    )
    # Taken before the pass, because reaping destroys the only description of
    # what it reaped. This is the same handle `cutover.retire` checks with, and
    # asking it here is what makes the assertion below independent of `drift`.
    was = await serving.watcher(pipeline.config, app.app_id, old)

    await expire_lease(pipeline, current)
    drift = await reconcile.once(pipeline.config, pipeline.sessions)

    assert drift.reaped.get(app.app_id) == [old]
    assert drift.adopted.get(app.app_id) == new
    assert await serving.generations(pipeline.config, app.app_id) == [new]
    # The evidence, independent of what the loop reported: neither half of the
    # old version answers any more.
    assert not await was()


async def test_the_app_never_stopped_answering_through_the_reaping(
    pipeline, after_flip
):
    """The reaper's blast radius, as the only thing a user would notice."""
    _, current = after_flip
    app = await pipeline.app_of(current)

    async with visit(proxy.public_url(pipeline.config, app.subdomain)) as browser:
        response = await browser.get("/todos")
    assert response.status_code != 404
    assert response.text != proxy.NO_SUCH_APP


# ---------------------------------------------------------------------------
# The loop over a healthy system
# ---------------------------------------------------------------------------


async def test_a_second_pass_over_a_healthy_system_changes_nothing(
    pipeline, after_flip
):
    """Protected. A reconciler that churns is worse than no reconciler.

    Asserted on the result rather than on the writes, and the distinction is
    deliberate: the route table is restated every pass by design, so *writing*
    it is not a change. Writing something different would be, and `changed` is
    what says whether anything moved.
    """
    first = await reconcile.once(pipeline.config, pipeline.sessions)
    second = await reconcile.once(pipeline.config, pipeline.sessions)

    assert first.changed is False
    assert second.changed is False
    assert second.reaped == {}
    assert second.unstuck == []
    assert second.adopted == first.adopted
    assert second.routes == first.routes

    # And byte-identical config read back off the proxy, not off what we
    # believe we sent it.
    before = await proxy.published(pipeline.config)
    await reconcile.once(pipeline.config, pipeline.sessions)
    assert await proxy.published(pipeline.config) == before


async def test_a_hand_edited_proxy_is_stated_back_from_the_database(
    pipeline, after_flip
):
    """Protected. The route table is a statement, never a diff.

    Emptying Caddy stands in for all three of the things that produce the same
    problem — somebody editing it, a stale worker writing to it, and a Caddy
    that was restarted with nothing in it. The point is that the fix is the
    same for all three and nobody has to work out which happened.
    """
    _, current = after_flip
    app = await pipeline.app_of(current)
    host = proxy.host_for(pipeline.config, app.subdomain)

    await proxy.apply(pipeline.config, [])
    assert await proxy.published(pipeline.config) == []

    drift = await reconcile.once(pipeline.config, pipeline.sessions)

    assert host in drift.routes
    # Restating a route is not drift: the database never changed its mind.
    assert drift.changed is False

    async with visit(proxy.public_url(pipeline.config, app.subdomain)) as browser:
        response = await browser.get("/todos")
    assert response.text != proxy.NO_SUCH_APP


# ---------------------------------------------------------------------------
# The two cases the loop reports instead of fixing
# ---------------------------------------------------------------------------


async def _bare(pipeline, name: str) -> tuple[App, Deployment]:
    """An app and a deployment in the control plane with nothing running.

    Built by hand rather than deployed, because what is being tested is the
    reconciler's reading of a `deployments` row, and paying for a real deploy
    to produce a row we are about to overwrite buys nothing.

    Its job is settled on the way out so that a later `drain` — in this file or
    any other sharing the session — does not find a queued deploy for an app
    that was never meant to have one.
    """
    async with pipeline.sessions() as session, session.begin():
        app = await service.create_app(
            session,
            builder=await _operator(session),
            name=name,
            repo=str(REPO),
        )
        pipeline.app_ids.append(app.app_id)
        deployment = await service.start_deployment(
            session, app=app, ref="clean", answers=ANSWERS
        )
        job = await _job(session, deployment.id)
        job.status = JobStatus.done.value
        return app, deployment


async def test_a_live_generation_with_nothing_listening_is_reported_not_restarted(
    pipeline
):
    """Protected. The honest edge of the claim, and it is in the loop's output.

    Reviving an app after the machine it ran on rebooted is out of scope for
    V0.5, and this is what makes that a fact about the code rather than a note
    in a README. Restarting it would mean re-running the app's migrations and
    re-applying its policies against a schema nothing has just proved — which
    is a deployment, and a deployment is a thing a person asks for.

    `lost` rather than silence, because a loop that quietly skips the one case
    it cannot handle looks exactly like a loop that handles it.
    """
    app, deployment = await _bare(pipeline, "reconcile-lost")
    async with pipeline.sessions() as session, session.begin():
        row = await session.get(Deployment, deployment.id)
        row.status = DeploymentStatus.live.value
        (await session.get(App, app.id)).live_deployment_id = row.id

    drift = await reconcile.once(pipeline.config, pipeline.sessions)

    assert drift.lost.get(app.app_id) == deployment.generation
    assert app.app_id not in drift.adopted
    # Nothing was started, and no route was invented for it.
    assert await serving.generations(pipeline.config, app.app_id) == []
    assert proxy.host_for(pipeline.config, app.subdomain) not in drift.routes


async def test_a_deployment_with_no_job_left_to_finish_it_is_blocked(pipeline):
    """The third of `_unstick`'s three answers: nobody is coming.

    A transient deployment with no outstanding build token is normally left for
    the queue, which resumes it when the lease expires. That is only true while
    a job exists that can still be claimed. A job that has run out of attempts
    is a job nothing will claim again, and waiting for it would be waiting for
    ever.
    """
    _, deployment = await _bare(pipeline, "reconcile-abandoned")
    async with pipeline.sessions() as session, session.begin():
        (await session.get(Deployment, deployment.id)).status = (
            DeploymentStatus.deploying.value
        )
        job = await _job(session, deployment.id)
        job.status = JobStatus.failed.value
        job.lease_expires_at = None

    drift = await reconcile.once(pipeline.config, pipeline.sessions)

    assert str(deployment.id) in drift.unstuck
    blocked = await pipeline.deployment(deployment.id)
    assert blocked.status == DeploymentStatus.blocked.value
    assert blocked.blocked_reason == reconcile.NO_WORKER


# ---------------------------------------------------------------------------
# The loop itself
# ---------------------------------------------------------------------------


async def test_a_pass_that_fails_does_not_stop_the_loop(pipeline, monkeypatch):
    """Protected. A reconciler that gave up is a lie.

    One pass failing is normal — the proxy is restarting, the database is busy,
    a record is half written. None of those are reasons to stop converging on
    the next pass, and a loop that exited on the first one would leave a system
    that looks supervised and is not.
    """
    passes: list[int] = []

    async def sometimes(config, sessions):
        passes.append(len(passes))
        if len(passes) < 3:
            raise RuntimeError("the database was busy")
        return reconcile.Drift()

    monkeypatch.setattr(reconcile, "once", sometimes)
    loop = asyncio.create_task(
        reconcile.serve(pipeline.config, pipeline.sessions, interval=BRISK)
    )
    try:
        for _ in range(200):
            await asyncio.sleep(BRISK)
            if len(passes) >= 4:
                break
    finally:
        loop.cancel()
        try:
            await loop
        except asyncio.CancelledError:
            pass

    assert len(passes) >= 4


async def test_a_stopped_proxy_still_lets_the_leftovers_be_reaped(pipeline):
    """Reported, not raised. A laptop with Caddy stopped still wants a sweep.

    The route table is then whatever it was, which is the only correct answer —
    there is nowhere to state it — but the processes nothing claims are still
    processes nothing claims, and an exception here would mean a stopped proxy
    stopped the reaper too.
    """
    config = replace(pipeline.config, proxy_admin_url="http://127.0.0.1:1")
    drift = await reconcile.once(config, pipeline.sessions)

    assert drift.proxy_unavailable is True
    assert drift.routes == []
