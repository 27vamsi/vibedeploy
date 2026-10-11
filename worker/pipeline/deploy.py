"""One deployment, from a commit to an endpoint. Readme.md section 18.2.

The pipeline is driven by the deployment's own status, not by anything carried
in the job row, because the two things that move it forward are a builder
answering a question and a builder confirming the rules, and both of those
happen in the dashboard while this job is parked.

    queued      -> run the build job's `plan` phase
    building    -> waiting for `plan` to report (contract 7.4)
      -> awaiting_answers        park. Nothing is guessed; nothing is built.
      -> awaiting_confirmation   park. Section 11 step 6.
      -> blocked / error         stop, with a sentence.
    verifying   -> run the build job's `verify` phase
      -> deploying               apply to the real database and start it
      -> blocked / error         stop, with a sentence.
    deploying   -> resume the release. Only reached by a *second* claim of the
                   job: the worker that got this far stopped, and everything
                   expensive — the proof, and a person's confirmation — is
                   already recorded, so the release is stated again rather than
                   started over. See `_resume`.
    live

A blocked deployment is a **successful** job. The pipeline did exactly what it
exists to do. Only an unexpected failure marks the job itself failed.
"""

from __future__ import annotations

import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
from sqlalchemy import select

from control_plane import generations, tokens
from control_plane.config import ControlPlaneConfig
from control_plane.models import (
    AccessModel,
    App,
    BuildPhase,
    Deployment,
    DeploymentStatus,
    Job,
    TERMINAL_DEPLOYMENT_STATUSES,
)
from worker.pipeline import DONE, PARK, Sessions, Verdict, clone, container, cutover
from worker.pipeline.build import discard_deps, run_build_job
from worker.pipeline.detect import Stack, detect
from worker.pipeline.provision import DeployBlocked, ensure_roles, enforce, run_migration_task
from worker.runtimes import proxy, serving

SUPPORTED_MIGRATIONS = ("sql", "alembic")
UNSUPPORTED_MIGRATIONS = (
    "We could not find this app's migrations, so there is no way to build its"
    " database. V0.5 reads a folder of plain `.sql` files in `migrations/` or"
    " `db/migrations/`, or an Alembic project (`alembic.ini` next to an"
    " `alembic/` folder)."
)
SILENT_BUILD = (
    "The build finished without reporting whether the rules hold, so nothing"
    " was deployed."
)
UNCONFIRMED = (
    "Nobody has confirmed the rules for this deployment, so nothing was built."
)
UNEXPECTED = (
    "Something went wrong on our side before this could be proved safe, so"
    " nothing was deployed."
)

_TERMINAL = frozenset(status.value for status in TERMINAL_DEPLOYMENT_STATUSES)

# Terminal and yet still in use. `live` is a verdict, so it is terminal for the
# job; it is also the one verdict whose packages are *being imported right now*
# by the container that is serving traffic. Throwing them away because the job
# finished is how a succeeded deploy takes its own app down. They go when the
# generation goes — `cutover.retire` and `reconcile.once` are what reap them.
_STILL_SERVING = frozenset({DeploymentStatus.live.value})


@dataclass(frozen=True)
class State:
    """The deployment as it was when the job was claimed."""

    deployment_id: uuid.UUID
    app_pk: uuid.UUID
    app_id: str
    repo: str
    # The hostname this app is published under, which is a property of the app
    # and not of this attempt: a new deployment of it replaces what is behind
    # the same address.
    subdomain: str
    # What the builder asked for, and what git resolved it to on a previous
    # pass, if there was one.
    ref: str
    commit_sha: str | None
    status: str
    answers: dict[str, Any]
    stack: dict[str, Any] | None
    # The app's generation when this deployment was queued. Read once, here,
    # and never re-read: a worker that re-read it would always agree with
    # itself, which is the one thing the fence must not allow.
    generation: int


def _workdir(config: ControlPlaneConfig, deployment_id: uuid.UUID) -> Path:
    return config.state_root / "deployments" / str(deployment_id)


def _callback(config: ControlPlaneConfig, deployment_id: uuid.UUID) -> str:
    return f"{config.public_url}/internal/builds/{deployment_id}/result"


def _labels(state: State) -> dict[str, str]:
    """What every external resource this deployment creates is stamped with."""
    return generations.labels(
        app_id=state.app_id,
        generation=state.generation,
        deployment_id=state.deployment_id,
    )


