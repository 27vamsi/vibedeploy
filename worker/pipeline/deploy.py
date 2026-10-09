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
    live

A blocked deployment is a **successful** job. The pipeline did exactly what it
exists to do. Only an unexpected failure marks the job itself failed.
"""

from __future__ import annotations

import shutil
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
from sqlalchemy import select

from control_plane import tokens
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
from worker.pipeline import DONE, PARK, Sessions, Verdict, clone
from worker.pipeline.build import run_build_job
from worker.pipeline.detect import Stack, detect
from worker.pipeline.provision import DeployBlocked, ensure_roles, enforce, run_migration_task
from worker.runtimes import local

UNSUPPORTED_MIGRATIONS = (
    "We could not find a folder of plain `.sql` migrations, so there is no way"
    " to build this app's database. V0.5 looks in `migrations/` or"
    " `db/migrations/`."
)
SILENT_BUILD = (
    "The build finished without reporting whether the rules hold, so nothing"
    " was deployed."
)
UNEXPECTED = (
    "Something went wrong on our side before this could be proved safe, so"
    " nothing was deployed."
)

_TERMINAL = frozenset(status.value for status in TERMINAL_DEPLOYMENT_STATUSES)


@dataclass(frozen=True)
class State:
    """The deployment as it was when the job was claimed."""

    deployment_id: uuid.UUID
    app_pk: uuid.UUID
    app_id: str
    repo: str
    commit_sha: str
    status: str
    answers: dict[str, Any]
    stack: dict[str, Any] | None


def _workdir(config: ControlPlaneConfig, deployment_id: uuid.UUID) -> Path:
    return config.state_root / "deployments" / str(deployment_id)


def _callback(config: ControlPlaneConfig, deployment_id: uuid.UUID) -> str:
    return f"{config.public_url}/internal/builds/{deployment_id}/result"


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
            commit_sha=deployment.commit_sha,
            status=deployment.status,
            answers=dict(deployment.answers or {}),
            stack=deployment.stack,
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


async def _checkout(config: ControlPlaneConfig, state: State) -> Path:
    workdir = _workdir(config, state.deployment_id)
    destination = workdir / "src"
    shutil.rmtree(destination, ignore_errors=True)
    return clone.checkout(state.repo, state.commit_sha, destination)


async def _plan(config: ControlPlaneConfig, sessions: Sessions, state: State) -> Verdict:
    try:
        checkout = await _checkout(config, state)
    except clone.CheckoutError as exc:
        return await _block(sessions, state.deployment_id, str(exc))

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
    if stack.migrations_kind != "sql":
        return await _block(sessions, state.deployment_id, UNSUPPORTED_MIGRATIONS)
    if not stack.entrypoint:
        return await _block(sessions, state.deployment_id, local.NO_ENTRYPOINT)

    async with sessions() as session, session.begin():
        deployment = await session.get(Deployment, state.deployment_id)
        deployment.status = DeploymentStatus.building.value
        token = await tokens.mint(
            session, deployment_id=state.deployment_id, phase=BuildPhase.plan
        )

    outcome = await run_build_job(
        _spec(config, state, stack, "plan", token, checkout),
        workdir=_workdir(config, state.deployment_id),
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
        # A throwaway schema in the same local Postgres. The build job invents
        # its own role names and drops them again; it never learns the real
        # app's schema name, which is why a hash proved there is worth anything
        # here.
        "scratch_dsn": config.app_admin_dsn,
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
        return await _block(
            sessions,
            state.deployment_id,
            "Nobody has confirmed the rules for this deployment, so nothing"
            " was built.",
        )

    checkout = _workdir(config, state.deployment_id) / "src"
    if not checkout.is_dir():
        # The worker was restarted between confirming and verifying.
        try:
            checkout = await _checkout(config, state)
        except clone.CheckoutError as exc:
            return await _block(sessions, state.deployment_id, str(exc))

    stack = Stack(**state.stack) if state.stack else detect(checkout)

    async with sessions() as session, session.begin():
        token = await tokens.mint(
            session, deployment_id=state.deployment_id, phase=BuildPhase.verify
        )

    outcome = await run_build_job(
        _spec(config, state, stack, "verify", token, checkout, access_model=model),
        workdir=_workdir(config, state.deployment_id),
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

    running = await local.start(
        config,
        app_id=state.app_id,
        checkout=checkout,
        entrypoint=stack.entrypoint,
        logs=workdir,
    )

    async with sessions() as session, session.begin():
        previous = (
            (
                await session.execute(
                    select(Deployment).where(
                        Deployment.app_id == state.app_pk,
                        Deployment.status == DeploymentStatus.live.value,
                        Deployment.id != state.deployment_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for old in previous:
            old.status = DeploymentStatus.superseded.value

        deployment = await session.get(Deployment, state.deployment_id)
        deployment.status = DeploymentStatus.live.value
        deployment.blocked_reason = None
        deployment.endpoint = running.endpoint
        # Only now, when the live schema has matched the hash the rules were
        # proved against. An agent's allowlist is read from here, so it must
        # never describe a schema that was rejected.
        deployment.schema_graph = enforced.graph

        app = await session.get(App, state.app_pk)
        app.live_deployment_id = deployment.id
        app.status = DeploymentStatus.live.value
    return DONE


# ---------------------------------------------------------------------------
# The entry point the worker loop calls
# ---------------------------------------------------------------------------


async def handle(config: ControlPlaneConfig, sessions: Sessions, job_id: uuid.UUID) -> Verdict:
    state = await _state(sessions, job_id)
    try:
        if state.status == DeploymentStatus.queued.value:
            return await _plan(config, sessions, state)
        if state.status == DeploymentStatus.verifying.value:
            return await _verify(config, sessions, state)
        if state.status in (
            DeploymentStatus.awaiting_answers.value,
            DeploymentStatus.awaiting_confirmation.value,
        ):
            # Woken by something other than an answer. Go back to waiting
            # rather than spinning.
            return PARK
        return DONE
    except (DeployBlocked, local.StartupFailed) as exc:
        return await _block(sessions, state.deployment_id, str(exc))
    except Exception:  # noqa: BLE001 - a builder is owed a sentence either way
        # We broke, not them. The deployment still has to leave a status a
        # person can read rather than sitting at `verifying` for ever; the
        # traceback goes on the job, where an operator will look for it.
        await _block(sessions, state.deployment_id, UNEXPECTED)
        return Verdict("failed", error=traceback.format_exc()[-4000:])
