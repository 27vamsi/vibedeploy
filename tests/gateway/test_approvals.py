"""Approvals, verify and undo. Readme.md section 8 M12, sections 19.4 and 7.6.

These tests are about the gap between "the agent asked" and "the data changed".
Everything in that gap exists so that a person decides, and so that what they
decided on is what actually happens:

  - an approval is bound to the `diff_hash` the dry run showed, so if the data
    moves underneath it the approval is void rather than approximate;
  - an action executes **at most once**, which is a claim about races, not about
    tidiness, so the second approval is tested against a committed first one;
  - a write that cannot be seen afterwards is undone, not reported as done.

The row in the app's database is the assertion throughout. "It returned the
right status" is not the question; "did the customer's name change" is.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from control_plane.models import Action, ActionStatus, AuditLog
from gateway.connectors.postgres import PostgresConnector
from gateway.pipeline import APPROVAL_WINDOW
from tests.gateway.conftest import wire

CONTROL_URL = (
    "postgresql+asyncpg://vd_admin:vd_local_password@127.0.0.1:55432"
    "/vibedeploy_approvals_test"
)

# Writing needs a person to say yes. Reading does not, so the tests can look
# through the agent's own eyes without arranging an approval first.
POLICY = """
agent: alices-helper
acts_for: alice@example.com
session_ttl_minutes: 30
rate_limit_per_minute: 120
postgres:
  default: {read: auto}
  tables:
    customers: {read: auto, create: approve, update: approve, delete: approve}
"""


@pytest_asyncio.fixture(scope="module")
async def wired(gateway_admin, tmp_path_factory):
    async with wire(
        gateway_admin,
        tmp_path_factory.mktemp("approvals"),
        control_url=CONTROL_URL,
        policy=POLICY,
    ) as wired:
        yield wired


@pytest.fixture
def mine(wired):
    """One of Alice's own customers, so nothing here is about access at all."""
    row = wired.app.plan.one_owned_by("customers", "A")
    assert row is not None
    return row


async def _rename(wired, row, to: str):
    """Ask, as the agent, for a name change that will need approving."""
    return await wired.gateway.call(
        await wired.session_id(),
        "db_update",
        {
            "table": "customers",
            "filters": [{"column": "id", "op": "=", "value": str(row.key[0])}],
            "values": {"name": to},
        },
    )


async def _action(wired, action_id) -> Action:
    async with wired.sessions() as session:
        return await session.get(Action, action_id)


async def _executions(wired, action_id) -> int:
    """How many times the audit log says this action ran."""
    async with wired.sessions() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action_id == action_id,
                    AuditLog.event.in_(("action.verified", "action.failed")),
                )
            )
        ).scalar_one()


async def _set_name(wired, row_id, name):
    """A change from outside the gateway, as the app's owner would make it."""
    await wired.app.admin.execute(
        f'UPDATE "{wired.app.roles.schema}".customers SET name = $1 WHERE id = $2',
        name,
        row_id,
    )


# ---------------------------------------------------------------------------
# Waiting, and then happening once
# ---------------------------------------------------------------------------


async def test_an_action_that_needs_approval_waits_and_changes_nothing(wired, mine):
    was = await wired.name_of(mine.key[0])

    outcome = await _rename(wired, mine, "renamed if a person agrees")

    assert outcome.status == ActionStatus.pending_approval.value
    assert not outcome.ok
    assert outcome.action_id is not None
    # The person deciding is shown what would happen (19.4).
    assert outcome.affected == 1
    assert outcome.diff
    assert await wired.name_of(mine.key[0]) == was


async def test_approving_runs_it_exactly_once(wired, mine):
    was = await wired.name_of(mine.key[0])
    parked = await _rename(wired, mine, "approved once")

    done = await wired.gateway.approve(parked.action_id, approver=wired.admin)
    assert done.ok, done.reason
    assert done.status == ActionStatus.verified.value
    assert await wired.name_of(mine.key[0]) == "approved once"

    # Approving again does nothing at all: not a second write, not an error
    # that hides one.
    again = await wired.gateway.approve(parked.action_id, approver=wired.admin)
    assert not again.ok
    assert await wired.name_of(mine.key[0]) == "approved once"
    assert await _executions(wired, parked.action_id) == 1

    stored = await _action(wired, parked.action_id)
    assert stored.approved_by == wired.admin
    assert stored.approved_at is not None

    await _set_name(wired, mine.key[0], was)


