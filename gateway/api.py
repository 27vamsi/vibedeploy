"""What the console asks the gateway to do. Readme.md 19.4, 19.5, 19.7, 23.

The dashboard shows pending approvals, the audit chain and the evidence report,
and none of those can be computed by reading the control plane database alone:

  - approving redoes the dry run (19.4), which means connecting to the app's
    database as its `agent` role,
  - undoing runs real SQL against it,
  - and both have to be audited by the thing that holds the chain.

All three need `vd/apps/*/agent-*`, and CLAUDE.md gives that to the gateway and
to nothing else. So the console does not read the secret and do the work; it
asks the process that already holds it. In AWS the two are separate tasks with
separate roles, and that separation is only worth something if the console's
role genuinely cannot reach an agent secret.

This is not a second pipeline. Every handler here is three lines around one
`Gateway` method, and the decisions — is this action waiting, is this person an
admin of this app, has the diff changed, are the switches still off — are all
made in `gateway.pipeline`, on the same code path an agent's own call takes.

**Never reachable from the public internet.** One shared key, compared with
`hmac.compare_digest`, and a refusal says nothing but no.
"""

from __future__ import annotations

import hmac
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from gateway.pipeline import Gateway

# No prefix: this router is the whole of a sub-application that the gateway
# mounts at `/internal`, so a prefix here would make it `/internal/internal`.
router = APIRouter(tags=["internal"])


def get_gateway(request: Request) -> Gateway:
    return request.app.state.gateway


def check_key(
    request: Request,
    x_vd_console_key: Annotated[str | None, Header()] = None,
) -> None:
    """One key, constant-time, and no key configured means no entry.

    An unset key is the dangerous case: without this check an empty header
    would match an empty setting and the whole internal API would be open. It
    is refused rather than defaulted, because a gateway that was never told its
    key has not been told it may be administered.
    """
    expected = request.app.state.console_key
    if not expected or not x_vd_console_key:
        raise HTTPException(status_code=403, detail="no")
    if not hmac.compare_digest(x_vd_console_key, expected):
        raise HTTPException(status_code=403, detail="no")


Guarded = Depends(check_key)


class Decision(BaseModel):
    """Who is deciding. The gateway checks that they may; this is only the claim."""

    approver: str


def _outcome(outcome: Any) -> dict[str, Any]:
    return outcome.as_json()


# ---------------------------------------------------------------------------
# 19.4
# ---------------------------------------------------------------------------


@router.get("/apps/{app_uuid}/pending", dependencies=[Guarded])
async def pending(
    app_uuid: uuid.UUID, gateway: Annotated[Gateway, Depends(get_gateway)]
) -> list[dict[str, Any]]:
    """Everything waiting for a person, with what 19.4 says to show them."""
    return [
        {
            "action_id": str(action.id),
            "agent_id": str(action.agent_id),
            "acts_for": str(action.acts_for),
            "tool": action.tool,
            "args": action.args,
            "risk": action.risk,
            "reason": action.reason,
            "affected": (action.dry_run or {}).get("affected"),
            "diff": (action.dry_run or {}).get("diff") or [],
            "diff_hash": (action.dry_run or {}).get("diff_hash"),
            "expires_at": (
                action.approval_expires_at.isoformat()
                if action.approval_expires_at
                else None
            ),
            "created_at": action.created_at.isoformat(),
        }
        for action in await gateway.pending(app_uuid)
    ]


@router.post("/actions/{action_id}/approve", dependencies=[Guarded])
async def approve(
    action_id: str,
    decision: Annotated[Decision, Body()],
    gateway: Annotated[Gateway, Depends(get_gateway)],
) -> dict[str, Any]:
    """19.4: one click approves one action. There is no bulk approve to call."""
    return _outcome(await gateway.approve(action_id, approver=decision.approver))


@router.post("/actions/{action_id}/reject", dependencies=[Guarded])
async def reject(
    action_id: str,
    decision: Annotated[Decision, Body()],
    gateway: Annotated[Gateway, Depends(get_gateway)],
) -> dict[str, Any]:
    return _outcome(await gateway.reject(action_id, approver=decision.approver))


@router.post("/actions/{action_id}/undo", dependencies=[Guarded])
async def undo(
    action_id: str,
    decision: Annotated[Decision, Body()],
    gateway: Annotated[Gateway, Depends(get_gateway)],
) -> dict[str, Any]:
    return _outcome(await gateway.undo(action_id, by=decision.approver))


# ---------------------------------------------------------------------------
# 19.6, 19.7
# ---------------------------------------------------------------------------


@router.get("/audit", dependencies=[Guarded])
async def audit(gateway: Annotated[Gateway, Depends(get_gateway)]) -> dict[str, Any]:
    return (await gateway.verify_audit()).as_json()


@router.get("/apps/{app_uuid}/evidence", dependencies=[Guarded])
async def evidence(
    app_uuid: uuid.UUID,
    gateway: Annotated[Gateway, Depends(get_gateway)],
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    """19.7, downloadable as it stands: the report is already JSON."""
    report = await gateway.evidence(app_uuid, since=since, until=until)
    return {**report.as_json(), "summary": report.summary()}


@router.get("/apps/{app_uuid}/activity", dependencies=[Guarded])
async def activity(
    app_uuid: uuid.UUID,
    gateway: Annotated[Gateway, Depends(get_gateway)],
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Section 23's activity page: what the agent did, and why.

    `undoable` is a hint for whether to draw the button, not a decision. The
    decision is `Gateway.undo`'s, which refuses a read, an action that never
    ran, and one whose rows have moved since — so a button drawn in error ends
    in a plain refusal rather than in something happening.
    """
    return [
        {
            "action_id": str(action.id),
            "agent_id": str(action.agent_id),
            "tool": action.tool,
            "args": action.args,
            "status": action.status,
            "decision": action.decision,
            "risk": action.risk,
            "reason": action.reason,
            "affected": (action.result or {}).get("affected"),
            "approved_by": action.approved_by,
            "created_at": action.created_at.isoformat(),
            "undoable": action.status == "verified" and action.tool != "db_query",
        }
        for action in await gateway.activity(app_uuid, limit=limit)
    ]
