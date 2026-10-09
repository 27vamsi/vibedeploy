"""What a builder's actions actually do.

The dashboard and the JSON API are two ways to reach the same handful of
operations, so the operations live here and both are thin. It keeps the rules
in one place: which statuses may be answered, what confirming a model means,
and the fact that a person who cannot log in yet is `disabled` rather than
absent.
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import jobs
from control_plane.models import (
    AccessModel,
    Action,
    ActionStatus,
    Agent,
    AgentPolicy,
    AgentStatus,
    App,
    AppUser,
    Builder,
    Deployment,
    DeploymentStatus,
    GlobalSettings,
    Protection,
)
from control_plane.passwords import hash_password

# Agents belong to the control plane, but what a key is and what a policy means
# are the gateway's definitions (19.1, contract 7.5). Reaching for them here
# keeps one definition of each rather than a second one that drifts.
from gateway import keys as agent_keys
from gateway import sessions as agent_sessions
from gateway.policy import PolicyError
from gateway.policy import load as load_policy

BASE_ANSWERS = ("audience", "size", "visibility", "sensitive")

# Statuses where an answer is something we are actually waiting for. Answering
# anything else would be editing a decision that has already been acted on.
ANSWERABLE = frozenset(
    {
        DeploymentStatus.awaiting_answers.value,
        DeploymentStatus.awaiting_confirmation.value,
    }
)

_SUBDOMAIN = re.compile(r"[^a-z0-9-]+")


class ServiceError(RuntimeError):
    """Something a builder asked for that we will not do, with the reason."""


def new_app_id() -> str:
    """Short, lowercase, and valid as both a Postgres schema and a role stem."""
    return f"app_{secrets.token_hex(4)}"


def subdomain_for(name: str) -> str:
    slug = _SUBDOMAIN.sub("-", name.strip().lower()).strip("-")
    return slug or "app"


async def create_builder(session: AsyncSession, *, email: str, password: str) -> Builder:
    builder = Builder(email=email.strip().lower(), password_hash=hash_password(password))
    session.add(builder)
    await session.flush()
    return builder


async def create_app(
    session: AsyncSession,
    *,
    builder: Builder,
    name: str,
    repo: str,
    subdomain: str | None = None,
) -> App:
    app = App(
        builder_id=builder.id,
        app_id=new_app_id(),
        name=name,
        subdomain=subdomain or subdomain_for(name),
        repo=repo,
        protection=Protection.unknown.value,
    )
    session.add(app)
    await session.flush()
    return app


async def start_deployment(
    session: AsyncSession,
    *,
    app: App,
    commit_sha: str,
    answers: Mapping[str, Any] | None = None,
) -> Deployment:
    """Queue one attempt to put a commit live.

    The four questions of section 10 must already be answered, here or on a
    previous deployment. There is no default: a deployment that cannot say who
    may see what has nothing to enforce, so it never starts.

    A previously live deployment is deliberately left alone. If this one is
    blocked, the old version keeps running.
    """
    merged = dict(app.answers or {})
    merged.update(answers or {})
    missing = [key for key in BASE_ANSWERS if key not in merged]
    if missing:
        raise ServiceError(
            "These questions have not been answered yet, so nothing can be"
            f" deployed: {', '.join(missing)}."
        )

    deployment = Deployment(
        app_id=app.id,
        commit_sha=commit_sha,
        status=DeploymentStatus.queued.value,
        answers=merged,
    )
    session.add(deployment)
    await session.flush()

    app.answers = merged
    await jobs.enqueue(
        session, app_id=app.id, deployment_id=deployment.id, payload={"phase": "plan"}
    )
    return deployment


async def save_answers(
    session: AsyncSession, *, deployment: Deployment, answers: Mapping[str, Any]
) -> None:
    """Record follow-up answers and let the parked job run again."""
    if deployment.status not in ANSWERABLE:
        raise ServiceError(
            "This deployment is not waiting for an answer, so there is nothing"
            " to answer."
        )
    merged = dict(deployment.answers or {})
    merged.update(answers)
    deployment.answers = merged
    deployment.status = DeploymentStatus.queued.value
    deployment.questions = []
    deployment.updated_at = datetime.now(timezone.utc)

    app = await session.get(App, deployment.app_id)
    if app is not None:
        # Section 10: answers are reused, so only new or changed tables ask
        # again on the next deployment.
        app.answers = merged
    await jobs.wake(session, deployment.id)


async def confirm_model(
    session: AsyncSession, *, deployment: Deployment, confirmed_by: str
) -> AccessModel:
    """Section 11 step 6. Nothing is built until a person says yes.

    The confirmation is stamped onto the model itself as well as the row, so
    the JSON that contract 7.2 describes is self-describing wherever it ends
    up.
    """
    if deployment.status != DeploymentStatus.awaiting_confirmation.value:
        raise ServiceError("There is nothing here to confirm yet.")

    model = (
        await session.execute(
            select(AccessModel).where(AccessModel.deployment_id == deployment.id)
        )
    ).scalar_one_or_none()
    if model is None:
        raise ServiceError("The rules for this deployment were never derived.")

    now = datetime.now(timezone.utc)
    model.confirmed_by = confirmed_by
    model.confirmed_at = now
    model.model_json = {
        **model.model_json,
        "confirmed_by": confirmed_by,
        "confirmed_at": now.isoformat(),
    }
    deployment.status = DeploymentStatus.verifying.value
    deployment.updated_at = now
    await jobs.wake(session, deployment.id)
    return model


async def add_app_user(
    session: AsyncSession, *, app: App, email: str, password: str, role: str = "member"
) -> AppUser:
    """Add a person to a deployed app.

    They are created `disabled`, because the row that represents them inside
    the app's own database does not exist yet. The worker inserts it as the
    migrator, writes back the key the database actually gave that row, and
    enables them. Until then, logging in fails closed rather than handing out a
    session that maps to nothing.

    `principal_key` starts as a placeholder the worker overwrites. It is a
    random UUID rather than an empty string on purpose: the empty string is
    what "nobody is logged in" looks like to the policies, and it must never
    be something a row can hold.
    """
    user = AppUser(
        app_id=app.id,
        email=email.strip().lower(),
        password_hash=hash_password(password),
        role=role,
        principal_key=str(uuid.uuid4()),
        disabled=True,
    )
    session.add(user)
    await session.flush()
    await jobs.enqueue(
        session,
        kind=jobs.KIND_ADD_USER,
        app_id=app.id,
        payload={"app_user_id": str(user.id)},
    )
    return user


# ---------------------------------------------------------------------------
# Agents. Readme.md 19.1 and 19.5.
# ---------------------------------------------------------------------------


async def create_agent(
    session: AsyncSession,
    *,
    app: App,
    name: str,
    acts_for: AppUser,
    created_by: str,
    policy_yaml: str,
    high_risk_ack: bool = False,
) -> tuple[Agent, str]:
    """Add an agent and issue its one and only key.

    An agent is created with a policy or not at all. There is no window in
    which one exists with nothing saying what it may do, because the gateway
    would deny every call in that window and the builder would be debugging a
    race instead of reading a form.

    The key is returned here and nowhere else, ever again.
    """
    if acts_for.app_id != app.id:
        raise ServiceError("That person does not belong to this app.")
    try:
        load_policy(policy_yaml, version=1, high_risk_ack=high_risk_ack)
    except PolicyError as exc:
        raise ServiceError(str(exc)) from exc

    agent = Agent(
        app_id=app.id,
        name=name.strip(),
        acts_for_user_id=acts_for.id,
        status=AgentStatus.active.value,
        created_by=created_by,
    )
    session.add(agent)
    await session.flush()
    session.add(
        AgentPolicy(
            agent_id=agent.id,
            version=1,
            policy_yaml=policy_yaml,
            author=created_by,
            high_risk_ack=high_risk_ack,
        )
    )
    minted = await agent_keys.mint(session, agent_id=agent.id)
    return agent, minted.key


async def save_policy(
    session: AsyncSession,
    *,
    agent: Agent,
    policy_yaml: str,
    author: str,
    high_risk_ack: bool = False,
) -> AgentPolicy:
    """Contract 7.5: every edit is a new version. Nothing is ever rewritten.

    An action records the version that decided it, so a policy that is edited
    after the fact cannot change the answer to "why was this allowed?".
    """
    latest = (
        await session.execute(
            select(func.max(AgentPolicy.version)).where(AgentPolicy.agent_id == agent.id)
        )
    ).scalar()
    version = (latest or 0) + 1
    try:
        load_policy(policy_yaml, version=version, high_risk_ack=high_risk_ack)
    except PolicyError as exc:
        raise ServiceError(str(exc)) from exc

    row = AgentPolicy(
        agent_id=agent.id,
        version=version,
        policy_yaml=policy_yaml,
        author=author,
        high_risk_ack=high_risk_ack,
    )
    session.add(row)
    await session.flush()
    return row


async def revoke_agent(session: AsyncSession, *, agent: Agent) -> None:
    """One click, four things (19.5): disable the agent, revoke its keys, end
    its sessions, and cancel anything of its that was waiting for a person.

    All four in one transaction. Disabling it and leaving a pending action for
    somebody to approve tomorrow would be a revoke that did not revoke.
    """
    agent.status = AgentStatus.disabled.value
    await agent_keys.revoke_all(session, agent_id=agent.id)
    await agent_sessions.revoke_all(session, agent_id=agent.id)
    await session.execute(
        update(Action)
        .where(
            Action.agent_id == agent.id,
            Action.status == ActionStatus.pending_approval.value,
        )
        .values(
            status=ActionStatus.rejected.value,
            reason="The agent was revoked while this was waiting to be approved.",
        )
    )


async def set_kill_switch(session: AsyncSession, *, on: bool) -> None:
    """The switch that stops every agent everywhere. Section 19.5."""
    await session.execute(
        update(GlobalSettings).where(GlobalSettings.id == 1).values(agent_kill_switch=on)
    )


async def set_app_agents(session: AsyncSession, *, app: App, on: bool) -> None:
    """The middle of 19.5's three switches: every agent of one app.

    Here rather than in a handler so that all three switches are written in the
    same place, and so that the gateway's check (19.1) has exactly one row to
    read for each of them.
    """
    app.agents_enabled = on
