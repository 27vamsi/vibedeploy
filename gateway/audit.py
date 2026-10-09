"""The tamper-evident audit log. Readme.md 19.6.

`hash = sha256(prev_hash || canonical_json(row without hash))`. Each row commits
to the one before it, so editing any row breaks every hash after it and
`verify_chain` reports the first break. It does not stop somebody with database
access from editing a row; it stops them from doing it quietly, which is the
property the evidence report of 19.7 rests on.

Three things make that work and are easy to get wrong:

  - **Nothing is ever updated.** The row is hashed before it is written, not
    after, so the id is taken from the sequence first. There is no second pass
    to set the hash, which is just as well: the migration puts a trigger on the
    table that refuses UPDATE and DELETE outright.
  - **One appender at a time.** `prev_hash` is read and used in the same
    transaction, under an advisory lock, so two calls cannot chain onto the
    same predecessor and fork the chain.
  - **The payload is normalised before it is hashed.** What is hashed and what
    is stored have to be the same bytes, so the payload goes through canonical
    JSON once and the result is what lands in the column.

Payloads carry primary keys, changed column names and the `diff_hash` — never
row values. Full diffs live in `actions` for 30 days and are deleted; the hash
stays for ever, so an old action's evidence survives its data.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models import AuditLog

# What the first row chains onto. Not a hash of anything: it exists so that the
# rule "every row has a prev_hash" has no exception to special-case.
GENESIS = "0" * 64

# Any constant; it only has to be the same in every gateway process.
_APPEND_LOCK = 0x76446761  # "vDga"

_NEXT_ID = text(
    "SELECT nextval(pg_get_serial_sequence('vd_control.audit_log', 'id'))"
)


def canonical(value: Any) -> str:
    """Sorted keys, no spaces. The same dict always produces the same bytes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _jsonable(value: Any) -> Any:
    return json.loads(canonical(value))


def _content(
    *,
    entry_id: int,
    ts: datetime,
    action_id: uuid.UUID | None,
    event: str,
    payload: Any,
    prev_hash: str,
) -> dict[str, Any]:
    """The row, without its hash, in the shape the chain commits to.

    `ts` is forced to UTC rather than used as it arrives. Postgres hands a
    `timestamptz` back in whatever the session's timezone is, and a verifier
    running in a different one would otherwise recompute different bytes and
    report a break that is not there.
    """
    return {
        "id": entry_id,
        "ts": ts.astimezone(timezone.utc).isoformat(),
        "action_id": str(action_id) if action_id else None,
        "event": event,
        "payload_json": payload,
        "prev_hash": prev_hash,
    }


def digest(prev_hash: str, content: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical(content)).encode()).hexdigest()


async def append(
    session: AsyncSession,
    *,
    event: str,
    payload: Any = None,
    action_id: uuid.UUID | None = None,
) -> AuditLog:
    """Add one link. The caller commits, and must commit before acting.

    Readme.md 19.2 step 6: the "about to do this" record is written **before**
    anything happens, and a write that fails denies the call. That ordering is
    only real if the transaction holding it has been committed, so callers use
    a session of their own for this and nothing else.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _APPEND_LOCK}
    )
    previous = (
        await session.execute(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1))
    ).scalar_one_or_none()
    prev_hash = previous or GENESIS

    entry_id = (await session.execute(_NEXT_ID)).scalar_one()
    ts = datetime.now(timezone.utc)
    body = _jsonable(payload if payload is not None else {})
    content = _content(
        entry_id=entry_id,
        ts=ts,
        action_id=action_id,
        event=event,
        payload=body,
        prev_hash=prev_hash,
    )

    entry = AuditLog(
        id=entry_id,
        ts=ts,
        action_id=action_id,
        event=event,
        payload_json=body,
        prev_hash=prev_hash,
        hash=digest(prev_hash, content),
    )
    session.add(entry)
    await session.flush()
    return entry


@dataclass(frozen=True)
class ChainStatus:
    """What `/audit/verify` answers. Section 19.7 shows it on the report."""

    intact: bool
    entries: int
    broken_at: int | None = None
    detail: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "intact": self.intact,
            "entries": self.entries,
            "broken_at": self.broken_at,
            "detail": self.detail,
        }


async def verify_chain(session: AsyncSession) -> ChainStatus:
    """Recompute every link and report the first one that does not hold."""
    rows = (
        (await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
    )
    prev_hash = GENESIS
    for index, row in enumerate(rows):
        if row.prev_hash != prev_hash:
            return ChainStatus(
                intact=False,
                entries=index,
                broken_at=row.id,
                detail="This entry does not follow the one before it.",
            )
        content = _content(
            entry_id=row.id,
            ts=row.ts,
            action_id=row.action_id,
            event=row.event,
            payload=row.payload_json,
            prev_hash=row.prev_hash,
        )
        if digest(prev_hash, content) != row.hash:
            return ChainStatus(
                intact=False,
                entries=index,
                broken_at=row.id,
                detail="This entry has been changed since it was written.",
            )
        prev_hash = row.hash
    return ChainStatus(intact=True, entries=len(rows))
