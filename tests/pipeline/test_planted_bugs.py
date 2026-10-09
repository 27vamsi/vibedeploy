"""The other half of M7: every planted bug blocks, with a sentence.

Protected for good. These are the tests the product is: if one of them starts
passing by being deleted, the pipeline no longer proves anything. A blocked
deploy is a working deploy pipeline, so the assertion is never "it crashed" —
it is "it stopped, on purpose, and said why in words a builder can act on".

`membership` is the odd one out and deliberately so. Its bug is in the *shape*
of the schema, not in anything a policy could be attacked through, so it never
reaches the attack suite: derivation refuses, and there is no answer that would
make it deployable.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from control_plane.models import DeploymentStatus, JobStatus

# The branch, and something the reason has to name. The fragment is matched
# loosely on purpose: it pins *which* bug was caught, not the wording, which
# kernel/messages.py owns.
PLANTED = {
    "permissive_policy": "todos",
    "definer_function": "all_todos",
    "leaky_view": "everyones_todos",
    "truncate_grant": "todos",
}


def _readable(reason: str | None) -> str:
    assert reason, "a blocked deployment with no reason leaves a builder stuck"
    assert "Traceback" not in reason
    assert "asyncpg" not in reason
    return reason


@pytest_asyncio.fixture(scope="session")
async def blocked(pipeline):
    """Deploy and confirm every planted-bug branch once. Each is slow."""
    done = {}
    for branch in PLANTED:
        deployment_id = await pipeline.deploy(branch)
        status = (await pipeline.deployment(deployment_id)).status
        if status == DeploymentStatus.awaiting_confirmation.value:
            await pipeline.confirm(deployment_id)
        done[branch] = deployment_id
    return done


@pytest.mark.parametrize("branch", list(PLANTED))
async def test_planted_bug_blocks_the_deploy(pipeline, blocked, branch):
    deployment = await pipeline.deployment(blocked[branch])
    job = await pipeline.job_of(blocked[branch])
    assert deployment.status == DeploymentStatus.blocked.value, (
        f"{branch} was not blocked. {job.last_error}"
    )
    reason = _readable(deployment.blocked_reason)
    assert PLANTED[branch] in reason, f"{reason}\n\n{job.last_error}"


@pytest.mark.parametrize("branch", list(PLANTED))
async def test_nothing_went_live(pipeline, blocked, branch):
    deployment = await pipeline.deployment(blocked[branch])
    assert deployment.endpoint is None

    app = await pipeline.app_of(blocked[branch])
    assert app.live_deployment_id is None
    assert app.status != DeploymentStatus.live.value


@pytest.mark.parametrize("branch", list(PLANTED))
async def test_a_blocked_deploy_still_saves_its_report(pipeline, blocked, branch):
    """Section 3 rule 8. A blocked deploy is exactly when the report is read."""
    runs = await pipeline.runs(blocked[branch])
    assert set(runs) == {"runtime", "agent"}
    assert runs["runtime"].failed > 0
    # Section 13: the agent must see exactly what the runtime sees. A bug that
    # only one of them notices is a bug in the proof.
    assert runs["runtime"].failed == runs["agent"].failed
    assert runs["runtime"].total == runs["agent"].total


@pytest.mark.parametrize("branch", list(PLANTED))
async def test_blocking_is_a_finished_job_not_a_failed_one(pipeline, blocked, branch):
    job = await pipeline.job_of(blocked[branch])
    assert job.status == JobStatus.done.value, job.last_error


async def test_a_schema_we_cannot_reason_about_never_gets_confirmed(pipeline):
    """`membership`: a junction table whose primary key is its own foreign keys.

    There is no single owner column and no single path back to a person, so
    guessing one would be exactly the guess rule 5 forbids. It is refused in
    the plan phase, before anything is built.
    """
    deployment_id = await pipeline.deploy("membership")

    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.blocked.value, (
        "a refusal must not be offered for confirmation"
    )
    _readable(deployment.blocked_reason)
    assert await pipeline.model_of(deployment_id) is None
    assert await pipeline.runs(deployment_id) == {}
