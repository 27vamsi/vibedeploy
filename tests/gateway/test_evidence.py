"""The evidence report. Readme.md 19.7, section 8 M12, demo step 11.

The acceptance for this is "the numbers match the audit log exactly", so every
test here counts the audit log itself and compares. That is deliberately a
tautology-looking test and it is the only one worth writing: the report's whole
value is that it is not a second set of books. A counter maintained alongside
the log could drift, and the first time anybody noticed would be the time it
mattered.

The one number that is not a count is "actions that reached data outside the
acts-for person's access: 0". A zero printed unconditionally would be a lie
dressed as evidence, so it rests on the deployment's own attack run — runtime
and agent, agreeing — and is withdrawn when that is missing or disagrees.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import delete, func, select, update

from control_plane.models import App, AuditLog, VerificationRun
from tests.gateway.conftest import wire

CONTROL_URL = (
    "postgresql+asyncpg://vd_admin:vd_local_password@127.0.0.1:55432"
    "/vibedeploy_evidence_test"
)

POLICY = """
agent: alices-helper
acts_for: alice@example.com
session_ttl_minutes: 30
rate_limit_per_minute: 240
postgres:
  default: {read: auto}
  tables:
    customers: {read: auto, update: auto}
    orders: {read: auto, create: approve}
"""


@pytest_asyncio.fixture(scope="module")
async def wired(gateway_admin, tmp_path_factory):
    async with wire(
        gateway_admin,
        tmp_path_factory.mktemp("evidence"),
        control_url=CONTROL_URL,
        policy=POLICY,
    ) as wired:
        yield wired


@pytest_asyncio.fixture(scope="module")
async def proved(wired):
    """The attack run a finished deployment leaves behind: both roles, no
    failures, identical totals."""
    async with wired.sessions() as session, session.begin():
        app = await session.get(App, wired.app_uuid)
        for role in ("runtime", "agent"):
            session.add(
                VerificationRun(
                    deployment_id=app.live_deployment_id,
                    role=role,
                    total=520,
                    passed=520,
                    failed=0,
                    report_json={"checks": []},
                )
            )
    await _busy(wired)
    return wired


async def _count(wired, event) -> int:
    async with wired.sessions() as session:
        return (
            await session.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.event == event)
            )
        ).scalar_one()


async def _busy(wired):
    """Six calls: four that are allowed, two that are not."""
    session_id = await wired.session_id()
    mine = wired.app.plan.one_owned_by("customers", "A")
    theirs = wired.app.plan.one_owned_by("customers", "B")

    # 1 and 2: reads, allowed automatically.
    await wired.gateway.call(session_id, "db_query", {"table": "customers"})
    await wired.gateway.call(session_id, "db_query", {"table": "orders"})

    # 3: a write on her own row, allowed automatically, and verified.
    await wired.gateway.call(
        session_id,
        "db_update",
        {
            "table": "customers",
            "filters": [{"column": "id", "op": "=", "value": str(mine.key[0])}],
            "values": {"name": "touched by the agent"},
        },
    )

    # 4: a write aimed at somebody else's row. The database has nothing to
    # change, so this succeeds at doing nothing.
    await wired.gateway.call(
        session_id,
        "db_update",
        {
            "table": "customers",
            "filters": [{"column": "id", "op": "=", "value": str(theirs.key[0])}],
            "values": {"name": "aimed at Bob"},
        },
    )

    # 5: a tool the policy does not open at all. Refused before any record of
    # an action exists, which is exactly why it has to be logged too.
    await wired.gateway.call(
        session_id, "db_delete", {"table": "customers", "filters": []}
    )

    # 6: a table that does not exist in the deployed schema.
    await wired.gateway.call(session_id, "db_query", {"table": "no_such_table"})
    return session_id


# ---------------------------------------------------------------------------
# The numbers are the log's numbers
# ---------------------------------------------------------------------------


async def test_every_total_matches_the_audit_log(proved):
    wired = proved
    report = await wired.gateway.evidence(wired.app_uuid)

    assert report.verified == await _count(wired, "action.verified")
    assert report.failed == await _count(wired, "action.failed")
    assert report.approved == await _count(wired, "action.approved")
    assert report.rejected == await _count(wired, "action.rejected")
    assert report.expired == await _count(wired, "action.expired")
    assert report.undone == await _count(wired, "action.undone")
    assert report.parked == await _count(wired, "action.pending_approval")
    assert report.denied == (
        await _count(wired, "action.denied") + await _count(wired, "call.denied")
    )
    assert report.total == (
        await _count(wired, "action.received") + await _count(wired, "call.denied")
    )


async def test_the_six_calls_are_all_accounted_for(proved):
    """Nothing is double counted and nothing goes missing: every call ended up
    in exactly one of the buckets."""
    wired = proved
    report = await wired.gateway.evidence(wired.app_uuid)

    assert report.total == 6
    assert report.verified == 4
    assert report.denied == 2
    assert report.failed == 0
    assert report.parked == 0
    assert report.auto == 5  # the sixth never got as far as a decision
    assert report.needed_approval == 0
    assert report.verified + report.failed + report.denied + report.parked == report.total


async def test_denials_are_broken_down_by_reason(proved):
    """19.7: "denied (by reason)". The codes are the ones `Denied` carries, so
    a new way of saying no cannot quietly land in a bucket called other."""
    wired = proved
    report = await wired.gateway.evidence(wired.app_uuid)

    assert report.denials == {"forbidden_tool": 1, "unknown_table": 1}
    assert sum(report.denials.values()) == report.denied


async def test_a_refusal_that_happens_before_the_action_row_is_still_counted(proved):
    """The forbidden tool is refused at step 3, before step 6 writes anything.
    An evidence report that only counted actions would never show it, and
    "denied: forbidden tool" is one of the lines 19.7 asks for."""
    wired = proved
    assert await _count(wired, "call.denied") == 1
    report = await wired.gateway.evidence(wired.app_uuid)
    assert report.denials["forbidden_tool"] == 1


async def test_an_action_still_waiting_is_counted_as_waiting(proved):
    wired = proved
    before = await wired.gateway.evidence(wired.app_uuid)

    await wired.gateway.call(
        await wired.session_id(),
        "db_create",
        {
            "table": "orders",
            "values": {
                "customer_id": str(wired.app.plan.one_owned_by("customers", "A").key[0])
            },
        },
    )

    after = await wired.gateway.evidence(wired.app_uuid)
    assert after.total == before.total + 1
    assert after.parked == before.parked + 1
    assert after.needed_approval == before.needed_approval + 1
    assert after.verified == before.verified
    assert after.waiting == 1


async def test_the_report_is_scoped_to_one_app(proved):
    """Another app's actions are another app's business."""
    wired = proved
    somebody_else = await wired.gateway.evidence(uuid.uuid4())
    assert somebody_else.total == 0
    assert somebody_else.denied == 0
    assert somebody_else.denials == {}


async def test_a_date_range_narrows_it(proved):
    wired = proved
    now = datetime.now(timezone.utc)

    assert (await wired.gateway.evidence(wired.app_uuid, since=now + timedelta(days=1))).total == 0
    assert (await wired.gateway.evidence(wired.app_uuid, until=now - timedelta(days=1))).total == 0

    whole = await wired.gateway.evidence(wired.app_uuid)
    windowed = await wired.gateway.evidence(
        wired.app_uuid, since=now - timedelta(days=1), until=now + timedelta(days=1)
    )
    assert windowed.total == whole.total


# ---------------------------------------------------------------------------
# The claim the whole product rests on
# ---------------------------------------------------------------------------


async def test_nothing_reached_outside_the_acts_for_persons_access(proved):
    wired = proved
    outside = (await wired.gateway.evidence(wired.app_uuid)).outside_access

    assert outside.reached == 0
    assert outside.proven
    # And it says what that rests on, rather than just asserting it.
    assert "520" in outside.basis
    assert "agent" in outside.basis
    # The write aimed at Bob is counted as blocked, not as nothing.
    assert outside.blocked >= 1


async def test_the_zero_is_withdrawn_when_the_attack_run_is_missing(proved):
    """Fail closed. Without the agent-role run there is no evidence that the
    agent is held to the same rules as the person, so the report says so
    instead of printing a zero somebody might act on."""
    wired = proved
    async with wired.sessions() as session, session.begin():
        await session.execute(
            delete(VerificationRun).where(VerificationRun.role == "agent")
        )

    outside = (await wired.gateway.evidence(wired.app_uuid)).outside_access
    assert not outside.proven
    assert outside.reached is None
    assert "not been proved" in outside.basis


async def test_the_zero_is_withdrawn_when_the_two_roles_disagree(proved):
    """Section 13: the attack suite runs as runtime and as agent and must
    produce identical results. If it did not, the agent is not the person."""
    wired = proved
    async with wired.sessions() as session, session.begin():
        app = await session.get(App, wired.app_uuid)
        session.add(
            VerificationRun(
                deployment_id=app.live_deployment_id,
                role="agent",
                total=520,
                passed=519,
                failed=1,
                report_json={"checks": []},
            )
        )

    outside = (await wired.gateway.evidence(wired.app_uuid)).outside_access
    assert not outside.proven
    assert outside.reached is None


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


async def test_the_report_carries_the_chain_status(proved):
    wired = proved
    report = await wired.gateway.evidence(wired.app_uuid)
    assert report.chain.intact
    assert report.chain.entries > 0


async def test_an_edited_audit_row_shows_up_on_the_report(proved):
    """Permanently protected (CLAUDE.md): the tamper check. An evidence report
    that still said "intact" after somebody edited the log would be worse than
    no report at all.

    The last entry is the one edited, and it is then deleted rather than put
    back, so the rest of this module's chain is left as it was. Editing it at
    all takes disabling the append-only trigger, which is the point of the
    trigger.
    """
    from sqlalchemy import text

    from control_plane.models import SCHEMA

    wired = proved
    async with wired.sessions() as session, session.begin():
        last = (
            await session.execute(
                select(AuditLog.id).order_by(AuditLog.id.desc()).limit(1)
            )
        ).scalar_one()
        await session.execute(
            text(f'ALTER TABLE "{SCHEMA}".audit_log DISABLE TRIGGER audit_log_append_only')
        )
        await session.execute(
            update(AuditLog)
            .where(AuditLog.id == last)
            .values(payload_json={"tool": "something else entirely"})
        )
    try:
        report = await wired.gateway.evidence(wired.app_uuid)
        assert not report.chain.intact
        assert report.chain.broken_at == last
    finally:
        async with wired.sessions() as session, session.begin():
            await session.execute(delete(AuditLog).where(AuditLog.id == last))
            await session.execute(
                text(
                    f'ALTER TABLE "{SCHEMA}".audit_log'
                    " ENABLE TRIGGER audit_log_append_only"
                )
            )
    assert (await wired.gateway.evidence(wired.app_uuid)).chain.intact
