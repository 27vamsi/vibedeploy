"""Agent keys. Readme.md 19.1.

`vd_agent_<key id>.<secret>`. The id half is a lookup, the secret half is the
only part that proves anything, and only its argon2 hash is stored. Carrying the
id means verifying a key is one row and one hash comparison; without it the
gateway would have to argon2-verify against every key it has ever issued, which
is slow enough that an attacker could tell how many there are by timing it.

The key is returned exactly once, by `mint`. There is no endpoint that shows it
again and no column it could be read out of.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models import AgentKey
from control_plane.passwords import hash_password, verify_password

PREFIX = "vd_agent_"


@dataclass(frozen=True)
class MintedKey:
    key_id: uuid.UUID
    key: str


async def mint(session: AsyncSession, *, agent_id: uuid.UUID) -> MintedKey:
    secret = secrets.token_urlsafe(32)
    row = AgentKey(agent_id=agent_id, key_hash=hash_password(secret))
    session.add(row)
    await session.flush()
    return MintedKey(key_id=row.id, key=f"{PREFIX}{row.id.hex}.{secret}")


def _split(key: str) -> tuple[uuid.UUID, str] | None:
    if not isinstance(key, str) or not key.startswith(PREFIX):
        return None
    body = key[len(PREFIX) :]
    head, _, secret = body.partition(".")
    if not secret:
        return None
    try:
        return uuid.UUID(hex=head), secret
    except ValueError:
        return None


async def resolve(session: AsyncSession, key: str) -> AgentKey | None:
    """The key row this key proves, or None.

    Every way of failing returns None: malformed, unknown, revoked, wrong
    secret. Telling them apart is how a caller learns which guess was close.
    """
    parts = _split(key)
    if parts is None:
        return None
    key_id, secret = parts
    row = (
        await session.execute(
            select(AgentKey).where(AgentKey.id == key_id, AgentKey.revoked_at.is_(None))
        )
    ).scalar_one_or_none()
    if row is None or not verify_password(row.key_hash, secret):
        return None
    return row


async def revoke_all(session: AsyncSession, *, agent_id: uuid.UUID) -> None:
    await session.execute(
        update(AgentKey)
        .where(AgentKey.agent_id == agent_id, AgentKey.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
