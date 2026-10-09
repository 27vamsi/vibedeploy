"""The evidence report. Readme.md 19.7, demo step 11.

Computed from `audit_log` and nothing else. That is the only interesting design
decision in this file, and it is the reason there are no counters anywhere in
the gateway: a tally kept alongside the log is a second set of books, and the
first time anybody would notice it had drifted is the time it mattered. Here,
"38 actions, 31 auto, 4 approved, 3 blocked" is a `GROUP BY` over the same rows
the chain verifier hashes, so a number on the report that is wrong means the
chain is broken, and the report says that too.

The log is joined to `actions` only to find out which app each entry belongs
to. The counting is over events.

One number is not a count: "actions that reached data outside the acts-for
person's access: 0". Nothing the gateway can observe would make that number
anything other than zero — it executes everything as the app's `agent` role
with the person's identity set, so the database decides, and a row outside the
person's access is not a row the query can return. Printing an unconditional
zero would therefore be true and worthless, indistinguishable from printing it
because we forgot to check. So the zero is tied to the thing that really does
prove it, the deployment's own attack run as **both** roles (section 13), and
is withdrawn when that is missing or when the two roles disagreed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models import Action, App, AuditLog, VerificationRun
from gateway import audit

# Every event that means "one call arrived and was attributed to an app". A
# refusal at step 3 or 4 never gets as far as an action row (19.2 writes that
# at step 6), so it is logged under its own event and counted here, otherwise
# "denied: forbidden tool" could never appear on the report 19.7 asks for.
RECEIVED = "action.received"
CALL_DENIED = "call.denied"
ACTION_DENIED = "action.denied"

COUNTED = {
    "verified": "action.verified",
    "failed": "action.failed",
    "approved": "action.approved",
    "rejected": "action.rejected",
    "expired": "action.expired",
    "undone": "action.undone",
    "rolled_back": "action.rolled_back",
    "undo_failed": "action.undo_failed",
    "parked": "action.pending_approval",
}

# What the database said no to, and what an agent aiming at somebody else's
# rows looks like from here. Both are evidence of the boundary holding.
BLOCKED_CODES = ("database_refused",)
NOTHING_MATCHED = "No rows you can access matched."


@dataclass(frozen=True)
class OutsideAccess:
    """Demo step 11's headline number, and what it rests on."""

    reached: int | None
    proven: bool
    basis: str
    blocked: int

    def as_json(self) -> dict[str, Any]:
        return {
            "reached": self.reached,
            "proven": self.proven,
            "basis": self.basis,
            "blocked": self.blocked,
        }


@dataclass(frozen=True)
class Evidence:
    app_id: str
    since: datetime | None
    until: datetime | None

    total: int
    auto: int
    needed_approval: int
    denied: int
    denials: dict[str, int]
    verified: int
    failed: int
    approved: int
    rejected: int
    expired: int
    undone: int
    rolled_back: int
    undo_failed: int
    parked: int
    waiting: int

    outside_access: OutsideAccess
    chain: audit.ChainStatus = field(
        default_factory=lambda: audit.ChainStatus(intact=True, entries=0)
    )

    def as_json(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
            "total": self.total,
            "auto": self.auto,
            "needed_approval": self.needed_approval,
            "denied": self.denied,
            "denials": dict(self.denials),
            "verified": self.verified,
            "failed": self.failed,
            "approved": self.approved,
            "rejected": self.rejected,
            "expired": self.expired,
            "undone": self.undone,
            "rolled_back": self.rolled_back,
            "undo_failed": self.undo_failed,
            "parked": self.parked,
            "waiting": self.waiting,
            "outside_access": self.outside_access.as_json(),
            "chain": self.chain.as_json(),
        }

    def summary(self) -> str:
        """The sentence from demo step 11."""
        outside = (
            f"{self.outside_access.reached} beyond the person's access"
            if self.outside_access.proven
            else "beyond the person's access: not proved"
        )
        chain = (
            "Audit chain intact."
            if self.chain.intact
            else f"Audit chain broken at entry {self.chain.broken_at}."
        )
        return (
            f"{self.total} actions, {self.auto} auto, {self.approved} approved,"
            f" {self.denied} blocked, {outside}. {chain}"
        )