async def _state(sessions: Sessions, job_id: uuid.UUID) -> State:
    async with sessions() as session:
        job = await session.get(Job, job_id)
        deployment = await session.get(Deployment, job.deployment_id)
        app = await session.get(App, deployment.app_id)
        return State(
            deployment_id=deployment.id,
            app_pk=app.id,
            app_id=app.app_id,
            repo=app.repo,
            subdomain=app.subdomain,
            ref=deployment.ref,
            commit_sha=deployment.commit_sha,
            status=deployment.status,
            answers=dict(deployment.answers or {}),
            stack=deployment.stack,
            generation=deployment.generation,
        )


async def _status(sessions: Sessions, deployment_id: uuid.UUID) -> str:
    async with sessions() as session:
        deployment = await session.get(Deployment, deployment_id)
        return deployment.status


async def _block(sessions: Sessions, deployment_id: uuid.UUID, reason: str) -> Verdict:
    """Stop, with a sentence, unless a verdict is already recorded.

    The build job reports over HTTP and we read the result back out of the
    database, so there is a moment where the verdict has landed and we have not
    seen it yet. Ours is always the vaguer of the two sentences, and replacing a
    named check with "something went wrong" would throw away the only thing a
    builder can act on. A verdict is written once.
    """
    async with sessions() as session, session.begin():
        deployment = await session.get(Deployment, deployment_id)
        if deployment.status in _TERMINAL:
            return DONE
        deployment.status = DeploymentStatus.blocked.value
        deployment.blocked_reason = reason
    return DONE


# ---------------------------------------------------------------------------
# Phase 1: plan
# ---------------------------------------------------------------------------


async def _checkout(
    config: ControlPlaneConfig, sessions: Sessions, state: State
) -> Path:
    """Fetch the code, and record what it turned out to be.

    Once a commit is known it is what gets asked for from then on, never the
    ref again. A branch that moves between `plan` and `verify` must not be able
    to swap the code out from under rules a person has already confirmed.

    A folder has no commit, so there is nothing to pin and every pass takes a
    fresh snapshot. That is the deal with deploying a working tree, and the
    schema hash still has to match across the two passes, so editing the files
    mid-deploy blocks rather than slipping through.
    """
    destination = _workdir(config, state.deployment_id) / "src"
    result = clone.checkout(state.repo, state.commit_sha or state.ref, destination)

    # The ref is only ever corrected one way: a folder has no versions, so
    # whatever was typed becomes `working-tree`. It is never replaced by the
    # commit, or the second pass -- which asks by commit -- would lose the
    # branch name the builder recognises.
    ref = clone.WORKING_TREE if result.ref == clone.WORKING_TREE else state.ref
    if ref != state.ref or result.commit_sha != state.commit_sha:
        async with sessions() as session, session.begin():
            deployment = await session.get(Deployment, state.deployment_id)
            deployment.ref = ref
            deployment.commit_sha = result.commit_sha
    return result.path


async def _source(config: ControlPlaneConfig, sessions: Sessions, state: State) -> Path:
    """The checked-out code, fetched again if this worker has never seen it.

    Every pass after the first runs in a process that may not be the one that
    did the clone — because the builder answered a question hours later, or
    because the first worker was killed — and a working tree under
    `state_root` is not something a new process can assume is there.

    Re-fetching is safe only because `_checkout` asks by commit once one is
    known: the second fetch gets the code the rules were confirmed against,
    not whatever the branch points at now.
    """
    checkout = _workdir(config, state.deployment_id) / "src"
    if checkout.is_dir():
        return checkout
    return await _checkout(config, sessions, state)


