"""A real git repository, deployed. Slice 1 of M7.5.

Everything else in `tests/pipeline` deploys a fixture tree, because planting a
bug on a branch is easiest when a branch is a directory. That leaves one thing
unproved, and it is the first thing a builder does: hand over a git repository
and get a URL back.

So this builds an actual repository with `git`, commits the fixture app into it
on two branches, and runs the whole pipeline over it. The clean branch has to
reach a browser with two people separated; the planted-bug branch has to be
blocked by name, out of a repository where the only difference between the two
is a commit.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from control_plane.models import DeploymentStatus, Protection
from tests.pipeline.conftest import REPO, settle, sign_in
from tests.pipeline.gitrepo import git, make_repo

ALICE = ("alice@example.com", "alice-password-1")
BOB = ("bob@example.com", "bob-password-2")


def _tree(*parts: str) -> dict[str, str]:
    """The fixture repository's files, read off disk, as a flat mapping."""
    files: dict[str, str] = {}
    for part in parts:
        root = REPO / part
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                files[path.relative_to(root).as_posix()] = path.read_text(
                    encoding="utf-8"
                )
    return files


@pytest.fixture(scope="session")
def origin(tmp_path_factory):
    """The fixture app, committed to a real repository on two branches."""
    return make_repo(
        tmp_path_factory.mktemp("origin") / "todo",
        {
            "main": _tree("base", "branches/clean"),
            "leaky": _tree("base", "branches/leaky_view"),
        },
    )


@pytest_asyncio.fixture(scope="session")
async def cloned(pipeline, origin):
    """Deploy `main` out of the git repository and confirm it."""
    deployment_id = await pipeline.deploy("main", repo=origin)

    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.awaiting_confirmation.value, (
        deployment.blocked_reason
    )
    await pipeline.confirm(deployment_id)
    return deployment_id


async def test_a_git_repository_goes_live(pipeline, cloned):
    deployment = await pipeline.deployment(cloned)
    assert deployment.status == DeploymentStatus.live.value, deployment.blocked_reason
    assert deployment.endpoint

    app = await pipeline.app_of(cloned)
    assert app.protection == Protection.protected.value
    assert app.stack["language"] == "python"


async def test_the_commit_that_was_built_is_recorded(pipeline, cloned, origin):
    """A branch name is not a build identity. The commit is."""
    deployment = await pipeline.deployment(cloned)

    assert deployment.ref == "main"
    assert deployment.commit_sha == git(origin, "rev-parse", "main")


async def test_two_people_are_still_separated(pipeline, cloned):
    """The same claim as `test_clean.py`, out of a cloned repository.

    The app's only query is `SELECT id, title, done FROM todos`, with no WHERE
    clause. Everything that separates these two lists is in the database.
    """
    deployment = await pipeline.deployment(cloned)
    app = await pipeline.app_of(cloned)

    alice = await pipeline.add_person(cloned, *ALICE)
    bob = await pipeline.add_person(cloned, *BOB)
    await pipeline.give_todo(app.app_id, alice.principal_key, "alice's errand")
    await pipeline.give_todo(app.app_id, bob.principal_key, "bob's errand")
    await settle()

    browser = await sign_in(deployment.endpoint, *ALICE)
    try:
        body = (await browser.get("/")).text
    finally:
        await browser.aclose()

    assert "alice's errand" in body
    assert "bob's errand" not in body


async def test_a_planted_bug_blocks_a_git_branch_too(pipeline, origin):
    """Same repository, one commit apart, and this one does not go live."""
    deployment_id = await pipeline.deploy("leaky", repo=origin)
    if (await pipeline.deployment(deployment_id)).status == (
        DeploymentStatus.awaiting_confirmation.value
    ):
        await pipeline.confirm(deployment_id)

    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.blocked.value
    assert "everyones_todos" in deployment.blocked_reason
    assert deployment.blocked_reason
    assert deployment.commit_sha == git(origin, "rev-parse", "leaky")


async def test_an_unknown_branch_is_blocked_with_a_sentence(pipeline, origin):
    deployment_id = await pipeline.deploy("no-such-branch", repo=origin)

    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.blocked.value
    assert "no-such-branch" in deployment.blocked_reason
    # Nothing was built, so there is nothing to have a commit.
    assert deployment.commit_sha is None
