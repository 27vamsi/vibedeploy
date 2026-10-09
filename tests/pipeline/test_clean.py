"""The half of M7 that has to work: a clean branch reaches a browser.

The proof is not that the pipeline said `live`. It is that two people opening
the same URL see two different lists, out of an app whose only query is
`SELECT id, title, done FROM todos` with no WHERE clause at all.
"""

from __future__ import annotations

import pytest_asyncio

from control_plane.models import DeploymentStatus, Protection
from tests.pipeline.conftest import settle, sign_in

ALICE = ("alice@example.com", "alice-password-1")
BOB = ("bob@example.com", "bob-password-2")


@pytest_asyncio.fixture(scope="session")
async def live(pipeline):
    """Deploy `clean` once and confirm it. Everything below reads this run."""
    deployment_id = await pipeline.deploy("clean")

    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.awaiting_confirmation.value, (
        deployment.blocked_reason
    )
    assert not deployment.questions, "a clean schema should not need asking about"

    await pipeline.confirm(deployment_id)
    return deployment_id


async def test_the_pipeline_stopped_to_be_confirmed(pipeline, live):
    """Nothing was built until a person said yes. Section 11 step 6."""
    model = await pipeline.model_of(live)
    assert model.confirmed_by == "builder@localhost"
    assert model.confirmed_at is not None
    assert model.model_json["confirmed_by"] == "builder@localhost"


async def test_clean_branch_is_live(pipeline, live):
    deployment = await pipeline.deployment(live)
    assert deployment.status == DeploymentStatus.live.value, deployment.blocked_reason
    assert deployment.blocked_reason is None
    assert deployment.endpoint

    app = await pipeline.app_of(live)
    assert app.protection == Protection.protected.value
    assert app.live_deployment_id == live
    assert app.stack["language"] == "python"
    assert app.stack["db_lib"] == "sqlalchemy"


async def test_the_job_finished_rather_than_parking(pipeline, live):
    job = await pipeline.job_of(live)
    assert job.status == "done"
    assert job.last_error is None


async def test_runtime_and_agent_were_attacked_identically(pipeline, live):
    """Section 13: the agent is the same security model under another role."""
    runs = await pipeline.runs(live)
    assert set(runs) == {"runtime", "agent"}
    assert runs["runtime"].failed == 0
    assert runs["agent"].failed == 0
    assert runs["runtime"].total == runs["agent"].total
    assert runs["runtime"].passed == runs["agent"].passed
    assert runs["runtime"].total > 0


async def test_two_people_see_only_their_own_todos(pipeline, live):
    app = await pipeline.app_of(live)
    endpoint = (await pipeline.deployment(live)).endpoint

    alice = await pipeline.add_person(live, *ALICE)
    bob = await pipeline.add_person(live, *BOB)

    # The worker inserted them as the migrator and wrote back the key the
    # database chose, so the placeholder is gone and they can log in.
    assert not alice.disabled and not bob.disabled
    assert alice.principal_key != bob.principal_key

    await pipeline.give_todo(app.app_id, alice.principal_key, "alice buys milk")
    await pipeline.give_todo(app.app_id, alice.principal_key, "alice calls mum")
    await pipeline.give_todo(app.app_id, bob.principal_key, "bob books a flight")

    await settle()
    hers = await sign_in(endpoint, *ALICE)
    his = await sign_in(endpoint, *BOB)
    try:
        assert [t["title"] for t in (await hers.get("/todos")).json()["todos"]] == [
            "alice buys milk",
            "alice calls mum",
        ]
        assert [t["title"] for t in (await his.get("/todos")).json()["todos"]] == [
            "bob books a flight"
        ]
        assert (await hers.get("/whoami")).json()["sub"] == alice.principal_key
    finally:
        await hers.aclose()
        await his.aclose()


async def test_nobody_signed_in_sees_nothing(pipeline, live):
    """Fail closed: no identity is zero rows, never everything."""
    import httpx

    endpoint = (await pipeline.deployment(live)).endpoint
    async with httpx.AsyncClient(
        base_url=endpoint, follow_redirects=False, timeout=20
    ) as anonymous:
        response = await anonymous.get("/todos")
    # The gate never lets the request reach the app at all.
    assert response.status_code in (302, 303, 401)
    assert "alice" not in response.text