async def _plan(config: ControlPlaneConfig, sessions: Sessions, state: State) -> Verdict:
    checkout = await _checkout(config, sessions, state)
    stack = detect(checkout)

    async with sessions() as session, session.begin():
        deployment = await session.get(Deployment, state.deployment_id)
        app = await session.get(App, state.app_pk)
        deployment.stack = stack.as_json()
        # Section 4: what we found is recorded whether or not we can protect
        # it, so an unprotected app is labelled and never silently accepted.
        app.stack = stack.as_json()
        app.protection = stack.protection

    if stack.unprotected_reason:
        return await _block(sessions, state.deployment_id, stack.unprotected_reason)
    if stack.migrations_kind not in SUPPORTED_MIGRATIONS:
        return await _block(sessions, state.deployment_id, UNSUPPORTED_MIGRATIONS)
    if not stack.entrypoint:
        return await _block(sessions, state.deployment_id, serving.NO_ENTRYPOINT)

    async with sessions() as session, session.begin():
        deployment = await session.get(Deployment, state.deployment_id)
        deployment.status = DeploymentStatus.building.value
        token = await tokens.mint(
            session, deployment_id=state.deployment_id, phase=BuildPhase.plan
        )

    outcome = await run_build_job(
        config,
        _spec(config, state, stack, "plan", token, checkout),
        workdir=_workdir(config, state.deployment_id),
        labels=_labels(state),
    )

    status = await _status(sessions, state.deployment_id)
    if status in (
        DeploymentStatus.awaiting_answers.value,
        DeploymentStatus.awaiting_confirmation.value,
    ):
        return PARK
    if status == DeploymentStatus.building.value:
        # The build job never reported. Its verdict is the only thing that can
        # clear a deploy, so silence blocks.
        await _block(sessions, state.deployment_id, SILENT_BUILD)
        return Verdict("done", error=outcome.tail())
    return DONE


