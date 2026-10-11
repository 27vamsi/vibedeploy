"""Deploying a folder, with no git and no GitHub. Slice 1 of M7.5.

A builder who has an app on their laptop and no repository has to be able to
deploy it. That is the honest starting state for most of them, and "commit it
first" is not an answer.

Two things make this more than a `copytree`:

  - a folder that *is* a git repository is the normal case, and its working tree
    is not its `HEAD`. Deploying the last commit when somebody asked for the
    folder would run code they are not looking at. `working-tree` means exactly
    what is on disk, uncommitted changes and all;
  - a `.env` on a laptop holds a `DATABASE_URL`. Copying it into a deployment
    hands the app a second way to reach a database, and an app that builds its
    own engine out of its own `.env` is outside everything this product
    enforces. It does not travel.
"""

from __future__ import annotations

import shutil

import pytest
import pytest_asyncio

from control_plane.models import DeploymentStatus, Protection
from tests.pipeline.conftest import REPO, settle, sign_in
from tests.pipeline.gitrepo import git, make_repo
from worker.pipeline import clone

ALICE = ("alice@example.com", "alice-password-1")
BOB = ("bob@example.com", "bob-password-2")


@pytest.fixture
def folder(tmp_path):
    """Somebody's app directory. No git anywhere in it."""
    root = tmp_path / "my-app"
    (root / "migrations").mkdir(parents=True)
    (root / "app.py").write_text("app = 1\n", encoding="utf-8")
    (root / "requirements.txt").write_text("sqlalchemy\n", encoding="utf-8")
    (root / "migrations" / "001.sql").write_text("SELECT 1;\n", encoding="utf-8")
    return root


def test_a_plain_folder_is_deployable(folder, tmp_path):
    result = clone.checkout(folder, "main", tmp_path / "src")

    assert (result.path / "app.py").read_text() == "app = 1\n"
    assert (result.path / "migrations" / "001.sql").exists()


def test_a_plain_folder_records_what_was_built(folder, tmp_path):
    """There is no commit, and the ref somebody typed was never consulted."""
    result = clone.checkout(folder, "main", tmp_path / "src")

    assert result.commit_sha is None
    assert result.ref == clone.WORKING_TREE


def test_working_tree_deploys_uncommitted_changes(tmp_path):
    """The point of asking for a folder rather than a branch."""
    repo = make_repo(tmp_path / "origin", {"main": {"app.py": "committed\n"}})
    (repo / "app.py").write_text("not committed yet\n", encoding="utf-8")

    result = clone.checkout(repo, clone.WORKING_TREE, tmp_path / "src")

    assert (result.path / "app.py").read_text() == "not committed yet\n"
    assert result.ref == clone.WORKING_TREE
    assert result.commit_sha is None


def test_a_branch_of_the_same_folder_still_builds_the_commit(tmp_path):
    """Asking for a ref means the ref, even when the working tree is dirty."""
    repo = make_repo(tmp_path / "origin", {"main": {"app.py": "committed\n"}})
    (repo / "app.py").write_text("not committed yet\n", encoding="utf-8")

    result = clone.checkout(repo, "main", tmp_path / "src")

    assert (result.path / "app.py").read_text() == "committed\n"
    assert result.commit_sha == git(repo, "rev-parse", "main")


def test_secrets_and_junk_do_not_travel(folder, tmp_path):
    (folder / ".env").write_text("DATABASE_URL=postgres://real/prod\n", encoding="utf-8")
    (folder / ".env.local").write_text("TOKEN=hunter2\n", encoding="utf-8")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "app.pyc").write_bytes(b"\x00")
    (folder / ".venv" / "Scripts").mkdir(parents=True)
    (folder / "node_modules" / "left-pad").mkdir(parents=True)
    (folder / ".git").mkdir()
    (folder / ".git" / "config").write_text("[credential]\n", encoding="utf-8")

    result = clone.checkout(folder, clone.WORKING_TREE, tmp_path / "src")

    assert not (result.path / ".env").exists()
    assert not (result.path / ".env.local").exists()
    assert not (result.path / "__pycache__").exists()
    assert not (result.path / ".venv").exists()
    assert not (result.path / "node_modules").exists()
    assert not (result.path / ".git").exists()
    # The app itself is still there.
    assert (result.path / "app.py").exists()


def test_a_missing_folder_is_blocked(tmp_path):
    with pytest.raises(clone.CheckoutError):
        clone.checkout(tmp_path / "nope", clone.WORKING_TREE, tmp_path / "src")


def test_the_checkout_may_not_live_inside_the_folder(folder):
    """Copying a directory into itself never ends."""
    with pytest.raises(clone.CheckoutError):
        clone.checkout(folder, clone.WORKING_TREE, folder / "build" / "src")


def test_a_fixture_tree_is_not_mistaken_for_a_folder(tmp_path):
    """`base/` + `branches/` still means overlays, not a snapshot."""
    fixture = tmp_path / "fixture"
    (fixture / "base").mkdir(parents=True)
    (fixture / "base" / "shared.txt").write_text("shared\n", encoding="utf-8")
    (fixture / "branches" / "clean").mkdir(parents=True)

    result = clone.checkout(fixture, "clean", tmp_path / "src")

    assert (result.path / "shared.txt").exists()
    assert not (result.path / "base").exists()
    assert result.ref == "clean"


# ---------------------------------------------------------------------------
# The whole pipeline, over a folder. No git, no GitHub, no network.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def app_folder(tmp_path_factory):
    """The fixture app as a builder would have it: one directory, no git."""
    root = tmp_path_factory.mktemp("laptop") / "my-todo-app"
    shutil.copytree(REPO / "base", root)
    # The thing a laptop always has and a deployment must never get.
    (root / ".env").write_text(
        "DATABASE_URL=postgresql://someone@localhost/their_own_db\n", encoding="utf-8"
    )
    return root


@pytest_asyncio.fixture(scope="session")
async def live_folder(pipeline, app_folder):
    deployment_id = await pipeline.deploy(clone.WORKING_TREE, repo=app_folder)

    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.awaiting_confirmation.value, (
        deployment.blocked_reason
    )
    await pipeline.confirm(deployment_id)
    return deployment_id


async def test_a_folder_goes_live(pipeline, live_folder):
    deployment = await pipeline.deployment(live_folder)
    assert deployment.status == DeploymentStatus.live.value, deployment.blocked_reason
    assert deployment.endpoint

    app = await pipeline.app_of(live_folder)
    assert app.protection == Protection.protected.value


async def test_a_folder_has_no_commit_and_says_so(pipeline, live_folder):
    deployment = await pipeline.deployment(live_folder)
    assert deployment.ref == clone.WORKING_TREE
    assert deployment.commit_sha is None


async def test_a_folder_is_still_proved(pipeline, live_folder):
    """The attack suite ran as both roles. Deploying a folder proves no less."""
    runs = await pipeline.runs(live_folder)
    assert set(runs) == {"runtime", "agent"}
    for run in runs.values():
        assert run.failed == 0
        assert run.passed > 0


async def test_two_people_are_separated_in_a_deployed_folder(pipeline, live_folder):
    deployment = await pipeline.deployment(live_folder)
    app = await pipeline.app_of(live_folder)

    alice = await pipeline.add_person(live_folder, *ALICE)
    bob = await pipeline.add_person(live_folder, *BOB)
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