async def test_an_approval_is_bound_to_the_diff_it_was_given(wired, mine):
    """19.4: different hash = approval void, back to pending."""
    was = await wired.name_of(mine.key[0])
    parked = await _rename(wired, mine, "approved against a stale diff")

    await _set_name(wired, mine.key[0], "but somebody edited it first")

    outcome = await wired.gateway.approve(parked.action_id, approver=wired.admin)
    assert not outcome.ok
    assert outcome.status == ActionStatus.pending_approval.value
    assert "changed" in (outcome.reason or "")
    # Not written, and not quietly dropped either: it is waiting again.
    assert await wired.name_of(mine.key[0]) == "but somebody edited it first"
    assert (
        await _action(wired, parked.action_id)
    ).status == ActionStatus.pending_approval.value
    assert await _executions(wired, parked.action_id) == 0

    # And the diff it now shows is the current one, so approving again is
    # approving what would really happen.
    second = await wired.gateway.approve(parked.action_id, approver=wired.admin)
    assert second.ok, second.reason
    assert await wired.name_of(mine.key[0]) == "approved against a stale diff"

    await _set_name(wired, mine.key[0], was)


async def test_an_approval_expires_after_fifteen_minutes(wired, mine):
    was = await wired.name_of(mine.key[0])
    parked = await _rename(wired, mine, "too late")

    stored = await _action(wired, parked.action_id)
    assert stored.approval_expires_at is not None
    window = stored.approval_expires_at - stored.updated_at
    assert abs(window - APPROVAL_WINDOW) < timedelta(seconds=5)
    assert APPROVAL_WINDOW == timedelta(minutes=15)

    async with wired.sessions() as session, session.begin():
        await session.execute(
            update(Action)
            .where(Action.id == parked.action_id)
            .values(approval_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )

    outcome = await wired.gateway.approve(parked.action_id, approver=wired.admin)
    assert not outcome.ok
    assert outcome.status == ActionStatus.expired.value
    assert await wired.name_of(mine.key[0]) == was
    assert await _executions(wired, parked.action_id) == 0

    # An expired action is finished. Approving it later does not revive it.
    assert not (
        await wired.gateway.approve(parked.action_id, approver=wired.admin)
    ).ok
    assert await wired.name_of(mine.key[0]) == was


async def test_rejecting_stops_it_for_good(wired, mine):
    was = await wired.name_of(mine.key[0])
    parked = await _rename(wired, mine, "rejected")

    outcome = await wired.gateway.reject(parked.action_id, approver=wired.admin)
    assert not outcome.ok
    assert outcome.status == ActionStatus.rejected.value
    assert await wired.name_of(mine.key[0]) == was

    assert not (await wired.gateway.approve(parked.action_id, approver=wired.admin)).ok
    assert await wired.name_of(mine.key[0]) == was
    assert await _executions(wired, parked.action_id) == 0


# ---------------------------------------------------------------------------
# Who may approve
# ---------------------------------------------------------------------------


async def test_only_an_admin_of_this_app_may_approve(wired, mine):
    """19.4: the approver must be an app admin. Alice is a member, and the
    person this agent acts for, which is exactly the loophole to close."""
    was = await wired.name_of(mine.key[0])
    parked = await _rename(wired, mine, "self-approved")

    outcome = await wired.gateway.approve(
        parked.action_id, approver="alice@example.com"
    )
    assert not outcome.ok
    assert outcome.status == ActionStatus.pending_approval.value
    assert await wired.name_of(mine.key[0]) == was
    assert await _executions(wired, parked.action_id) == 0

    outcome = await wired.gateway.approve(parked.action_id, approver="nobody@nowhere")
    assert not outcome.ok
    assert await wired.name_of(mine.key[0]) == was
    assert await _executions(wired, parked.action_id) == 0


async def test_an_unknown_action_is_refused(wired):
    outcome = await wired.gateway.approve(uuid.uuid4(), approver=wired.admin)
    assert not outcome.ok
    assert outcome.status == ActionStatus.denied.value


async def test_approving_is_refused_while_the_kill_switch_is_on(wired, mine):
    """19.5: the switch stops agents. An action waiting for approval is an
    agent's action, so it does not get to slip through on a person's click."""
    from control_plane import service

    was = await wired.name_of(mine.key[0])
    parked = await _rename(wired, mine, "approved during a lockdown")

    async with wired.sessions() as session, session.begin():
        await service.set_kill_switch(session, on=True)
    try:
        outcome = await wired.gateway.approve(parked.action_id, approver=wired.admin)
        assert not outcome.ok
        assert await wired.name_of(mine.key[0]) == was
        assert await _executions(wired, parked.action_id) == 0
    finally:
        async with wired.sessions() as session, session.begin():
            await service.set_kill_switch(session, on=False)


# ---------------------------------------------------------------------------
# Verify, and undo
# ---------------------------------------------------------------------------


async def test_a_write_that_cannot_be_seen_afterwards_is_undone(wired, mine, monkeypatch):
    """19.2 step 13. The mismatch is injected, because a real one needs a
    database that lies; what is being tested is what we do about it."""
    was = await wired.name_of(mine.key[0])

    async def cannot_see_it(self, creds, tool, args, result):
        return False

    monkeypatch.setattr(PostgresConnector, "verify", cannot_see_it)

    parked = await _rename(wired, mine, "written but not verifiable")
    outcome = await wired.gateway.approve(parked.action_id, approver=wired.admin)

    assert not outcome.ok
    assert outcome.status == ActionStatus.rolled_back.value
    # Undone means undone: the row is back as it was.
    assert await wired.name_of(mine.key[0]) == was


async def test_an_undo_that_cannot_put_it_back_says_so(wired, mine, monkeypatch):
    async def cannot_see_it(self, creds, tool, args, result):
        return False

    monkeypatch.setattr(PostgresConnector, "verify", cannot_see_it)

    async def moved_on(self, creds, action):
        from gateway.connectors.base import UndoResult

        return UndoResult(ok=False, reason="somebody else has changed it since")

    monkeypatch.setattr(PostgresConnector, "undo", moved_on)

    parked = await _rename(wired, mine, "neither verifiable nor undoable")
    outcome = await wired.gateway.approve(parked.action_id, approver=wired.admin)

    assert not outcome.ok
    assert outcome.status == ActionStatus.undo_failed.value
    assert await wired.name_of(mine.key[0]) == "neither verifiable nor undoable"


async def test_a_verified_action_can_be_undone_on_request(wired, mine):
    was = await wired.name_of(mine.key[0])
    parked = await _rename(wired, mine, "undo me")
    assert (await wired.gateway.approve(parked.action_id, approver=wired.admin)).ok
    assert await wired.name_of(mine.key[0]) == "undo me"

    outcome = await wired.gateway.undo(parked.action_id, by=wired.admin)
    assert outcome.ok, outcome.reason
    assert outcome.status == ActionStatus.undone.value
    assert await wired.name_of(mine.key[0]) == was

    # Once undone, there is nothing left to undo.
    assert not (await wired.gateway.undo(parked.action_id, by=wired.admin)).ok
    assert await wired.name_of(mine.key[0]) == was


async def test_undo_refuses_when_the_row_has_moved_since(wired, mine):
    """Readme M12: undo restores before-values only if the rows still match
    the after-values. Otherwise it would silently discard somebody's edit."""
    parked = await _rename(wired, mine, "about to be edited by a person")
    assert (await wired.gateway.approve(parked.action_id, approver=wired.admin)).ok

    await _set_name(wired, mine.key[0], "and then a person edited it")

    outcome = await wired.gateway.undo(parked.action_id, by=wired.admin)
    assert not outcome.ok
    assert "changed since" in (outcome.reason or "")
    assert await wired.name_of(mine.key[0]) == "and then a person edited it"
    # Still verified: the undo failing is not the action failing.
    assert (
        await _action(wired, parked.action_id)
    ).status == ActionStatus.verified.value


async def test_a_read_cannot_be_undone(wired):
    outcome = await wired.gateway.call(
        await wired.session_id(), "db_query", {"table": "customers"}
    )
    assert outcome.ok
    undone = await wired.gateway.undo(outcome.action_id, by=wired.admin)
    assert not undone.ok
    assert "read" in (undone.reason or "").lower()


# ---------------------------------------------------------------------------
# The audit log saw all of it
# ---------------------------------------------------------------------------


async def test_every_decision_is_in_the_audit_log_and_the_chain_holds(wired, mine):
    parked = await _rename(wired, mine, "fully audited")
    await wired.gateway.approve(parked.action_id, approver=wired.admin)
    await wired.gateway.undo(parked.action_id, by=wired.admin)

    async with wired.sessions() as session:
        events = [
            row[0]
            for row in (
                await session.execute(
                    select(AuditLog.event)
                    .where(AuditLog.action_id == parked.action_id)
                    .order_by(AuditLog.id)
                )
            ).all()
        ]

    assert events == [
        "action.received",
        "action.dry_run",
        "action.pending_approval",
        "action.approved",
        "action.verified",
        "action.undone",
    ]
    assert (await wired.gateway.verify_audit()).intact