async def report(
    session: AsyncSession,
    *,
    app_uuid: uuid.UUID,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Evidence:
    """One app, one date range, every number out of the log."""
    app = await session.get(App, app_uuid)

    counts = await _by_event(session, app_uuid, since, until)
    decisions = await _by_decision(session, app_uuid, since, until)
    denials = await _by_denial_code(session, app_uuid, since, until)

    denied = counts.get(ACTION_DENIED, 0) + counts.get(CALL_DENIED, 0)
    total = counts.get(RECEIVED, 0) + counts.get(CALL_DENIED, 0)
    named = {name: counts.get(event, 0) for name, event in COUNTED.items()}

    # Still waiting = parked, less every way out of waiting. An approval voided
    # by a changed diff parks the same action twice, so this is the number of
    # actions waiting now, not the number of times anything ever waited.
    waiting = max(
        0,
        named["parked"]
        - named["approved"]
        - named["rejected"]
        - named["expired"],
    )

    return Evidence(
        app_id=app.app_id if app else str(app_uuid),
        since=since,
        until=until,
        total=total,
        auto=decisions.get("auto", 0),
        needed_approval=decisions.get("approve", 0),
        denied=denied,
        denials=denials,
        waiting=waiting,
        outside_access=await _outside_access(
            session, app, await _blocked(session, app_uuid, since, until)
        ),
        chain=await audit.verify_chain(session),
        **named,
    )


# ---------------------------------------------------------------------------
# The counting
# ---------------------------------------------------------------------------


COLUMNS = (AuditLog.id, AuditLog.ts, AuditLog.event, AuditLog.payload_json)


def _scoped(app_uuid, since, until):
    """Entries belonging to this app, in this window.

    Two kinds of entry, so two queries. Most are about an action and reach the
    app through it — the join is the only reason `actions` is touched here at
    all, because `audit_log` has no app column by design: its rows are links in
    one chain, not per-app facts. The rest are refusals from before an action
    existed, which carry the app in their payload instead.
    """
    window = []
    if since is not None:
        window.append(AuditLog.ts >= since)
    if until is not None:
        window.append(AuditLog.ts <= until)

    through_action = (
        select(*COLUMNS)
        .join(Action, Action.id == AuditLog.action_id)
        .where(Action.app_id == app_uuid, *window)
    )
    before_any_action = select(*COLUMNS).where(
        AuditLog.action_id.is_(None),
        AuditLog.payload_json["app"].astext == str(app_uuid),
        *window,
    )
    return through_action.union_all(before_any_action).subquery()


async def _by_event(session, app_uuid, since, until) -> dict[str, int]:
    entries = _scoped(app_uuid, since, until)
    rows = await session.execute(
        select(entries.c.event, func.count()).group_by(entries.c.event)
    )
    return {event: count for event, count in rows.all()}


async def _by_decision(session, app_uuid, since, until) -> dict[str, int]:
    """`auto` against `approve`, as recorded when the action arrived."""
    entries = _scoped(app_uuid, since, until)
    decision = entries.c.payload_json["decision"].astext
    rows = await session.execute(
        select(decision, func.count())
        .where(entries.c.event == RECEIVED)
        .group_by(decision)
    )
    return {value: count for value, count in rows.all() if value}


async def _by_denial_code(session, app_uuid, since, until) -> dict[str, int]:
    """19.7 wants denials broken down by reason, so the machine-readable code
    every `Denied` carries is what is grouped on — never the sentence."""
    entries = _scoped(app_uuid, since, until)
    code = entries.c.payload_json["code"].astext
    rows = await session.execute(
        select(code, func.count())
        .where(entries.c.event.in_((ACTION_DENIED, CALL_DENIED)))
        .group_by(code)
    )
    return {value: count for value, count in rows.all() if value}


async def _blocked(session, app_uuid, since, until) -> int:
    """Times the boundary visibly held: the database refused a write, or the
    rows an agent aimed at were not rows it could reach."""
    entries = _scoped(app_uuid, since, until)
    refused = (
        await session.execute(
            select(func.count())
            .select_from(entries)
            .where(
                entries.c.event.in_((ACTION_DENIED, CALL_DENIED)),
                entries.c.payload_json["code"].astext.in_(BLOCKED_CODES),
            )
        )
    ).scalar_one()
    nothing_matched = (
        await session.execute(
            select(func.count())
            .select_from(entries)
            .where(
                entries.c.event == COUNTED["verified"],
                entries.c.payload_json["reason"].astext == NOTHING_MATCHED,
            )
        )
    ).scalar_one()
    return refused + nothing_matched


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


async def _outside_access(
    session: AsyncSession, app: App | None, blocked: int
) -> OutsideAccess:
    unproven = lambda why: OutsideAccess(  # noqa: E731
        reached=None, proven=False, basis=why, blocked=blocked
    )

    if app is None or app.live_deployment_id is None:
        return unproven("This app has nothing live, so nothing has been proved.")

    runs = {
        run.role: run
        for run in (
            await session.execute(
                select(VerificationRun).where(
                    VerificationRun.deployment_id == app.live_deployment_id
                )
            )
        )
        .scalars()
        .all()
    }
    runtime, agent = runs.get("runtime"), runs.get("agent")
    if runtime is None or agent is None:
        return unproven(
            "The attack suite has not been proved as both roles for what is"
            " live, so this number is withheld."
        )
    if runtime.failed or agent.failed:
        return unproven(
            f"The attack suite reported {runtime.failed + agent.failed} failures"
            " for what is live, so this number is withheld."
        )
    if (runtime.total, runtime.passed) != (agent.total, agent.passed):
        return unproven(
            "The attack suite did not give identical results as the runtime"
            " role and as the agent role, so it has not been proved that the"
            " agent is held to the same rules. This number is withheld."
        )

    return OutsideAccess(
        reached=0,
        proven=True,
        basis=(
            f"Every action ran as the app's agent role with the person's own"
            f" identity set, and the attack suite passed {agent.passed} of"
            f" {agent.total} checks identically as the runtime role and as the"
            f" agent role. {blocked} attempts were stopped by the database."
        ),
        blocked=blocked,
    )
