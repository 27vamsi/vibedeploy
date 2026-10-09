"""One-time build job tokens. Contract 7.4.

A build job runs untrusted migrations, so it is the least trusted process we
operate. It gets no database credentials for the control plane and no long-lived
key: it gets one token, for one deployment, for one phase, that stops working
the moment a result is recorded against it.

Only the hash is stored. `spend` sets `used_at` in the same UPDATE that checks
it is unset, so two callbacks racing to report different verdicts cannot both
win.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models import BuildPhase, BuildToken

TTL = timedelta(hours=1)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def mint(
    session: AsyncSession, *, deployment_id: uuid.UUID, phase: BuildPhase
) -> str:
    """Return the token. This is the only time it exists in readable form."""
    token = secrets.token_urlsafe(32)
    session.add(
        BuildToken(
            deployment_id=deployment_id,
            phase=phase.value,
            token_hash=_hash(token),
            expires_at=datetime.now(timezone.utc) + TTL,
        )
    )
    await session.flush()
    return token


async def spend(
    session: AsyncSession, *, deployment_id: uuid.UUID, token: str
) -> BuildPhase | None:
    """Consume the token, returning the phase it was for.

    None means: unknown token, wrong deployment, expired, or already used. The
    caller must treat every one of those the same way, because telling them
    apart is how a caller learns which guess was close.
    """
    digest = _hash(token)
    now = datetime.now(timezone.utc)
    result = await session.execute(
        update(BuildToken)
        .where(
            BuildToken.deployment_id == deployment_id,
            BuildToken.token_hash == digest,
            BuildToken.used_at.is_(None),
            BuildToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(BuildToken.phase)
    )
    phase = result.scalar_one_or_none()
    return BuildPhase(phase) if phase is not None else None
