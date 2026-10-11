"""Getting real code from a real repository. Readme.md section 15.

Slice 1 of M7.5. Until now a "repository" was a directory and a "branch" was an
overlay folder, which is fine for planting a bug on purpose and useless for
answering the only question a builder actually asks: can it deploy *my* repo.

These tests use real `git`, in a real temporary repository, because the things
that go wrong here are git's behaviours and not ours: a ref that does not exist,
a branch that moved, history and credentials leaking into the checkout.

Two properties are load bearing rather than cosmetic:

  - the resolved commit SHA comes back, so a deployment records what was built
    and not what somebody asked for. A branch name is not a build identity;
  - `.git` never reaches the checkout. The app's own process runs out of that
    directory, and git's config can hold a credential helper and a token.
"""

from __future__ import annotations

import re

import pytest

from worker.pipeline import clone
from tests.pipeline.gitrepo import git, make_repo

SHA = re.compile(r"^[0-9a-f]{40}$")


@pytest.fixture
def repo(tmp_path):
    """A repository with two branches whose files differ."""
    return make_repo(
        tmp_path / "origin",
        {
            "main": {"app.py": "main\n", "migrations/001.sql": "SELECT 1;\n"},
            "feature": {"app.py": "feature\n", "migrations/001.sql": "SELECT 1;\n"},
        },
    )


def test_clones_a_branch(repo, tmp_path):
    result = clone.checkout(repo, "main", tmp_path / "src")

    assert (result.path / "app.py").read_text() == "main\n"
    assert SHA.match(result.commit_sha or ""), result.commit_sha
    assert result.commit_sha == git(repo, "rev-parse", "main")


def test_branches_are_not_the_same_build(repo, tmp_path):
    main = clone.checkout(repo, "main", tmp_path / "a")
    feature = clone.checkout(repo, "feature", tmp_path / "b")

    assert (feature.path / "app.py").read_text() == "feature\n"
    assert main.commit_sha != feature.commit_sha


def test_a_commit_sha_is_a_valid_ref(repo, tmp_path):
    """`verify` re-checks out the exact commit `plan` resolved, not the branch."""
    sha = git(repo, "rev-parse", "feature")

    result = clone.checkout(repo, sha, tmp_path / "src")

    assert result.commit_sha == sha
    assert (result.path / "app.py").read_text() == "feature\n"


def test_unknown_ref_is_blocked_in_words_a_builder_can_act_on(repo, tmp_path):
    with pytest.raises(clone.CheckoutError) as caught:
        clone.checkout(repo, "nope", tmp_path / "src")

    message = str(caught.value)
    assert "nope" in message
    assert "Traceback" not in message
    assert not (tmp_path / "src").exists()


def test_missing_repository_is_blocked(tmp_path):
    with pytest.raises(clone.CheckoutError):
        clone.checkout(tmp_path / "not-a-repo", "main", tmp_path / "src")


def test_git_metadata_never_reaches_the_checkout(repo, tmp_path):
    """The app runs out of this directory, and `.git/config` can hold a token."""
    result = clone.checkout(repo, "main", tmp_path / "src")

    assert not (result.path / ".git").exists()


def test_untracked_files_are_not_carried_over(repo, tmp_path):
    """Proof it is a clone and not a copy of somebody's working directory."""
    (repo / "secret.env").write_text("TOKEN=hunter2\n", encoding="utf-8")
    (repo / "__pycache__").mkdir()

    result = clone.checkout(repo, "main", tmp_path / "src")

    assert not (result.path / "secret.env").exists()
    assert not (result.path / "__pycache__").exists()


def test_fixture_overlays_still_work(tmp_path):
    """The planted-bug repos are not git, and must keep working untouched."""
    fixture = tmp_path / "fixture"
    (fixture / "base").mkdir(parents=True)
    (fixture / "base" / "shared.txt").write_text("shared\n", encoding="utf-8")
    (fixture / "branches" / "clean").mkdir(parents=True)
    (fixture / "branches" / "clean" / "app.py").write_text("clean\n", encoding="utf-8")

    result = clone.checkout(fixture, "clean", tmp_path / "src")

    assert (result.path / "shared.txt").read_text() == "shared\n"
    assert (result.path / "app.py").read_text() == "clean\n"
    # No commit to record: a directory has no identity of its own.
    assert result.commit_sha is None


def test_unknown_fixture_branch_lists_the_real_ones(tmp_path):
    fixture = tmp_path / "fixture"
    (fixture / "base").mkdir(parents=True)
    (fixture / "branches" / "clean").mkdir(parents=True)
    (fixture / "branches" / "permissive").mkdir(parents=True)

    with pytest.raises(clone.CheckoutError) as caught:
        clone.checkout(fixture, "nope", tmp_path / "src")

    assert "`clean`" in str(caught.value)
    assert "`permissive`" in str(caught.value)
