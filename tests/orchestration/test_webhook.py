"""A push deploys, once. Readme.md 15 ("push webhook"), 3 rule 5.

Everything that reaches this endpoint is a stranger's HTTP request, so there are
only two interesting questions and this file is about each of them:

  - **is it really GitHub?** The body is signed, and the signature is checked
    over the exact bytes that arrived, with a constant-time compare. An unset
    secret admits nobody rather than everybody, because the failure mode of the
    other choice is a public endpoint that queues deployments for anyone who
    finds it.
  - **has this delivery already been handled?** GitHub retries. A retry is the
    normal case, not an error, and a second deployment of the same push would
    be two builds, two containers and two cutovers for one commit. The guard is
    a unique index and an INSERT: read-then-write would pass every sequential
    test in this file and lose a race, so one test here runs two deliveries at
    the same time and insists on one deployment.

What a push may *not* do is answer section 10's questions on a builder's behalf.
An app that has not said who may see what has nothing to enforce, so a push to
it is recorded and refused rather than deployed with a guess.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from dataclasses import replace

import httpx
import pytest
import pytest_asyncio

from control_plane import webhooks
from control_plane.app import create_app
from control_plane.models import DeploymentStatus

SECRET = "a secret only github and we have"
REPO = "octocat/todo"


# ---------------------------------------------------------------------------
# The harness: a real control plane process, reached over ASGI
# ---------------------------------------------------------------------------


def sign(secret: str, body: bytes) -> str:
    """Computed here independently of the code under test, on purpose."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def push_body(
    *,
    repo: str = REPO,
    ref: str = "refs/heads/main",
    after: str = "1" * 40,
    deleted: bool = False,
) -> bytes:
    """The subset of GitHub's push payload anything here actually reads."""
    return json.dumps(
        {
            "ref": ref,
            "after": after,
            "deleted": deleted,
            "repository": {
                "full_name": repo,
                "clone_url": f"https://github.com/{repo}.git",
                "ssh_url": f"git@github.com:{repo}.git",
            },
        }
    ).encode()


async def deliver(
    client: httpx.AsyncClient,
    body: bytes,
    *,
    secret: str = SECRET,
    delivery: str | None = None,
    event: str = "push",
    signature: str | None = None,
) -> httpx.Response:
    headers = {
        webhooks.EVENT_HEADER: event,
        webhooks.DELIVERY_HEADER: delivery or str(uuid.uuid4()),
        "content-type": "application/json",
    }
    presented = sign(secret, body) if signature is None else signature
    if presented:
        headers[webhooks.SIGNATURE_HEADER] = presented
    return await client.post("/hooks/github", content=body, headers=headers)


async def _client(config):
    app = create_app(config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://control.test"
        ) as client:
            yield client


@pytest_asyncio.fixture
async def hooks(_queue_engine):
    config, _ = _queue_engine
    async for client in _client(replace(config, github_webhook_secret=SECRET)):
        yield client


@pytest_asyncio.fixture
async def hooks_with_no_secret(_queue_engine):
    config, _ = _queue_engine
    async for client in _client(replace(config, github_webhook_secret="")):
        yield client


# ---------------------------------------------------------------------------
# The happy path, and the retry that follows it
# ---------------------------------------------------------------------------


async def test_a_signed_push_queues_one_deployment_of_that_branch(queue, hooks):
    app = await queue.make_app(repo=REPO)

    answer = await deliver(hooks, push_body())

    assert answer.status_code == 200, answer.text
    deployments = await queue.deployments_of(app.id)
    assert len(deployments) == 1
    assert deployments[0].ref == "main"
    assert deployments[0].status == DeploymentStatus.queued.value
    # And something to run it, because a deployment nobody picks up is a page
    # that says "queued" for ever.
    assert len(await queue.jobs_of(app.id)) == 1