def _spec(
    config: ControlPlaneConfig,
    state: State,
    stack: Stack,
    phase: str,
    token: str,
    checkout: Path,
    access_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = {
        "phase": phase,
        "deployment_id": str(state.deployment_id),
        "app_id": state.app_id,
        "repo_dir": str(checkout),
        "migrations": {
            "kind": stack.migrations_kind,
            "path": stack.migrations_path,
        },
        # A throwaway database of its own, not the one apps live in. The build
        # job runs code we have never read and is admin of whatever it is
        # pointed at, so pointing it at the customer instance would hand that
        # code every other app's data (18.2). It invents its own role names in
        # there and drops them again, and never learns the real app's schema
        # name, which is why a hash proved there is worth anything here.
        "scratch_dsn": config.build_dsn,
        "callback_url": _callback(config, state.deployment_id),
        "job_token": token,
        "answers": state.answers,
        "stack": stack.as_json(),
    }
    if access_model is not None:
        spec["access_model"] = access_model
    return spec


# ---------------------------------------------------------------------------
# Phase 2: verify, then actually deploy
# ---------------------------------------------------------------------------


async def _confirmed_model(sessions: Sessions, deployment_id: uuid.UUID) -> dict[str, Any] | None:
    async with sessions() as session:
        model = (
            await session.execute(
                select(AccessModel).where(AccessModel.deployment_id == deployment_id)
            )
        ).scalar_one_or_none()
        if model is None or model.confirmed_at is None:
            return None
        return model.model_json


async def _verify(config: ControlPlaneConfig, sessions: Sessions, state: State) -> Verdict:
    model = await _confirmed_model(sessions, state.deployment_id)
    if model is None:
        return await _block(sessions, state.deployment_id, UNCONFIRMED)

    checkout = await _source(config, sessions, state)
    stack = Stack(**state.stack) if state.stack else detect(checkout)

    async with sessions() as session, session.begin():
        token = await tokens.mint(
            session, deployment_id=state.deployment_id, phase=BuildPhase.verify
        )

    outcome = await run_build_job(
        config,
        _spec(config, state, stack, "verify", token, checkout, access_model=model),
        workdir=_workdir(config, state.deployment_id),
        labels=_labels(state),
    )

    status = await _status(sessions, state.deployment_id)
    if status == DeploymentStatus.verifying.value:
        await _block(sessions, state.deployment_id, SILENT_BUILD)
        return Verdict("done", error=outcome.tail())
    if status != DeploymentStatus.deploying.value:
        return DONE

    return await _release(config, sessions, state, stack, model, checkout)


async def _release(
    config: ControlPlaneConfig,
    sessions: Sessions,
    state: State,
    stack: Stack,
    model: dict[str, Any],
    checkout: Path,
) -> Verdict:
    """Readme.md 18.2 steps 2 to 7, against the database the app lives in."""
    workdir = _workdir(config, state.deployment_id)

    # First, before anything is started or migrated. An app is reachable
    # through its proxy or not at all (section 16), so a missing proxy is not a
    # degraded deploy that hands out a port instead — it is a deploy that does
    # not happen, and the version that is already serving keeps serving.
    await proxy.require(config)

    admin = await asyncpg.connect(config.app_admin_dsn)
    try:
        provisioned = await ensure_roles(admin, config, state.app_id)
        await run_migration_task(
            config,
            provisioned.roles,
            checkout=checkout,
            migrations_kind=stack.migrations_kind,
            migrations_path=stack.migrations_path or "",
            workdir=workdir,
        )
        enforced = await enforce(
            admin, provisioned.roles, model, fresh=provisioned.fresh
        )
    finally:
        await admin.close()

    if enforced.failures:
        # Everything here was already proved in the throwaway database, so a
        # failure means the real schema is not what was proved. Block: the
        # previous version keeps running.
        reason = enforced.failures[0]
        if len(enforced.failures) > 1:
            reason += f" ({len(enforced.failures) - 1} more problem(s) in the report.)"
        return await _block(sessions, state.deployment_id, reason)

    # Adopted if it is already answering, which is the whole of what makes this
    # function safe to call twice. A worker killed between here and the publish
    # left two processes behind for this generation, and starting a third would
    # be worse than either: all of them would answer, the run record would name
    # only the newest, and the rest would be unreachable, unkillable through
    # `reap`, and still holding database connections.
    running = await serving.adopt(config, state.app_id, state.generation)
    if running is None:
        running = await serving.start(
            config,
            app_id=state.app_id,
            generation=state.generation,
            deployment_id=state.deployment_id,
            checkout=checkout,
            entrypoint=stack.entrypoint,
            logs=workdir,
        )

    # Between starting it and giving it the traffic. `serving.start` waited for
    # the gate to answer once, which an app that falls over on its second
    # request also does; the gate wants N answers in a row before anybody's
    # request is sent there. The version already serving is untouched while
    # this runs, so the cost of watching is only ever this worker's time.
    gate = await cutover.health_gate(
        health_url=f"{running.endpoint}{cutover.HEALTH_PATH}",
        app_alive=lambda: serving.alive(config, running),
        gate=cutover.gate_for(config),
    )
    if not gate.passed:
        await serving.stop(config, state.app_id, state.generation)
        return await _block(
            sessions, state.deployment_id, cutover.gate_failed(gate)
        )

    async with sessions() as session, session.begin():
        granted = await generations.publish(
            session,
            app_pk=state.app_pk,
            deployment_id=state.deployment_id,
            generation=state.generation,
            # The public address, not wherever the generation happens to be
            # listening. `running.upstream` is private and stays with the
            # runtime: the route the proxy dials is built from it, and nothing
            # else ever learns it.
            endpoint=proxy.public_url(config, state.subdomain),
            schema_graph=enforced.graph,
        )

    if not granted:
        # Another deployment of this app was queued while this one was being
        # proved, so the right to publish a route moved on. Nothing was granted
        # — that is what the `False` means — and the only thing left to do is
        # take back the process that was started on the strength of a claim
        # this worker no longer has.
        await serving.stop(config, state.app_id, state.generation)
        return await _superseded(sessions, state)

    # Written from the database, never from `state`: the fenced write above is
    # what decided this, and re-reading its decision is what makes a stale
    # worker reaching this line harmless. It would publish whatever won.
    await proxy.reassert(config, sessions)

    # Only now that the database says which generation has the traffic, and the
    # proxy is pointing at it. Doing this before the publish would have had a
    # worker that was about to be refused stopping the version still serving;
    # doing it without the drain would kill requests that were already running
    # on the old version when the route moved.
    await cutover.retire(
        config,
        app_id=state.app_id,
        keep=state.generation,
        drain=config.drain_seconds,
    )
    return DONE


async def _resume(
    config: ControlPlaneConfig, sessions: Sessions, state: State
) -> Verdict:
    """Pick up a release whose worker stopped part way through it.

    Only ever reached by a *second* claim of the job, after the first worker's
    lease expired. By the time a deployment says `deploying`, the two things
    that cannot be done again have both been done: the access model was proved
    in the throwaway database, and a person confirmed it. None of that is in
    this process's memory, so this reassembles exactly what `_verify` held when
    it called `_release`, and calls it again.

    Calling it again is allowed because every step of `_release` states a fact
    rather than performing a transition — the roles are ensured, the migrations
    are whatever the ledger has not applied, the policies are re-enforced, the
    process is adopted if it is already there — and the one step that is not
    idempotent, granting traffic, is the fenced write, which is precisely the
    write that is designed to be attempted twice and succeed once.
    """
    model = await _confirmed_model(sessions, state.deployment_id)
    if model is None:
        # Not reachable by the pipeline's own route, since `deploying` is only
        # ever entered from a confirmed model. Blocked rather than worked
        # around, because the only way around it is to derive an access model
        # here, and nothing outside the build job may decide one.
        return await _block(sessions, state.deployment_id, UNCONFIRMED)

    checkout = await _source(config, sessions, state)
    stack = Stack(**state.stack) if state.stack else detect(checkout)
    return await _release(config, sessions, state, stack, model, checkout)


async def _superseded(sessions: Sessions, state: State) -> Verdict:
    """Record that this attempt lost the race, without touching what won.

    A successful job on a deployment that is not live: the pipeline did
    everything it was asked and the answer turned out to be stale, which is not
    a failure anybody can act on. Deliberately not `blocked` — nothing is wrong
    with this code, and a builder reading "blocked" would go looking for it.
    """
    async with sessions() as session, session.begin():
        deployment = await session.get(Deployment, state.deployment_id)
        if deployment.status not in _TERMINAL:
            deployment.status = DeploymentStatus.superseded.value
    return DONE


# ---------------------------------------------------------------------------
# The entry point the worker loop calls
# ---------------------------------------------------------------------------


async def handle(config: ControlPlaneConfig, sessions: Sessions, job_id: uuid.UUID) -> Verdict:
    state = await _state(sessions, job_id)
    verdict = await _dispatch(config, sessions, state)
    await _discard_deps_if_finished(config, sessions, state.deployment_id)
    return verdict


async def _dispatch(
    config: ControlPlaneConfig, sessions: Sessions, state: State
) -> Verdict:
    try:
        if state.status == DeploymentStatus.queued.value:
            return await _plan(config, sessions, state)
        if state.status == DeploymentStatus.verifying.value:
            return await _verify(config, sessions, state)
        if state.status == DeploymentStatus.deploying.value:
            return await _resume(config, sessions, state)
        if state.status in (
            DeploymentStatus.awaiting_answers.value,
            DeploymentStatus.awaiting_confirmation.value,
        ):
            # Woken by something other than an answer. Go back to waiting
            # rather than spinning.
            return PARK
        return DONE
    except clone.CheckoutError as exc:
        # A named reason, so the builder gets git's own words and the job keeps
        # the detail. Handled here rather than at each of the three places the
        # code can be fetched, which is every phase that can run first.
        await _block(sessions, state.deployment_id, str(exc))
        return Verdict("done", error=exc.detail or None)
    except (
        DeployBlocked,
        serving.StartupFailed,
        container.DockerMissing,
        proxy.ProxyUnavailable,
    ) as exc:
        # `DockerMissing` is ours to fix, not theirs, but it is the one of those
        # that says what to do about it, so it is worth more than `UNEXPECTED`.
        return await _block(sessions, state.deployment_id, str(exc))
    except Exception:  # noqa: BLE001 - a builder is owed a sentence either way
        # We broke, not them. The deployment still has to leave a status a
        # person can read rather than sitting at `verifying` for ever; the
        # traceback goes on the job, where an operator will look for it.
        await _block(sessions, state.deployment_id, UNEXPECTED)
        return Verdict("failed", error=traceback.format_exc()[-4000:])


async def _discard_deps_if_finished(
    config: ControlPlaneConfig, sessions: Sessions, deployment_id: uuid.UUID
) -> None:
    """A deployment that has a verdict has no more use for its packages.

    Once, here, rather than at each of the four places a deploy can end: one
    that forgot would leak a volume per deploy. The status is re-read rather
    than taken from the verdict because `park` and `done` say what the *job*
    did, and a job that is done on a deployment that is only paused must keep
    the packages — the pass after the pause installs into the same volume and
    would otherwise pay for them twice.

    `live` is excluded for a stronger reason than thrift: the serving container
    imports from that volume, so discarding it would be this function taking
    down the app whose deploy just succeeded.
    """
    async with sessions() as session:
        deployment = await session.get(Deployment, deployment_id)
        if deployment is None or deployment.status not in _TERMINAL:
            return
        if deployment.status in _STILL_SERVING:
            return
    await discard_deps(_workdir(config, deployment_id))
