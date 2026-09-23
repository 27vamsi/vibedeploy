"""The pooling proof. Readme.md section 8 M2.

500 concurrent requests, 5 users, one connection pool, PgBouncer in transaction
mode. Every response must contain exactly the requester's own rows, from an
endpoint whose SQL has no WHERE clause.

Never delete or skip anything in this file to make CI green (CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from vibedeploy_shim.identity import sign_identity

REQUESTS = 500
RUNS = 10


def header_for(deployment, user_id) -> str:
    now = int(time.time())
    return sign_identity(
        {
            "v": 1,
            "app": deployment.roles.app_id,
            "sub": str(user_id),
            "role": "member",
            "iat": now,
            "exp": now + 60,
        },
        deployment.identity_key.encode(),
    )


async def _hammer(deployment, base_url, count):
    """Fire `count` requests concurrently, round-robin across the users."""
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=50)
    async with httpx.AsyncClient(
        base_url=base_url, limits=limits, timeout=30
    ) as client:
        async def one(i):
            user_id = deployment.users[i % len(deployment.users)]
            response = await client.get(
                "/notes", headers={"X-VD-Identity": header_for(deployment, user_id)}
            )
            response.raise_for_status()
            return user_id, set(response.json()["ids"])

        return await asyncio.gather(*(one(i) for i in range(count)))


@pytest.mark.parametrize("run", range(RUNS))
async def test_every_response_contains_only_its_own_rows(deployment, server, run):
    """The headline claim, ten runs in a row."""
    results = await _hammer(deployment, server.base_url, REQUESTS)
    assert len(results) == REQUESTS

    for user_id, got in results:
        expected = deployment.notes[user_id]
        assert got == expected, (
            f"user {user_id} got {len(got)} ids, expected {len(expected)}; "
            f"foreign ids: {sorted(got - expected)}"
        )


async def test_the_shim_installed_itself_in_the_app_process(server):
    """The app never imports the shim; PYTHONPATH does it. Readme.md section 14."""
    assert "vibedeploy-shim active" in server.output()
    assert "db=sqlalchemy" in server.output()
    assert "framework=fastapi" in server.output()


async def test_a_request_with_no_identity_gets_nothing(deployment, server):
    async with httpx.AsyncClient(base_url=server.base_url, timeout=30) as client:
        response = await client.get("/notes")
    response.raise_for_status()
    assert response.json()["ids"] == []


async def test_a_forged_identity_gets_nothing(deployment, server):
    """Signed with the wrong key: the shim refuses it, so no identity is set."""
    forged = sign_identity(
        {
            "v": 1,
            "app": deployment.roles.app_id,
            "sub": str(deployment.users[0]),
            "role": "member",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        b"not-the-right-key",
    )
    async with httpx.AsyncClient(base_url=server.base_url, timeout=30) as client:
        response = await client.get("/notes", headers={"X-VD-Identity": forged})
    response.raise_for_status()
    assert response.json()["ids"] == []


@pytest.mark.negative
async def test_the_harness_can_actually_detect_a_leak(deployment, leaky_server):
    """Negative control.

    Same app, same pooler, but identity is set at session scope and committed.
    PgBouncer then hands that server connection to another user's transaction.
    If this ever stops leaking, the positive test above proves nothing and must
    not be trusted.
    """
    results = await _hammer(deployment, leaky_server.base_url, REQUESTS)

    leaked = [
        (user_id, got - deployment.notes[user_id])
        for user_id, got in results
        if got - deployment.notes[user_id]
    ]
    assert leaked, (
        "expected the session-scoped variant to leak across pooled connections; "
        "if it no longer does, this test has stopped being a control"
    )