async def test_the_same_delivery_twice_creates_no_second_deployment(queue, hooks):
    app = await queue.make_app(repo=REPO)
    body = push_body()
    delivery = "delivery-0001"

    first = await deliver(hooks, body, delivery=delivery)
    second = await deliver(hooks, body, delivery=delivery)

    assert (first.status_code, second.status_code) == (200, 200)
    assert len(await queue.deployments_of(app.id)) == 1
    assert len(await queue.jobs_of(app.id)) == 1
    # One delivery, one record of it. The second request is told so rather than
    # being quietly dropped, because "already handled" is a different thing
    # from "ignored" and GitHub's delivery log is where a builder looks.
    assert [row.delivery_id for row in await queue.deliveries()] == [delivery]
    assert second.json()["outcome"] == webhooks.ALREADY_HANDLED


async def test_two_simultaneous_deliveries_create_one_deployment(queue, hooks):
    """The test a read-then-write check cannot pass.

    GitHub's retry can arrive while the first attempt is still in flight. If
    idempotency were "look for an existing row, then insert one", both requests
    would look, both would find nothing, and both would deploy.
    """
    app = await queue.make_app(repo=REPO)
    body = push_body()
    delivery = "delivery-at-the-same-time"

    answers = await asyncio.gather(
        deliver(hooks, body, delivery=delivery),
        deliver(hooks, body, delivery=delivery),
    )

    assert [a.status_code for a in answers] == [200, 200]
    assert len(await queue.deployments_of(app.id)) == 1
    outcomes = sorted(a.json()["outcome"] for a in answers)
    assert outcomes == sorted([webhooks.DEPLOYED, webhooks.ALREADY_HANDLED])


async def test_a_second_push_of_a_second_commit_does_deploy_again(queue, hooks):
    """Idempotency is per delivery, not per repository."""
    app = await queue.make_app(repo=REPO)

    await deliver(hooks, push_body(after="1" * 40), delivery="one")
    await deliver(hooks, push_body(after="2" * 40), delivery="two")

    assert len(await queue.deployments_of(app.id)) == 2


# ---------------------------------------------------------------------------
# Is it really GitHub
# ---------------------------------------------------------------------------


async def test_a_forged_signature_deploys_nothing(queue, hooks):
    app = await queue.make_app(repo=REPO)

    answer = await deliver(hooks, push_body(), secret="not the secret")

    assert answer.status_code == 401
    assert await queue.deployments_of(app.id) == []
    assert await queue.jobs_of(app.id) == []
    # Not even a record: a request we cannot attribute to GitHub is not a
    # delivery, and writing one would let anybody fill this table.
    assert await queue.deliveries() == []


async def test_a_missing_signature_deploys_nothing(queue, hooks):
    app = await queue.make_app(repo=REPO)

    answer = await deliver(hooks, push_body(), signature="")

    assert answer.status_code == 401
    assert await queue.deployments_of(app.id) == []


async def test_an_unconfigured_secret_admits_nobody(queue, hooks_with_no_secret):
    """Fail closed. A public endpoint that queues builds for strangers is the
    alternative, and it would look exactly like working."""
    app = await queue.make_app(repo=REPO)
    body = push_body()

    signed_with_nothing = await deliver(hooks_with_no_secret, body, secret="")
    signed_with_ours = await deliver(hooks_with_no_secret, body, secret=SECRET)

    assert signed_with_nothing.status_code == 401
    assert signed_with_ours.status_code == 401
    assert await queue.deployments_of(app.id) == []


async def test_the_signature_has_to_cover_the_body_that_arrived(queue, hooks):
    """A signature lifted from a real delivery proves nothing about this one."""
    app = await queue.make_app(repo=REPO)
    real = push_body(after="1" * 40)
    tampered = push_body(after="9" * 40)

    answer = await deliver(hooks, tampered, signature=sign(SECRET, real))

    assert answer.status_code == 401
    assert await queue.deployments_of(app.id) == []


async def test_a_body_that_is_not_json_is_refused_without_deploying(queue, hooks):
    app = await queue.make_app(repo=REPO)

    answer = await deliver(hooks, b"{not json at all")

    assert answer.status_code == 400
    assert await queue.deployments_of(app.id) == []


# ---------------------------------------------------------------------------
# Which pushes are ours to act on
# ---------------------------------------------------------------------------


