"""The gateway core. Readme.md section 8 M10, sections 19.1 to 19.6.

**Permanently protected.** The fail-closed tests and the audit tamper test are
on the list in CLAUDE.md that is never deleted or skipped to make CI green.

Everything here is real: a real control plane schema, a real app built by the
same helper the attack suite uses, a real agent with a real key, and the real
pipeline. The only thing that is arranged rather than earned is the app's
deployment row, because running the whole worker to get one would be testing
M7 again.

The question each of these asks is the same one: when something is wrong, does
the gateway do nothing? Not "does it return a tidy error" — does the row in the
app's database still say what it said before.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import select, text, update

from control_plane import service
from control_plane.models import SCHEMA, Agent, AgentPolicy, AgentSession
from gateway import keys as agent_keys
from gateway.pipeline import Gateway
from tests.gateway.conftest import wire

CONTROL_URL = (
    "postgresql+asyncpg://vd_admin:vd_local_password@127.0.0.1:55432"
    "/vibedeploy_gateway_test"
)

POLICY = """
agent: alices-helper
acts_for: alice@example.com
session_ttl_minutes: 30
rate_limit_per_minute: 60
postgres:
  default: {read: auto}
  tables:
    customers: {read: auto, update: auto}
    audit_log: {}
"""


@pytest_asyncio.fixture(scope="module")
async def wired(gateway_admin, tmp_path_factory):
    async with wire(
        gateway_admin,
        tmp_path_factory.mktemp("gateway"),
        control_url=CONTROL_URL,
        policy=POLICY,
    ) as wired:
        yield wired


def _one(app, table, persona):
    row = app.plan.one_owned_by(table, persona)
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# It works at all
# ---------------------------------------------------------------------------


async def test_an_agent_reads_exactly_its_persons_rows(wired):
    outcome = await wired.gateway.call(
        await wired.session_id(), "db_query", {"table": "customers"}
    )
    assert outcome.ok, outcome.reason
    expected = {str(key[0]) for key in wired.app.plan.owned_by("customers", "A")}
    assert {row["id"] for row in outcome.rows} == expected
    assert expected


async def test_a_write_aimed_at_somebody_else_says_so(wired):
    """M11: "0 affected, action reported as no rows you can access". Demo step
    7. The connector has always known this; what is tested here is that the
    whole pipeline carries the words out to the agent and into the record,
    rather than reporting a silent success that changed nothing."""
    theirs = _one(wired.app, "customers", "B")
    was = await wired.name_of(theirs.key[0])

    outcome = await wired.gateway.call(
        await wired.session_id(),
        "db_update",
        {
            "table": "customers",
            "filters": [{"column": "id", "op": "=", "value": str(theirs.key[0])}],
            "values": {"name": "marked by somebody else's agent"},
        },
    )

    assert outcome.affected == 0
    assert outcome.reason == "No rows you can access matched."
    assert await wired.name_of(theirs.key[0]) == was


async def test_only_the_tools_the_policy_leaves_open_are_listed(wired):
    listed = {tool["name"] for tool in await wired.gateway.tools(await wired.session_id())}
    assert listed == {"db_query", "db_update"}
    assert "db_delete" not in listed


async def test_a_tool_that_is_not_listed_is_not_callable(wired):
    outcome = await wired.gateway.call(
        await wired.session_id(),
        "db_delete",
        {"table": "customers", "filters": [{"column": "name", "op": "!=", "value": ""}]},
    )
    assert not outcome.ok
    assert outcome.status == "denied"


async def test_a_table_the_policy_does_not_open_for_writing_is_denied(wired):
    """`plans` falls to the default, which only allows reading."""
    outcome = await wired.gateway.call(
        await wired.session_id(),
        "db_update",
        {"table": "plans", "values": {"monthly": "1"}},
    )
    assert not outcome.ok
    assert outcome.status == "denied"


# ---------------------------------------------------------------------------
# Fail closed. M10's "Done when".
# ---------------------------------------------------------------------------


async def test_a_disabled_agent_is_denied(wired):
    async with wired.sessions() as session, session.begin():
        await session.execute(
            update(Agent).where(Agent.id == wired.agent_id).values(status="disabled")
        )
    try:
        with pytest.raises(Exception):
            await wired.gateway.open_session(wired.key)
        outcome = await wired.gateway.call(uuid.uuid4(), "db_query", {"table": "plans"})
        assert not outcome.ok
    finally:
        async with wired.sessions() as session, session.begin():
            await session.execute(
                update(Agent).where(Agent.id == wired.agent_id).values(status="active")
            )


async def test_an_expired_session_is_denied(wired):
    session_id = await wired.session_id()
    async with wired.sessions() as session, session.begin():
        await session.execute(
            update(AgentSession)
            .where(AgentSession.id == session_id)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )

    outcome = await wired.gateway.call(session_id, "db_query", {"table": "customers"})
    assert not outcome.ok
    assert outcome.rows == []


async def test_an_agent_with_no_policy_is_denied(wired):
    """A policy we cannot find is a policy that says nothing is allowed. It is
    never read as "no restrictions"."""
    async with wired.sessions() as session, session.begin():
        kept = (
            await session.execute(
                select(AgentPolicy).where(AgentPolicy.agent_id == wired.agent_id)
            )
        ).scalars().all()
        saved = [(row.version, row.policy_yaml, row.author, row.high_risk_ack) for row in kept]
        for row in kept:
            await session.delete(row)
    try:
        outcome = await wired.gateway.call(
            uuid.uuid4(), "db_query", {"table": "customers"}
        )
        assert not outcome.ok
        with pytest.raises(Exception):
            await wired.gateway.open_session(wired.key)
    finally:
        async with wired.sessions() as session, session.begin():
            for version, policy_yaml, author, ack in saved:
                session.add(
                    AgentPolicy(
                        agent_id=wired.agent_id,
                        version=version,
                        policy_yaml=policy_yaml,
                        author=author,
                        high_risk_ack=ack,
                    )
                )


async def test_nothing_happens_when_the_audit_log_cannot_be_written(wired):
    """19.2 step 6: the "about to do this" record is written before the work.
    If it cannot be written, the work does not happen."""
    mine = _one(wired.app, "customers", "A")
    was = await wired.name_of(mine.key[0])
    session_id = await wired.session_id()

    async with wired.sessions() as session, session.begin():
        await session.execute(text(f'ALTER TABLE "{SCHEMA}".audit_log RENAME TO gone'))
    try:
        outcome = await wired.gateway.call(
            session_id,
            "db_update",
            {
                "table": "customers",
                "filters": [{"column": "id", "op": "=", "value": str(mine.key[0])}],
                "values": {"name": "written while the log was down"},
            },
        )
        assert not outcome.ok
        assert outcome.status == "denied"
    finally:
        async with wired.sessions() as session, session.begin():
            await session.execute(
                text(f'ALTER TABLE "{SCHEMA}".gone RENAME TO audit_log')
            )

    assert await wired.name_of(mine.key[0]) == was


async def test_the_kill_switch_stops_the_very_next_call(wired):
    session_id = await wired.session_id()
    assert (await wired.gateway.call(session_id, "db_query", {"table": "customers"})).ok

    async with wired.sessions() as session, session.begin():
        await service.set_kill_switch(session, on=True)
    try:
        # The same session, immediately. Nothing is cached that could still
        # answer for the old state.
        outcome = await wired.gateway.call(
            session_id, "db_query", {"table": "customers"}
        )
        assert not outcome.ok
        assert outcome.rows == []
    finally:
        async with wired.sessions() as session, session.begin():
            await service.set_kill_switch(session, on=False)

    assert (await wired.gateway.call(session_id, "db_query", {"table": "customers"})).ok


async def test_revoking_an_agent_ends_its_open_sessions(wired):
    session_id = await wired.session_id()
    async with wired.sessions() as session, session.begin():
        agent = await session.get(Agent, wired.agent_id)
        await service.revoke_agent(session, agent=agent)
    try:
        outcome = await wired.gateway.call(
            session_id, "db_query", {"table": "customers"}
        )
        assert not outcome.ok
    finally:
        # Switching the agent back on does not bring its key back: a revoked key
        # stays revoked, which is the point. The later tests get a new one.
        async with wired.sessions() as session, session.begin():
            await session.execute(
                update(Agent).where(Agent.id == wired.agent_id).values(status="active")
            )
            wired.key = (await agent_keys.mint(session, agent_id=wired.agent_id)).key


async def test_too_many_calls_a_minute_are_refused(wired):
    from gateway.ratelimit import RateLimiter

    slow = Gateway(
        sessionmaker=wired.sessions,
        secrets_root=wired.gateway._store.root,
        limiter=RateLimiter(),
    )
    session_id = await wired.session_id()
    # The policy allows 60 a minute. The bucket starts full and refills at one
    # token a second, so the first 60 always go through and a burst that keeps
    # going runs out shortly after: exactly when depends on how long the calls
    # took, which is not something to assert on.
    for _ in range(60):
        assert (await slow.call(session_id, "db_query", {"table": "plans"})).ok

    refused = None
    for _ in range(200):
        outcome = await slow.call(session_id, "db_query", {"table": "plans"})
        if not outcome.ok:
            refused = outcome
            break
    assert refused is not None, "the burst was never refused"
    assert "too many" in (refused.reason or "").lower()


# ---------------------------------------------------------------------------
# The audit chain
# ---------------------------------------------------------------------------


async def test_the_chain_is_intact_while_nobody_has_touched_it(wired):
    await wired.gateway.call(await wired.session_id(), "db_query", {"table": "plans"})
    status = await wired.gateway.verify_audit()
    assert status.intact, status.detail
    assert status.entries > 0
    assert status.broken_at is None


async def test_the_verifier_finds_an_edited_row(wired):
    """The table refuses UPDATE, so this turns the refusal off first: the
    attacker being modelled is one who already has the database, not one who
    came through the gateway. The point of the chain is that even they cannot
    do it quietly."""
    await wired.gateway.call(await wired.session_id(), "db_query", {"table": "plans"})

    async with wired.sessions() as session, session.begin():
        target = (
            await session.execute(
                text(f'SELECT id FROM "{SCHEMA}".audit_log ORDER BY id LIMIT 1')
            )
        ).scalar_one()
        await session.execute(
            text(f'ALTER TABLE "{SCHEMA}".audit_log DISABLE TRIGGER audit_log_append_only')
        )
        await session.execute(
            text(
                f"UPDATE \"{SCHEMA}\".audit_log SET payload_json = '{{\"tool\": \"nothing\"}}'"
                " WHERE id = :id"
            ),
            {"id": target},
        )

    try:
        status = await wired.gateway.verify_audit()
        assert not status.intact
        assert status.broken_at == target
        assert "changed" in (status.detail or "")
    finally:
        async with wired.sessions() as session, session.begin():
            await session.execute(
                text(f'DELETE FROM "{SCHEMA}".audit_log WHERE id >= :id'), {"id": target}
            )
            await session.execute(
                text(
                    f'ALTER TABLE "{SCHEMA}".audit_log'
                    " ENABLE TRIGGER audit_log_append_only"
                )
            )


async def test_the_audit_log_refuses_to_be_edited_through_the_front_door(wired):
    # There has to be something to delete: the guard is a row trigger, and a
    # DELETE that matches nothing is not a delete anybody minds.
    await wired.gateway.call(await wired.session_id(), "db_query", {"table": "plans"})
    async with wired.sessions() as session:
        with pytest.raises(Exception):
            await session.execute(
                text(f'DELETE FROM "{SCHEMA}".audit_log WHERE id > 0')
            )
            await session.commit()
