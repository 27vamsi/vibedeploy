"""M2 acceptance: identity survives a transaction pooler. Readme.md section 8.

500 concurrent requests spread over 5 users, through PgBouncer in transaction
mode, against an endpoint whose SQL has no WHERE clause. Every response must
contain exactly the caller's rows.

The negative test runs the identical load against the leaky app, and fails if
no leak is found - that is what proves this test can fail.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from .conftest import Fixture

REQUESTS = 500


async def _fire(server, fixture: Fixture) -> list[tuple[uuid.UUID, set[str]]]:
    plan = [fixture.users[i % len(fixture.users)] for i in range(REQUESTS)]
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=50)
    async with httpx.AsyncClient(
        base_url=server.url, limits=limits, timeout=60
    ) as client:

        async def one(user: uuid.UUID):
            response = await client.get(
                "/notes", headers={"X-VD-Identity": fixture.header(user)}
            )
            response.raise_for_status()
            return user, set(response.json()["ids"])

        return await asyncio.gather(*(one(user) for user in plan))


async def test_shim_keeps_every_response_to_its_own_user(app_server, pool_fixture):
    server = app_server("shim")
    for user, seen in await _fire(server, pool_fixture):
        assert seen == pool_fixture.notes[user]


async def test_shim_reports_itself_active_on_startup(app_server):
    server = app_server("shim")
    assert (
        "vibedeploy-shim active lang=python db=sqlalchemy framework=fastapi"
        in server.logs()
    )


async def test_request_without_identity_sees_nothing(app_server):
    server = app_server("shim")
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as client:
        assert (await client.get("/notes")).json()["ids"] == []


async def test_forged_identity_sees_nothing(app_server, pool_fixture):
    """Wrong key, right shape. Fail closed, not fail open."""
    from vibedeploy_shim.identity import make_header

    forged = make_header(
        app="app_pooltest",
        sub=str(pool_fixture.users[0]),
        role="member",
        key=b"\x00" * 32,
    )
    server = app_server("shim")
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as client:
        response = await client.get("/notes", headers={"X-VD-Identity": forged})
        assert response.json()["ids"] == []


@pytest.mark.negative
async def test_the_test_can_fail(app_server, pool_fixture):
    """Session-level identity under transaction pooling must be caught."""
    server = app_server("leaky")
    results = await _fire(server, pool_fixture)

    leaked = [
        (user, seen - pool_fixture.notes[user])
        for user, seen in results
        if seen - pool_fixture.notes[user]
    ]
    assert leaked, (
        "the leaky app did not leak, so this suite cannot prove the shim is "
        "what keeps responses separate"
    )