async def test_a_push_to_another_branch_is_recorded_and_ignored(queue, hooks):
    """A branch nobody deploys is not an error, and not a deployment either."""
    app = await queue.make_app(repo=REPO, tracked_ref="main")

    answer = await deliver(hooks, push_body(ref="refs/heads/spike"))

    assert answer.status_code == 200
    assert answer.json()["outcome"] == webhooks.NOT_TRACKED
    assert await queue.deployments_of(app.id) == []
    # Recorded, because "why did my push not deploy?" needs an answer.
    assert len(await queue.deliveries()) == 1


async def test_a_deleted_branch_does_not_deploy(queue, hooks):
    app = await queue.make_app(repo=REPO)

    answer = await deliver(hooks, push_body(deleted=True))

    assert answer.status_code == 200
    assert await queue.deployments_of(app.id) == []


async def test_a_tag_push_does_not_deploy(queue, hooks):
    app = await queue.make_app(repo=REPO)

    answer = await deliver(hooks, push_body(ref="refs/tags/v1.0.0"))

    assert answer.status_code == 200
    assert await queue.deployments_of(app.id) == []


async def test_an_event_that_is_not_a_push_is_accepted_and_ignored(queue, hooks):
    """GitHub retries anything that is not a 2xx, so refusing a `ping` with an
    error would mean being retried for ever over something we chose to skip."""
    app = await queue.make_app(repo=REPO)

    answer = await deliver(hooks, push_body(), event="ping")

    assert answer.status_code == 200
    assert answer.json()["outcome"] == webhooks.NOT_A_PUSH
    assert await queue.deployments_of(app.id) == []


async def test_a_push_for_a_repository_nobody_deploys_is_ignored(queue, hooks):
    await queue.make_app(repo=REPO)

    answer = await deliver(hooks, push_body(repo="somebody/else"))

    assert answer.status_code == 200
    assert answer.json()["outcome"] == webhooks.NO_SUCH_APP
    assert len(await queue.deliveries()) == 1


async def test_a_push_cannot_answer_the_questions_on_a_builders_behalf(queue, hooks):
    """Section 3 rule 5. No answers, no deployment — not even from a push.

    This is the one refusal here that is not about security of the endpoint. An
    app that has never said who may see what has nothing for the attack suite
    to prove, so a push to it stops and says so.
    """
    app = await queue.make_app(repo=REPO, answers=None)

    answer = await deliver(hooks, push_body())

    assert answer.status_code == 200
    assert await queue.deployments_of(app.id) == []
    assert answer.json()["outcome"] == webhooks.REFUSED
    [recorded] = await queue.deliveries()
    assert "questions" in (recorded.detail or "")


async def test_two_apps_built_from_one_repository_both_deploy(queue, hooks):
    """One delivery is one record, whatever it turned out to mean."""
    first = await queue.make_app(repo=REPO)
    second = await queue.make_app(repo=f"https://github.com/{REPO}.git")

    answer = await deliver(hooks, push_body(), delivery="shared")

    assert answer.status_code == 200
    assert len(await queue.deployments_of(first.id)) == 1
    assert len(await queue.deployments_of(second.id)) == 1
    assert len(await queue.deliveries()) == 1


# ---------------------------------------------------------------------------
# Matching a repository, however it was written down
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    [
        "octocat/todo",
        "OctoCat/Todo",
        "https://github.com/octocat/todo",
        "https://github.com/octocat/todo.git",
        "https://github.com/octocat/todo/",
        "git@github.com:octocat/todo.git",
        "ssh://git@github.com/octocat/todo.git",
    ],
)
def test_the_same_repository_written_seven_ways_is_one_repository(written):
    """A builder pastes whatever GitHub's green button gave them."""
    assert webhooks.repo_key(written) == "octocat/todo"


def test_a_local_folder_is_not_mistaken_for_a_github_repository():
    """Most of this project's own fixtures are paths, and no push is ever
    about one. They must not collide with each other either."""
    assert webhooks.repo_key("/srv/fixtures/repos/todo") != "octocat/todo"
    assert webhooks.repo_key("/srv/fixtures/repos/todo") != webhooks.repo_key(
        "/srv/fixtures/repos/other"
    )
