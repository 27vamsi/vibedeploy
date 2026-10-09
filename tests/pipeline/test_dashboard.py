"""The same pause, driven the way a builder would drive it.

Every step here is a form post to the real control plane process. Nothing calls
`control_plane.service` directly, because the point of this file is that the
pages a person actually sees are wired to the pipeline: the question appears on
one, the answer resumes the job, and the rules appear in English before anything
is built.
"""

from __future__ import annotations

import httpx
import pytest_asyncio

from control_plane.models import DeploymentStatus
from tests.pipeline.conftest import REPO

BASE_ANSWERS = {
    "audience": "me",
    "size": "small",
    "visibility": "own_data",
    "sensitive": "no",
}


@pytest_asyncio.fixture(scope="session")
async def browser(pipeline):
    async with httpx.AsyncClient(
        base_url=pipeline.config.public_url, follow_redirects=True, timeout=30
    ) as client:
        yield client


async def _app_id(browser: httpx.AsyncClient, name: str) -> str:
    response = await browser.post("/apps", data={"name": name, "repo": str(REPO)})
    assert response.status_code == 200, response.text
    return str(response.url).rsplit("/", 1)[-1]


async def test_a_builder_answers_on_the_page_and_the_pipeline_moves(
    pipeline, browser
):
    app_id = await _app_id(browser, "dashboard todo")
    pipeline.app_ids.append(app_id)

    page = await browser.post(
        f"/apps/{app_id}/deploy", data={"commit_sha": "unlinked", **BASE_ANSWERS}
    )
    assert page.status_code == 200, page.text
    deployment_url = str(page.url)
    await pipeline.drain()

    # The pause, on the page. The question is asked in words, with the choices
    # spelled out, and there is a form to answer it with.
    page = await browser.get(deployment_url)
    assert "plans" in page.text
    assert "read_only_shared" in page.text
    assert 'action="' in page.text and "/answers" in page.text

    answered = await browser.post(
        f"{deployment_url}/answers", data={"unlinked.plans": "read_only_shared"}
    )
    assert answered.status_code == 200, answered.text
    await pipeline.drain()

    # Section 11 step 6: the rules, in English, before anything is built.
    page = await browser.get(deployment_url)
    assert "confirm" in page.text.lower()

    confirmed = await browser.post(f"{deployment_url}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    await pipeline.drain()

    page = await browser.get(deployment_url)
    assert DeploymentStatus.live.value in page.text, page.text[:2000]
    # Both attack runs are on the page, which is what makes "the agent cannot
    # exceed its person" something a builder can see rather than take on trust.
    assert "runtime" in page.text and "agent" in page.text


async def test_a_blocked_deploy_shows_its_reason(pipeline, browser):
    app_id = await _app_id(browser, "dashboard leaky")
    pipeline.app_ids.append(app_id)

    page = await browser.post(
        f"/apps/{app_id}/deploy", data={"commit_sha": "leaky_view", **BASE_ANSWERS}
    )
    deployment_url = str(page.url)
    await pipeline.drain()
    await browser.post(f"{deployment_url}/confirm")
    await pipeline.drain()

    page = await browser.get(deployment_url)
    assert DeploymentStatus.blocked.value in page.text
    assert "everyones_todos" in page.text


async def test_confirming_something_that_is_not_ready_is_refused(pipeline, browser):
    app_id = await _app_id(browser, "dashboard premature")
    pipeline.app_ids.append(app_id)

    page = await browser.post(
        f"/apps/{app_id}/deploy", data={"commit_sha": "unlinked", **BASE_ANSWERS}
    )
    deployment_url = str(page.url)
    await pipeline.drain()

    refused = await browser.post(f"{deployment_url}/confirm")
    assert refused.status_code == 400, refused.text
