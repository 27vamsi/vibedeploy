"""Who is calling, and whether they still may. Readme.md 19.1, 19.2 step 1.

Section 19.1 lists six things every call checks: key valid, agent active, app
agents enabled, global switch on, session not expired, acts-for user still
active. They are all checked here, on every call, against the database — not
against anything cached in the process.

Section 19.5 allows the switches to be cached for up to a second. They are not,
and that is deliberate: the checks ride along on the session lookup this call
has to make anyway, so the cache would save nothing and the only thing it could
add is a second in which a revoked agent still works. "At most one second" is
satisfied by none.

The other rule this file exists to hold is that **every way of being refused
looks the same from the outside**. An unknown session and an expired one are
both `no_session`, because the difference is exactly the information somebody
guessing session ids wants.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models import (
    Agent,
    AgentPolicy as PolicyRow,
    AgentSession,
    AgentStatus,
    App,
    AppUser,
    GlobalSettings,
)
from gateway import keys
from gateway.errors import Denied
from gateway.policy import AgentPolicy, PolicyError, load


@dataclass(frozen=True)
class Caller:
    """One authenticated call's worth of identity.

    `principal_key` is the only thing that reaches the app's database, as
    `app.user_id`. It is the key of the acts-for person's own row, so an agent
    is indistinguishable from that person as far as every policy is concerned,
    which is the entire claim.
    """

    # `None` when nobody is calling: an approval is a person acting on an
    # action the agent asked for earlier, and there is no agent session to
    # hang it on. Every other check below is made again anyway.
    session_id: uuid.UUID | None
    agent_id: uuid.UUID
    agent_name: str
    app_uuid: uuid.UUID
    app_id: str
    acts_for: uuid.UUID
    principal_key: str
    role: str
    policy: AgentPolicy


async def _switched_off(session: AsyncSession) -> bool:
    """The global kill switch. A missing settings row counts as off-limits.

    "No row" must never read as "the switch is not on": a control plane that
    cannot tell us whether agents are allowed has told us they are not.
    """
    value = (
        await session.execute(
            select(GlobalSettings.agent_kill_switch).where(GlobalSettings.id == 1)
        )
    ).scalar_one_or_none()
    return value is not False


async def _policy_for(session: AsyncSession, agent_id: uuid.UUID) -> AgentPolicy:
    row = (
        await session.execute(
            select(PolicyRow)
            .where(PolicyRow.agent_id == agent_id)
            .order_by(PolicyRow.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise Denied(
            "no_policy",
            "This agent has no policy, so there is nothing that says it may do"
            " anything. Nothing was done.",
        )
    try:
        return load(row.policy_yaml, version=row.version, high_risk_ack=row.high_risk_ack)
    except PolicyError as exc:
        # A policy we cannot read is a missing policy, never a partial one.
        raise Denied("no_policy", f"This agent's policy cannot be read. {exc}") from exc


async def _live(session: AsyncSession, agent_id: uuid.UUID) -> tuple[Agent, App, AppUser]:
    """The agent, its app and its person, with all four switches checked."""
    row = (
        await session.execute(
            select(Agent, App, AppUser)
            .join(App, App.id == Agent.app_id)
            .join(AppUser, AppUser.id == Agent.acts_for_user_id)
            .where(Agent.id == agent_id)
        )
    ).first()
    if row is None:
        raise Denied("unknown_agent", "There is no such agent.")

    agent, app, person = row
    if agent.status != AgentStatus.active.value:
        raise Denied("agent_disabled", "This agent has been switched off.")
    if not app.agents_enabled:
        raise Denied("agents_disabled", "Agents are switched off for this app.")
    if await _switched_off(session):
        raise Denied("kill_switch", "Agents are switched off everywhere right now.")
    if person.disabled:
        # 19.1: the person removed or demoted, so the agent goes with them.
        raise Denied(
            "acts_for_disabled",
            "The person this agent works for can no longer sign in, so neither"
            " can the agent.",
        )
    return agent, app, person


async def open_session(session: AsyncSession, *, key: str) -> Caller:
    """Exchange a key for a session. Section 19.1."""
    key_row = await keys.resolve(session, key)
    if key_row is None:
        raise Denied("bad_key", "That key does not work.")

    agent, app, person = await _live(session, key_row.agent_id)
    policy = await _policy_for(session, agent.id)

    row = AgentSession(
        agent_id=agent.id,
        expires_at=datetime.now(timezone.utc)
        + timedelta(minutes=policy.session_ttl_minutes),
    )
    session.add(row)
    await session.flush()
    return _caller(row.id, agent, app, person, policy)


async def authenticate(session: AsyncSession, *, session_id: Any) -> Caller:
    """Step 1 of the pipeline, run again on every single call."""
    row = None
    if isinstance(session_id, uuid.UUID):
        row = await session.get(AgentSession, session_id)
    elif isinstance(session_id, str):
        try:
            row = await session.get(AgentSession, uuid.UUID(session_id))
        except ValueError:
            row = None

    now = datetime.now(timezone.utc)
    if row is None or row.revoked or row.expires_at <= now:
        raise Denied(
            "no_session",
            "This session is not open. Start a new one with your key.",
        )

    agent, app, person = await _live(session, row.agent_id)
    policy = await _policy_for(session, agent.id)
    return _caller(row.id, agent, app, person, policy)


async def for_agent(session: AsyncSession, *, agent_id: uuid.UUID) -> Caller:
    """The same identity, rebuilt without a session, for work a person starts.

    Approving and undoing happen minutes after the call that created the
    action, so everything 19.1 checks is checked again here: a kill switch
    thrown in between, or an agent revoked, stops the approval as surely as it
    stops a call. The policy is reloaded at its current version too, because a
    policy narrowed since the action was parked has to narrow it.
    """
    agent, app, person = await _live(session, agent_id)
    policy = await _policy_for(session, agent.id)
    return _caller(None, agent, app, person, policy)


def _caller(
    session_id: uuid.UUID | None,
    agent: Agent,
    app: App,
    person: AppUser,
    policy: AgentPolicy,
) -> Caller:
    return Caller(
        session_id=session_id,
        agent_id=agent.id,
        agent_name=agent.name,
        app_uuid=app.id,
        app_id=app.app_id,
        acts_for=person.id,
        principal_key=person.principal_key,
        role=person.role,
        policy=policy,
    )


async def revoke_all(session: AsyncSession, *, agent_id: uuid.UUID) -> None:
    """End every session this agent has. Used by revoke (19.5)."""
    await session.execute(
        update(AgentSession)
        .where(AgentSession.agent_id == agent_id, AgentSession.revoked.is_(False))
        .values(revoked=True)
    )
