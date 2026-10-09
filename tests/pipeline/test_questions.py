"""The pause. M7's actual subject.

`plans` has no way back to `users`. A price list everybody may read and a table
of secrets only an admin may see look identical from the schema, so the pipeline
stops. What matters here is that it stops *without* deciding: the job is
`waiting`, not retried, not timed out into a default, and nothing exists in the
real database yet. It moves when somebody answers.
"""

from __future__ import annotations

import pytest_asyncio

from control_plane.models import DeploymentStatus, JobStatus


@pytest_asyncio.fixture(scope="session")
async def parked(pipeline):
    """One parked deployment, left parked. The tests below only read it."""
    return await pipeline.deploy("unlinked")


async def test_it_stopped_and_asked(pipeline, parked):
    deployment = await pipeline.deployment(parked)
    assert deployment.status == DeploymentStatus.awaiting_answers.value

    asked = {q["id"]: q for q in deployment.questions}
    assert "unlinked.plans" in asked, asked
    question = asked["unlinked.plans"]
    assert question["table"] == "plans"
    assert set(question["options"]) == {"shared", "read_only_shared", "admin_only"}
    assert "plans" in question["text"]


async def test_nothing_was_decided_while_it_waits(pipeline, parked):
    job = await pipeline.job_of(parked)
    assert job.status == JobStatus.waiting.value
    assert job.last_error is None

    # No model, so there is nothing that could be built by accident.
    assert await pipeline.model_of(parked) is None
    assert (await pipeline.deployment(parked)).blocked_reason is None


async def test_answering_resumes_the_same_pipeline(pipeline):
    deployment_id = await pipeline.deploy("unlinked")
    assert (
        await pipeline.deployment(deployment_id)
    ).status == DeploymentStatus.awaiting_answers.value

    await pipeline.answer(deployment_id, {"unlinked.plans": "read_only_shared"})

    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.awaiting_confirmation.value, (
        deployment.blocked_reason
    )
    assert not deployment.questions

    model = await pipeline.model_of(deployment_id)
    assert model.model_json["tables"]["plans"]["template"] == "read_only_shared"

    await pipeline.confirm(deployment_id)
    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.live.value, deployment.blocked_reason


async def test_an_answer_nobody_offered_is_refused(pipeline):
    """A typo must not quietly become an access model."""
    deployment_id = await pipeline.deploy("unlinked")
    await pipeline.answer(deployment_id, {"unlinked.plans": "everyone_obviously"})

    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.blocked.value
    assert "everyone_obviously" in deployment.blocked_reason
    assert await pipeline.model_of(deployment_id) is None
