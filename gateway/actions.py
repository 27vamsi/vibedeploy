"""Contract 7.6's status machine. Frozen; do not change without asking.

    received        -> denied | dry_run_done
    dry_run_done    -> executing | pending_approval
    pending_approval-> approved | rejected | expired
    approved        -> executing
    executing       -> verified | failed
    failed          -> rolled_back | undo_failed
    verified        -> undone            (only via an explicit undo)

No other transitions. The contract's other sentence, "an action executes at most
once", is not something a table of edges can promise on its own: two callers can
both read `approved` and both think they may go. So the move into `executing` is
not done here at all. It is done by `claim`, a single UPDATE that names the
status it expects to replace, and the loser of that race gets nothing back.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models import Action, ActionStatus
from gateway.errors import StatusError

S = ActionStatus

LEGAL: dict[ActionStatus, frozenset[ActionStatus]] = {
    S.received: frozenset({S.denied, S.dry_run_done}),
    S.dry_run_done: frozenset({S.executing, S.pending_approval, S.denied}),
    S.pending_approval: frozenset({S.approved, S.rejected, S.expired}),
    S.approved: frozenset({S.executing, S.pending_approval, S.expired}),
    S.executing: frozenset({S.verified, S.failed}),
    S.failed: frozenset({S.rolled_back, S.undo_failed}),
    S.verified: frozenset({S.undone}),
    S.denied: frozenset(),
    S.rejected: frozenset(),
    S.expired: frozenset(),
    S.rolled_back: frozenset(),
    S.undo_failed: frozenset(),
    S.undone: frozenset(),
}

# Nothing more will happen to an action in one of these on its own.
FINISHED = frozenset(
    {S.denied, S.rejected, S.expired, S.rolled_back, S.undo_failed, S.undone}
)


def can(current: str, target: ActionStatus) -> bool:
    try:
        return target in LEGAL[ActionStatus(current)]
    except ValueError:
        return False


def move(action: Action, target: ActionStatus, **fields: Any) -> Action:
    """Change an action's status, or refuse loudly.

    `dry_run_done -> denied` is in the table because a limit check (19.2 step 9)
    happens after the dry run and still has to be able to say no.
    """
    if not can(action.status, target):
        raise StatusError(
            f"An action cannot go from {action.status} to {target.value}."
        )
    action.status = target.value
    for name, value in fields.items():
        setattr(action, name, value)
    action.updated_at = datetime.now(timezone.utc)
    return action


async def claim(
    session: AsyncSession, action_id: uuid.UUID, *, expected: ActionStatus
) -> bool:
    """Take an action into `executing`, once, or return False.

    The guard is the WHERE clause, not a read followed by a write. Two approvals
    arriving together both see `approved`; only one of them changes a row.
    """
    if ActionStatus.executing not in LEGAL[expected]:
        raise StatusError(f"An action cannot go from {expected.value} to executing.")
    result = await session.execute(
        update(Action)
        .where(Action.id == action_id, Action.status == expected.value)
        .values(
            status=ActionStatus.executing.value,
            updated_at=datetime.now(timezone.utc),
        )
        .returning(Action.id)
    )
    return result.scalar_one_or_none() is not None
