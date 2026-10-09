"""The fourteen steps of Readme.md 19.2, in order, on every call.

The order is the design. Authentication before anything, the audit record
before the work, the dry run before the decision, and the decision before the
commit. Read `call` top to bottom and it is the numbered list.

Two rules hold the whole thing up:

  - **Any exception is a denial.** Not a 500, not a retry: a recorded refusal.
    `call` catches everything, and the only way out of it with work done is the
    path that went through every step.
  - **The intent is written before the work.** The `received` record and its
    audit entry are committed before the connector is touched, so an action
    that killed the process halfway is still a row somebody can find. If that
    write fails the call is denied, because an unrecordable action is one we
    have chosen not to be able to explain (19.2 step 6).

The second half of the file is the approval flow (19.4). An action that needs a
person parks as `pending_approval` and `call` returns its id; `approve` picks it
back up, and the only reason it is a separate entry point rather than a flag is
that a person decides minutes later, under a policy and a set of switches that
may have changed in between. So `approve` re-checks all of them, and redoes the
dry run, before anything is written.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane import secrets as secret_store
from control_plane.models import Action, ActionStatus, App, AppUser, Deployment
from control_plane.secrets import SecretStore
from gateway import actions as action_machine
from gateway import audit, sessions
from gateway import evidence as evidence_report
from gateway.connectors.base import DryRun
from gateway.connectors.postgres import PostgresConnector, SchemaCatalog
from gateway.errors import Denied
from gateway.policy import FORBID
from gateway.ratelimit import RateLimiter
from gateway.risk import score
from gateway.sessions import Caller

CONNECTOR = "postgres"


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _refusal(action_id: Any, refusal: Denied) -> "Outcome":
    """A refused decision, reporting where the action actually is."""
    return Outcome(
        status=(
            ActionStatus.denied.value
            if refusal.code in NOT_WAITING
            else ActionStatus.pending_approval.value
        ),
        ok=False,
        action_id=_as_uuid(action_id),
        reason=refusal.reason,
    )


# 19.4. Long enough for somebody to read a diff, short enough that the diff is
# still describing the database when they do.
APPROVAL_WINDOW = timedelta(minutes=15)

# The one role that may say yes. Not the agent, and not the person the agent
# acts for: both of those would make the approval the agent's own.
APPROVER_ROLE = "admin"

# Refusing to approve usually leaves the action exactly where it was: waiting.
# These two are the exceptions, where there is no waiting action to report on,
# so saying `pending_approval` would invent one.
NOT_WAITING = frozenset({"unknown_action", "not_waiting"})


@dataclass(frozen=True)
class Scope:
    """What a connector is allowed to open for this one call.

    Built here rather than in the connector so that the only place an app's
    agent secret is read is a place that already knows who is acting for whom.
    """

    dsn: str
    schema: str
    acts_for: str
    role: str


@dataclass(frozen=True)
class Outcome:
    """What the agent is told, and what the action row ends up saying."""

    status: str
    ok: bool
    action_id: uuid.UUID | None = None
    reason: str | None = None
    risk: str | None = None
    affected: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)
    diff: list[dict[str, Any]] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "action_id": str(self.action_id) if self.action_id else None,
            "reason": self.reason,
            "risk": self.risk,
            "affected": self.affected,
            "rows": self.rows,
            "diff": self.diff,
        }


class Gateway:
    """One process, one control plane, many apps.

    V0.5 has exactly one connector, so this talks to it directly. When the AWS
    connector lands this becomes a lookup; inventing the lookup now would be
    inventing a second implementation of a thing that has one.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        secrets_root: Path,
        limiter: RateLimiter | None = None,
    ):
        self._sessions = sessionmaker
        self._store = SecretStore(Path(secrets_root))
        self._limiter = limiter or RateLimiter()
        # Keyed by app, invalidated by the live deployment id, so a redeploy
        # that changes the schema changes what an agent may name.
        self._catalogs: dict[uuid.UUID, tuple[uuid.UUID, SchemaCatalog]] = {}

    # -- the front door ------------------------------------------------------

    async def open_session(self, key: str) -> Caller:
        async with self._sessions() as session:
            caller = await sessions.open_session(session, key=key)
            await audit.append(
                session,
                event="session.opened",
                payload={"agent": str(caller.agent_id), "app": caller.app_id},
            )
            await session.commit()
            return caller

    async def tools(self, session_id: Any) -> list[dict[str, Any]]:
        """Only what this agent's policy leaves open. A forbidden tool is not
        listed, so an agent is never told what it may not do."""
        async with self._sessions() as session:
            caller = await sessions.authenticate(session, session_id=session_id)
            connector = await self._connector(session, caller)
            return [spec.as_json() for spec in connector.tools(caller.policy)]

    async def verify_audit(self) -> audit.ChainStatus:
        async with self._sessions() as session:
            return await audit.verify_chain(session)

    async def evidence(
        self,
        app_uuid: uuid.UUID,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> evidence_report.Evidence:
        """19.7, counted out of the audit log."""
        async with self._sessions() as session:
            return await evidence_report.report(
                session, app_uuid=app_uuid, since=since, until=until
            )

    # -- 19.2 ----------------------------------------------------------------

    async def call(self, session_id: Any, tool: str, args: dict[str, Any]) -> Outcome:
        try:
            return await self._call(session_id, tool, args)
        except Denied as refusal:
            return Outcome(
                status=ActionStatus.denied.value, ok=False, reason=refusal.reason
            )
        except Exception:
            # 19.2: "Any exception at any step = deny or stop." Nothing about
            # what went wrong is guessed at, and nothing is retried.
            return Outcome(
                status=ActionStatus.denied.value,
                ok=False,
                reason="Something went wrong, so nothing was done.",
            )

    async def _call(self, session_id: Any, tool: str, args: dict[str, Any]) -> Outcome:
        async with self._sessions() as session:
            # 1. authenticate: key/session valid, agent active, switches off.
            caller = await sessions.authenticate(session, session_id=session_id)

            # Steps 2 to 5 can all refuse before step 6 has written anything,
            # and 19.7 asks for those refusals by name on the evidence report,
            # so they are logged under their own event. They are not actions:
            # nothing was received, nothing was decided, nothing ran.
            try:
                connector, table, mode, risk = await self._check(
                    session, caller, tool, args
                )
            except Denied as refusal:
                await self._record_refusal(session, caller, tool, refusal)
                raise

            # 6. audit the intent, and commit it, before anything happens.
            action = Action(
                app_id=caller.app_uuid,
                agent_id=caller.agent_id,
                acts_for=caller.acts_for,
                connector=CONNECTOR,
                tool=tool,
                args=args if isinstance(args, dict) else {},
                policy_version=caller.policy.version,
                decision=mode,
                risk=risk,
                status=ActionStatus.received.value,
            )
            session.add(action)
            await session.flush()
            await audit.append(
                session,
                action_id=action.id,
                event="action.received",
                payload={
                    "agent": str(caller.agent_id),
                    "acts_for": str(caller.acts_for),
                    "tool": tool,
                    "table": table,
                    "risk": risk,
                    # 19.7 counts auto-allowed against approved, and the
                    # evidence report is computed from this log and nothing
                    # else, so the decision has to be in it.
                    "decision": mode,
                    "policy_version": caller.policy.version,
                },
            )
            await session.commit()
            action_id = action.id

        # 7. scoped credentials, for this call only.
        scope = self._scope(caller)
        creds = await connector.scoped_credentials(scope)

        if tool == "db_query":
            # A read has nothing to dry run and nothing to approve, so steps 8
            # to 10 do not apply to it. It is still audited either way.
            return await self._finish(
                action_id,
                tool,
                args,
                connector,
                creds,
                lambda: connector.execute(creds, tool, args, None),
                risk,
            )

        # 8. dry run, and 9. limits, which the connector raises from inside it.
        try:
            dry = await connector.dry_run(creds, tool, args)
        except Denied as refusal:
            return await self._record_denial(action_id, refusal)

        risk = score(tool, dry.affected)
        async with self._sessions() as session:
            stored = await session.get(Action, action_id)
            action_machine.move(
                stored, ActionStatus.dry_run_done, dry_run=dry.as_json(), risk=risk
            )
            await audit.append(
                session,
                action_id=action_id,
                event="action.dry_run",
                payload={
                    "affected": dry.affected,
                    "diff_hash": dry.diff_hash,
                    "risk": risk,
                    "keys": [entry["key"] for entry in dry.diff],
                    "columns": sorted(args.get("values") or {}),
                },
            )

            # 10. approval, if the policy says a person decides.
            if mode == "approve":
                await self._park(session, stored, dry)
                await session.commit()
                return Outcome(
                    status=ActionStatus.pending_approval.value,
                    ok=False,
                    action_id=action_id,
                    risk=risk,
                    affected=dry.affected,
                    diff=dry.diff,
                    reason="This needs a person to approve it before it happens.",
                )

            # 11. execute. The claim is a guarded UPDATE, so an action cannot
            # move into `executing` twice however many callers try.
            if not await action_machine.claim(
                session, action_id, expected=ActionStatus.dry_run_done
            ):
                await session.commit()
                return Outcome(
                    status=ActionStatus.denied.value,
                    ok=False,
                    action_id=action_id,
                    reason="This action has already been run.",
                )
            await session.commit()

        return await self._finish(
            action_id,
            tool,
            args,
            connector,
            creds,
            lambda: connector.execute(creds, tool, args, dry),
            risk,
        )

    # -- steps 2 to 5 --------------------------------------------------------

    async def _check(
        self, session: AsyncSession, caller: Caller, tool: str, args: dict[str, Any]
    ) -> tuple[PostgresConnector, str | None, str, str]:
        """Everything that can say no before anything is recorded."""
        # 2. rate limit, per agent, per minute.
        if not self._limiter.take(
            str(caller.agent_id), per_minute=caller.policy.rate_limit_per_minute
        ):
            raise Denied(
                "rate_limited",
                "This agent has made too many calls in the last minute."
                " Nothing was done; try again shortly.",
            )

        connector = await self._connector(session, caller)

        # 3. validate: the tool has to be one this agent was offered. Names
        # inside `args` are checked by the connector against the schema the
        # deployment proved, before it opens a connection.
        listed = {spec.name for spec in connector.tools(caller.policy)}
        if tool not in listed:
            raise Denied(
                "forbidden_tool",
                f"`{tool}` is not something this agent can do.",
            )

        # 4. policy: auto | approve | forbid, with a reason.
        table = args.get("table") if isinstance(args, dict) else None
        mode = caller.policy.mode_for(CONNECTOR, tool, table)
        if mode == FORBID:
            raise Denied(
                "forbidden_tool",
                f"This agent may not {tool.removeprefix('db_')} `{table}`.",
            )

        # 5. risk, from the fixed table. Scored again after the dry run,
        # because the row count changes the answer for an update.
        return connector, table, mode, score(tool)

    async def _record_refusal(
        self, session: AsyncSession, caller: Caller, tool: str, refusal: Denied
    ) -> None:
        """A refusal with no action to attach it to.

        The app is in the payload, because `audit_log` has no app column: its
        rows are links in one chain, not per-app facts. The evidence report
        picks these up by it.
        """
        await audit.append(
            session,
            event="call.denied",
            payload={
                "app": str(caller.app_uuid),
                "agent": str(caller.agent_id),
                "tool": tool,
                "code": refusal.code,
                "reason": refusal.reason,
            },
        )
        await session.commit()

    # -- steps 12 to 14 ------------------------------------------------------

    async def _finish(
        self, action_id, tool, args, connector, creds, run, risk: str
    ) -> Outcome:
        """Execute, check it really happened, and record what happened.

        Step 13 is the one worth reading: the write is read back, as the same
        person, in a new transaction. A write that cannot be seen afterwards is
        not reported as done — it is undone. So the only way to `verified` is
        to have been seen.
        """
        try:
            result = await run()
        except Denied as refusal:
            return await self._record_denial(action_id, refusal)

        seen = result.ok and await connector.verify(creds, tool, args, result)

        async with self._sessions() as session:
            stored = await session.get(Action, action_id)
            if stored.status == ActionStatus.received.value:
                # The read path never dry ran, so walk it through 7.6's edges
                # rather than jumping over them.
                action_machine.move(stored, ActionStatus.dry_run_done)
                action_machine.move(stored, ActionStatus.executing)

            if seen:
                action_machine.move(
                    stored,
                    ActionStatus.verified,
                    result=result.as_json(),
                    risk=risk,
                    reason=result.reason,
                )
                await audit.append(
                    session,
                    action_id=action_id,
                    event="action.verified",
                    payload={
                        "ok": True,
                        "affected": result.affected,
                        "reason": result.reason,
                    },
                )
                await session.commit()
                return Outcome(
                    status=ActionStatus.verified.value,
                    ok=True,
                    action_id=action_id,
                    reason=result.reason,
                    risk=risk,
                    affected=result.affected,
                    rows=result.rows,
                )

            reason = result.reason or (
                "The change could not be found afterwards, so it was put back."
                if result.ok
                else None
            )
            action_machine.move(
                stored,
                ActionStatus.failed,
                result=result.as_json(),
                risk=risk,
                reason=reason,
            )
            await audit.append(
                session,
                action_id=action_id,
                event="action.failed",
                payload={
                    "ok": False,
                    "affected": result.affected,
                    "verified": False if result.ok else None,
                    "reason": reason,
                },
            )
            await session.commit()

        if not result.ok:
            # Nothing was committed — the connector rolled its own transaction
            # back — so there is nothing to put back.
            return Outcome(
                status=ActionStatus.failed.value,
                ok=False,
                action_id=action_id,
                reason=reason,
                risk=risk,
            )

        return await self._roll_back(action_id, connector, creds, risk, reason)

    async def _roll_back(self, action_id, connector, creds, risk, reason) -> Outcome:
        """A write that happened but could not be seen. Put it back."""
        async with self._sessions() as session:
            stored = await session.get(Action, action_id)
            undone = await connector.undo(creds, stored)
            target = (
                ActionStatus.rolled_back if undone.ok else ActionStatus.undo_failed
            )
            action_machine.move(stored, target, undo=undone.as_json())
            await audit.append(
                session,
                action_id=action_id,
                event=f"action.{target.value}",
                payload={"ok": undone.ok, "reason": undone.reason},
            )
            await session.commit()

        return Outcome(
            status=target.value,
            ok=False,
            action_id=action_id,
            risk=risk,
            reason=reason if undone.ok else (undone.reason or reason),
        )

    # -- 19.4: what a person decides -----------------------------------------

    async def approve(self, action_id: Any, *, approver: str) -> Outcome:
        """Let a parked action happen. Readme.md 19.4.

        The order here is the whole point. Expiry, then who is asking, then
        every switch and the policy as they are *now*, then the dry run again —
        and only then the claim that takes it into `executing`. An approval is
        permission for what the diff showed, so if the diff has moved the
        approval is void and the action goes back to waiting rather than
        proceeding on a description nobody agreed to.
        """
        try:
            return await self._approve(action_id, approver=approver)
        except Denied as refusal:
            return _refusal(action_id, refusal)
        except Exception:
            return Outcome(
                status=ActionStatus.pending_approval.value,
                ok=False,
                action_id=_as_uuid(action_id),
                reason="Something went wrong, so nothing was done.",
            )

    async def _approve(self, action_id: Any, *, approver: str) -> Outcome:
        async with self._sessions() as session:
            stored = await self._waiting(session, action_id)

            if stored.approval_expires_at is None or (
                stored.approval_expires_at <= datetime.now(timezone.utc)
            ):
                action_machine.move(stored, ActionStatus.expired)
                await audit.append(
                    session,
                    action_id=stored.id,
                    event="action.expired",
                    payload={"approver": approver},
                )
                await session.commit()
                return Outcome(
                    status=ActionStatus.expired.value,
                    ok=False,
                    action_id=stored.id,
                    reason=(
                        "This waited more than 15 minutes, so it was dropped."
                        " Nothing was done. Ask for it again if it is still wanted."
                    ),
                )

            await self._may_approve(session, stored, approver)
            caller, connector = await self._resume(session, stored)

            action_id = stored.id
            tool, args, risk = stored.tool, stored.args, stored.risk
            expected = DryRun(**(stored.dry_run or {}))

            action_machine.move(
                stored,
                ActionStatus.approved,
                approved_by=approver,
                approved_at=datetime.now(timezone.utc),
            )
            await audit.append(
                session,
                action_id=action_id,
                event="action.approved",
                payload={"approver": approver, "diff_hash": expected.diff_hash},
            )
            await session.commit()

        scope = self._scope(caller)
        creds = await connector.scoped_credentials(scope)

        # The dry run again, because the approval was for what it showed.
        try:
            now = await connector.dry_run(creds, tool, args)
        except Denied as refusal:
            return await self._record_denial(action_id, refusal)

        if now.diff_hash != expected.diff_hash or now.affected != expected.affected:
            async with self._sessions() as session:
                stored = await session.get(Action, action_id)
                await self._park(session, stored, now)
                await audit.append(
                    session,
                    action_id=action_id,
                    event="action.diff_changed",
                    payload={"was": expected.diff_hash, "now": now.diff_hash},
                )
                await session.commit()
            return Outcome(
                status=ActionStatus.pending_approval.value,
                ok=False,
                action_id=action_id,
                risk=stored.risk,
                affected=now.affected,
                diff=now.diff,
                reason=(
                    "The data has changed since this was checked, so the"
                    " approval no longer covers it. Nothing was done; it needs"
                    " approving again."
                ),
            )

        async with self._sessions() as session:
            if not await action_machine.claim(
                session, action_id, expected=ActionStatus.approved
            ):
                await session.commit()
                return Outcome(
                    status=ActionStatus.denied.value,
                    ok=False,
                    action_id=action_id,
                    reason="This action has already been run.",
                )
            await session.commit()

        return await self._finish(
            action_id,
            tool,
            args,
            connector,
            creds,
            lambda: connector.execute(creds, tool, args, now),
            risk,
        )

    async def reject(self, action_id: Any, *, approver: str) -> Outcome:
        """Say no. Nothing runs, and nothing can revive it."""
        try:
            async with self._sessions() as session:
                stored = await self._waiting(session, action_id)
                await self._may_approve(session, stored, approver)
                action_machine.move(stored, ActionStatus.rejected, approved_by=approver)
                await audit.append(
                    session,
                    action_id=stored.id,
                    event="action.rejected",
                    payload={"approver": approver},
                )
                await session.commit()
                return Outcome(
                    status=ActionStatus.rejected.value,
                    ok=False,
                    action_id=stored.id,
                    reason="A person said no, so nothing was done.",
                )
        except Denied as refusal:
            return _refusal(action_id, refusal)

    async def undo(self, action_id: Any, *, by: str) -> Outcome:
        """Put a verified action back, if nothing has moved since.

        Contract 7.6 allows `verified -> undone` and nothing else, so this
        refuses anything that did not finish, including an action that was
        already undone. The connector does the checking that matters: it only
        restores the before-values if the rows still hold the after-values.
        """
        try:
            return await self._undo(action_id, by=by)
        except Denied as refusal:
            return Outcome(
                status=ActionStatus.denied.value,
                ok=False,
                action_id=_as_uuid(action_id),
                reason=refusal.reason,
            )

    async def _undo(self, action_id: Any, *, by: str) -> Outcome:
        async with self._sessions() as session:
            stored = await self._action(session, action_id)
            if stored.status != ActionStatus.verified.value:
                raise Denied(
                    "not_undoable",
                    f"This action is {stored.status}, so there is nothing to undo.",
                )
            await self._may_approve(session, stored, by)
            caller, connector = await self._resume(session, stored)

        creds = await connector.scoped_credentials(self._scope(caller))
        async with self._sessions() as session:
            stored = await self._action(session, action_id)
            undone = await connector.undo(creds, stored)
            if not undone.ok:
                # Still verified: the undo failing is not the action failing.
                await audit.append(
                    session,
                    action_id=stored.id,
                    event="action.undo_refused",
                    payload={"by": by, "reason": undone.reason},
                )
                await session.commit()
                return Outcome(
                    status=ActionStatus.verified.value,
                    ok=False,
                    action_id=stored.id,
                    reason=undone.reason,
                )

            action_machine.move(stored, ActionStatus.undone, undo=undone.as_json())
            await audit.append(
                session,
                action_id=stored.id,
                event="action.undone",
                payload={"by": by},
            )
            await session.commit()
            return Outcome(
                status=ActionStatus.undone.value, ok=True, action_id=stored.id
            )

    async def status_of(self, session_id: Any, action_id: Any) -> dict[str, Any]:
        """One action, as the agent that asked for it may see it. Section 21.

        Scoped to the calling agent, not the app: an agent asking after an
        action it did not ask for is told there is no such action, which is the
        same sentence an invented id gets. The dry run is summarised rather
        than returned, because an agent that is waiting for approval has
        already been shown the diff it asked about, and the rows in there are
        the ones a second agent must not be handed by guessing ids.
        """
        async with self._sessions() as session:
            caller = await sessions.authenticate(session, session_id=session_id)
            stored = await self._action(session, action_id)
            if stored.agent_id != caller.agent_id:
                raise Denied("unknown_action", "There is no such action.")
            return {
                "action_id": str(stored.id),
                "tool": stored.tool,
                "status": stored.status,
                "risk": stored.risk,
                "reason": stored.reason,
                "affected": (stored.dry_run or {}).get("affected"),
                "waiting_until": (
                    stored.approval_expires_at.isoformat()
                    if stored.approval_expires_at
                    and stored.status == ActionStatus.pending_approval.value
                    else None
                ),
            }

    async def pending(self, app_uuid: uuid.UUID) -> list[Action]:
        """What is waiting for a person, newest last. For the dashboard."""
        async with self._sessions() as session:
            return list(
                (
                    await session.execute(
                        select(Action)
                        .where(
                            Action.app_id == app_uuid,
                            Action.status == ActionStatus.pending_approval.value,
                        )
                        .order_by(Action.created_at)
                    )
                )
                .scalars()
                .all()
            )

    async def activity(self, app_uuid: uuid.UUID, *, limit: int = 50) -> list[Action]:
        """What this app's agents did, newest first. For the dashboard.

        Not the audit log: this is the action rows, which carry the arguments
        and the result, and which 19.3 keeps for 30 days. The audit log is the
        thing that proves none of it was edited, and is read by
        `verify_audit` instead.
        """
        async with self._sessions() as session:
            return list(
                (
                    await session.execute(
                        select(Action)
                        .where(Action.app_id == app_uuid)
                        .order_by(Action.created_at.desc())
                        .limit(max(1, min(limit, 500)))
                    )
                )
                .scalars()
                .all()
            )

    # -- the pieces the approval flow is made of -----------------------------

    async def _park(self, session: AsyncSession, stored: Action, dry: DryRun) -> None:
        """Wait for a person, until `APPROVAL_WINDOW` runs out.

        Used both when the action first needs approving and when an approval is
        voided by a changed diff. In the second case the stored dry run is
        replaced, so that what the next person is shown — and what their
        approval is then bound to — is the database as it is now, not as it was
        when the agent asked.
        """
        action_machine.move(
            stored,
            ActionStatus.pending_approval,
            dry_run=dry.as_json(),
            risk=score(stored.tool, dry.affected),
            approval_expires_at=datetime.now(timezone.utc) + APPROVAL_WINDOW,
        )
        await audit.append(
            session,
            action_id=stored.id,
            event="action.pending_approval",
            payload={"affected": dry.affected, "diff_hash": dry.diff_hash},
        )

    async def _action(self, session: AsyncSession, action_id: Any) -> Action:
        stored = (
            await session.get(Action, _as_uuid(action_id))
            if _as_uuid(action_id)
            else None
        )
        if stored is None:
            raise Denied("unknown_action", "There is no such action.")
        return stored

    async def _waiting(self, session: AsyncSession, action_id: Any) -> Action:
        stored = await self._action(session, action_id)
        if stored.status != ActionStatus.pending_approval.value:
            # Including an action that has already run: "approving twice does
            # nothing" is this sentence.
            raise Denied(
                "not_waiting",
                f"This action is {stored.status}, so it is not waiting for a"
                " decision. Nothing was done.",
            )
        return stored

    async def _may_approve(
        self, session: AsyncSession, stored: Action, approver: str
    ) -> None:
        """19.4: an app admin, and a person.

        The lookup is against this app's own people, so an admin of another app
        is a stranger here, and an agent is nobody at all: agents have no row
        in `app_users` to be found by.
        """
        person = (
            await session.execute(
                select(AppUser).where(
                    AppUser.app_id == stored.app_id,
                    AppUser.email == approver,
                    AppUser.role == APPROVER_ROLE,
                    AppUser.disabled.is_(False),
                )
            )
        ).scalar_one_or_none()
        if person is None:
            raise Denied(
                "not_an_approver",
                "Only an admin of this app can decide this, so nothing was done.",
            )
        if person.id == stored.acts_for:
            # An agent approving its own work through the person it acts for
            # would make the approval a formality.
            raise Denied(
                "not_an_approver",
                "The person an agent acts for cannot approve that agent's own"
                " action. Nothing was done.",
            )

    async def _resume(
        self, session: AsyncSession, stored: Action
    ) -> tuple[Caller, PostgresConnector]:
        """Rebuild who the action belongs to, with every check made again."""
        caller = await sessions.for_agent(session, agent_id=stored.agent_id)
        table = stored.args.get("table") if isinstance(stored.args, dict) else None
        if caller.policy.mode_for(stored.connector, stored.tool, table) == FORBID:
            raise Denied(
                "forbidden_tool",
                "This agent's policy no longer allows that, so nothing was done.",
            )
        return caller, await self._connector(session, caller)

    async def _record_denial(self, action_id: uuid.UUID, refusal: Denied) -> Outcome:
        """A refusal that arrived after the action was recorded.

        Where it lands depends on where it happened: a refusal during the dry
        run is a denial, one during execution is a failure, because contract
        7.6 has no edge from `executing` to `denied` and inventing one would
        make "it executed at most once" unreadable afterwards.
        """
        async with self._sessions() as session:
            stored = await session.get(Action, action_id)
            executing = stored.status == ActionStatus.executing.value
            target = ActionStatus.failed if executing else ActionStatus.denied
            action_machine.move(stored, target, reason=refusal.reason)
            await audit.append(
                session,
                action_id=action_id,
                event="action.failed" if executing else "action.denied",
                payload={"code": refusal.code, "reason": refusal.reason},
            )
            await session.commit()
        return Outcome(
            status=target.value,
            ok=False,
            action_id=action_id,
            reason=refusal.reason,
        )

    # -- what the connector is pointed at ------------------------------------

    def _scope(self, caller: Caller) -> Scope:
        """Section 21: the gateway may read `agent-*` and nothing else. It has
        no way to reach the runtime, migrator or sidecar secret from here."""
        try:
            agent = self._store.get(
                secret_store.secret_name(caller.app_id, secret_store.AGENT)
            )
        except secret_store.SecretNotFound as exc:
            raise Denied(
                "no_credentials",
                "There is no agent login for this app, so nothing was done.",
            ) from exc
        return Scope(
            dsn=agent["dsn"],
            schema=caller.app_id,
            acts_for=caller.principal_key,
            role=caller.role,
        )

    async def _connector(
        self, session: AsyncSession, caller: Caller
    ) -> PostgresConnector:
        """Built from the schema the live deployment proved, never from the
        database as it is now. A table added behind our back is unreachable
        until it has been through a deploy and survived the attack suite."""
        app = await session.get(App, caller.app_uuid)
        if app is None or app.live_deployment_id is None:
            raise Denied(
                "not_live",
                "This app has nothing live, so there is nothing to act on.",
            )

        cached = self._catalogs.get(caller.app_uuid)
        if cached is None or cached[0] != app.live_deployment_id:
            deployment = await session.get(Deployment, app.live_deployment_id)
            if deployment is None or not deployment.schema_graph:
                raise Denied(
                    "no_schema",
                    "This app's shape was never recorded, so an agent has no"
                    " list of things it may name.",
                )
            cached = (
                app.live_deployment_id,
                SchemaCatalog.from_graph(caller.app_id, deployment.schema_graph),
            )
            self._catalogs[caller.app_uuid] = cached

        return PostgresConnector(catalog=cached[1], limits=caller.policy.limits)
