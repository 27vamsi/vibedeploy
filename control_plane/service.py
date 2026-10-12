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

from control_plane import appenv, generations, jobs
from control_plane import secrets as secrets_store
from control_plane.config import ControlPlaneConfig
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


async def ensure_builder(session: AsyncSession, *, email: str, password: str) -> Builder:
    """The sole operator of this installation, created if it has none.

    V0.5 has one operator and no sign-up flow, so this is how both the
    environment-driven bootstrap and the test fixtures get hold of one. The
    caller names the email and the password: what this replaced invented both,
    which meant an unauthenticated console could not tell "nobody has set this
    up" from "this is who set it up" — and answered the second to everybody.
    """
    existing = (await session.execute(select(Builder).limit(1))).scalar_one_or_none()
    if existing is not None:
        return existing
    return await create_builder(session, email=email, password=password)


async def create_app(
    session: AsyncSession,
    *,
    builder: Builder,
    name: str,
    repo: str,
    subdomain: str | None = None,
) -> App:
    app_id = new_app_id()
    app = App(
        builder_id=builder.id,
        app_id=app_id,
        name=name,
        subdomain=subdomain or await _free_subdomain(session, name, app_id),
        repo=repo,
        protection=Protection.unknown.value,
    )
    session.add(app)
    await session.flush()
    return app


async def _free_subdomain(session: AsyncSession, name: str, app_id: str) -> str:
    """A hostname nobody else on this installation already has.

    The subdomain is a unique column, because it is the address requests
    arrive at and two apps cannot share one. With a single operator that was
    invisible; with sign-up it is the first thing that breaks, because the
    second person to call an app "todo" is not doing anything wrong and should
    not meet a 500.

    The fallback appends the app id's own random half rather than counting
    upwards. Counting needs a loop and a race to go with it; this is one extra
    query and the result is still something a person can read.
    """
    slug = subdomain_for(name)
    taken = await session.execute(select(App.id).where(App.subdomain == slug).limit(1))
    if taken.scalar_one_or_none() is None:
        return slug
    return f"{slug}-{app_id.removeprefix('app_')}"


async def request_teardown(session: AsyncSession, *, app: App) -> None:
    """Queue the removal of an app, and stop it being deployable immediately.

    Two writes before the job, both of which matter on their own:

    `status` becomes `deleting`, which is what the console reads to stop
    offering a deploy button for something that is on its way out. A second
    teardown request is refused on it as well, so a person clicking twice does
    not get two jobs racing to drop the same roles.

    `live_deployment_id` is cleared, which is the one that does real work: the
    reconciler's idea of what should be running comes from that column, so from
    this commit onwards this app's containers are generations nothing claims
    and its route is a route the next `proxy.reassert` will not restate. The
    job below does the same thing deliberately and immediately; clearing it
    here is what makes the outcome the same if the job never runs at all.

    The generation is bumped for the same reason a deploy bumps it: a worker
    part way through deploying this app must not be able to publish a route to
    it after this point.
    """
    if app.status == "deleting":
        raise ServiceError("This app is already being removed.")
    app.status = "deleting"
    app.live_deployment_id = None
    await generations.claim_generation(session, app)
    await jobs.enqueue(
        session,
        kind=jobs.KIND_TEARDOWN,
        # No `app_id=`: see `jobs.KIND_TEARDOWN`.
        payload={"app_id": app.app_id, "name": app.name},
    )


async def start_deployment(
    session: AsyncSession,
    *,
    app: App,
    ref: str,
    answers: Mapping[str, Any] | None = None,
) -> Deployment:
    """Queue one attempt to put `ref` live.

    `ref` is a branch, a tag or a commit, whichever the builder named. The
    commit it resolves to is recorded by the worker once git has said, because
    only the clone knows: a branch can move between queueing and building.

    The four questions of section 10 must already be answered, here or on a
    previous deployment. There is no default: a deployment that cannot say who
    may see what has nothing to enforce, so it never starts.

    A previously live deployment is deliberately left alone. If this one is
    blocked, the old version keeps running. Queueing does bump the app's
    generation, which is not the same thing: it revokes nothing, it only means
    that any worker still running an earlier attempt has lost the right to
    publish a route. `control_plane/generations.py` says why that has to happen
    here rather than at cutover.
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
        ref=ref,
        status=DeploymentStatus.queued.value,
        answers=merged,
        generation=await generations.claim_generation(session, app),
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


# ---------------------------------------------------------------------------
# The app's own settings (18.1, "builder env vars")
# ---------------------------------------------------------------------------


def set_app_env(
    config: ControlPlaneConfig, app_id: str, env: Mapping[str, str]
) -> dict[str, str]:
    """Store the variables the app's process will be given.

    In the secret store, not the control plane's database: a Stripe key is a
    credential, and section 18.1 already says the runtime secret is where the
    app's settings live. This is the whole set, not a patch, so removing one is
    sending it again without that one.

    Not a coroutine because nothing here touches the database.
    """
    checked = appenv.validate(env)
    secrets_store.store_for(config).update(
        secrets_store.secret_name(app_id, secrets_store.RUNTIME), {"env": checked}
    )
    return checked


def app_env(config: ControlPlaneConfig, app_id: str) -> dict[str, str]:
    """What is stored, for a form to render. Values included: the form shows
    names only, and that is the template's decision, not this function's."""
    try:
        secret = secrets_store.store_for(config).get(
            secrets_store.secret_name(app_id, secrets_store.RUNTIME)
        )
    except secrets_store.SecretNotFound:
        return {}
    return appenv.safe(secret.get("env"))
