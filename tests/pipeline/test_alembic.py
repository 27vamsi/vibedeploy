"""Deploying an app whose migrations are Alembic. Readme.md 8 M3, 15, 657.

Almost nothing an AI writes is a folder of `.sql` files. It writes SQLAlchemy
models and `alembic revision --autogenerate`, so until this worked "a real repo
can be deployed" was not true, and both refusals said so in as many words.

An Alembic project is not SQL we can read and run. It is the app's own Python:
an `env.py` that imports the app and opens its own connection. So the migration
is run by *running it*, which brings three new problems, and this file is about
each of them:

  - the app's own dependencies have to be importable, which means installing
    packages chosen by the builder. That happens in the container, never here;
  - the database has to be the one we chose, not the one committed in
    `alembic.ini`;
  - Alembic leaves `alembic_version` behind in the schema. It is not the app's
    data, so it must not reach the access model or the schema hash — and it is
    still in a schema the app can reach, so it still has to be shut.

The last one is the subtle one. `attack.structural` fails any table in the
schema that the access model does not cover, which is exactly right for app
tables and would block every Alembic app on earth. Dropping it from the graph
to dodge that check would leave a table nobody had decided anything about, so
it is dropped from the graph *and* locked, and both halves are asserted here.
"""

from __future__ import annotations

import json

import asyncpg
import pytest
import pytest_asyncio

from buildjob import deps, migrate_alembic
from buildjob.introspect import BOOKKEEPING
from control_plane.config import REPO_ROOT
from control_plane.models import DeploymentStatus
from worker.pipeline.detect import detect
from tests.pipeline.conftest import sign_in

REPO = REPO_ROOT / "fixtures" / "repos" / "todo_alembic"


# ---------------------------------------------------------------------------
# What the pipeline decides before anything runs
# ---------------------------------------------------------------------------


def test_the_fixture_is_detected_as_alembic():
    stack = detect(REPO / "base")

    assert (stack.migrations_kind, stack.migrations_path) == ("alembic", "alembic")
    assert stack.protection == "protected"


def test_an_alembic_project_is_no_longer_refused():
    """The promise the two refusals used to make has been kept.

    Both said Alembic "arrives with the containerised build". The containerised
    build arrived, so neither sentence may still be reachable for an Alembic
    repo — in any of the three places a deploy could have said no.
    """
    from buildjob import migrate_task, run
    from worker.pipeline import deploy

    for sentence in (
        run.UNSUPPORTED_MIGRATIONS,
        migrate_task.UNSUPPORTED,
        deploy.UNSUPPORTED_MIGRATIONS,
    ):
        assert "arrives with" not in sentence

    assert "alembic" in deploy.SUPPORTED_MIGRATIONS


# ---------------------------------------------------------------------------
# The database the migrations are pointed at is ours, not the repo's
# ---------------------------------------------------------------------------


def test_the_url_committed_in_alembic_ini_is_never_used():
    """`alembic.ini` names a database. It is not the app's choice to make."""
    committed = (REPO / "base" / "alembic.ini").read_text(encoding="utf-8")
    assert "postgresql://localhost/todo_dev" in committed

    config = migrate_alembic.configure(
        dsn="postgresql://vd:pw@db:5432/scratch",
        repo_dir=REPO / "base",
        migrations_path="alembic",
    )

    assert config.get_main_option("sqlalchemy.url") == (
        "postgresql://vd:pw@db:5432/scratch"
    )


def test_a_password_with_a_percent_in_it_survives_the_ini_parser():
    """`%` is ConfigParser's interpolation character, so it has to be doubled."""
    config = migrate_alembic.configure(
        dsn="postgresql://vd:p%40ss%25word@db:5432/scratch",
        repo_dir=REPO / "base",
        migrations_path="alembic",
    )

    assert config.get_main_option("sqlalchemy.url") == (
        "postgresql://vd:p%40ss%25word@db:5432/scratch"
    )


def test_both_places_an_env_py_might_look_say_the_same_thing(monkeypatch):
    """Half of real `env.py` files read the environment and half read the ini."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    seen = {}

    with migrate_alembic.pointed_at("postgresql://vd:pw@db:5432/scratch"):
        seen["env"] = __import__("os").environ["DATABASE_URL"]

    assert seen["env"] == "postgresql://vd:pw@db:5432/scratch"
    # And put back afterwards, because the build job does other things after
    # migrating and none of them should see a migrator DSN.
    assert "DATABASE_URL" not in __import__("os").environ


def test_a_repo_with_no_alembic_ini_is_refused_in_words():
    with pytest.raises(migrate_alembic.AlembicError) as refused:
        migrate_alembic.configure(
            dsn="postgresql://x/y", repo_dir=REPO, migrations_path="alembic"
        )

    assert "alembic.ini" in str(refused.value)


# ---------------------------------------------------------------------------
# The app's own dependencies
# ---------------------------------------------------------------------------


def test_the_requirements_file_is_found_and_hashed(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")

    found = deps.requirements(tmp_path)

    assert found == tmp_path / "requirements.txt"
    assert deps.fingerprint(found) == deps.fingerprint(found)


def test_an_app_with_no_requirements_installs_nothing(tmp_path):
    assert deps.requirements(tmp_path) is None


def test_installing_twice_does_the_work_once(tmp_path):
    """Three containers per deploy read the same target. Only one may pay."""
    source = tmp_path / "repo"
    source.mkdir()
    requirements = source / "requirements.txt"
    requirements.write_text("# nothing at all\n", encoding="utf-8")
    target = tmp_path / "deps"

    calls = []
    first = deps.install(source, target, _run=lambda argv: calls.append(argv))
    second = deps.install(source, target, _run=lambda argv: calls.append(argv))

    assert first == target and second == target
    assert len(calls) == 1
    assert "--target" in calls[0] and str(target) in calls[0]


def test_a_changed_requirements_file_is_installed_again(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    requirements = source / "requirements.txt"
    requirements.write_text("fastapi\n", encoding="utf-8")
    target = tmp_path / "deps"

    calls = []
    deps.install(source, target, _run=lambda argv: calls.append(argv))
    requirements.write_text("fastapi\nuvicorn\n", encoding="utf-8")
    deps.install(source, target, _run=lambda argv: calls.append(argv))

    assert len(calls) == 2


def test_a_failed_install_blocks_rather_than_guessing(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "requirements.txt").write_text("not-a-real-package\n", encoding="utf-8")

    def explode(argv):
        raise deps.DependencyError("pip said no")

    with pytest.raises(deps.DependencyError):
        deps.install(source, tmp_path / "deps", _run=explode)


# ---------------------------------------------------------------------------
# `alembic_version` is not the app's data, and is not left open either
# ---------------------------------------------------------------------------


def test_alembic_version_is_known_to_be_bookkeeping():
    assert "alembic_version" in BOOKKEEPING


# ---------------------------------------------------------------------------
# One real deployment, all the way to live
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def live(pipeline):
    """A whole Alembic app, deployed and confirmed, in containers."""
    deployment_id = await pipeline.deploy("clean", name="alembic", repo=REPO)
    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.awaiting_confirmation.value, (
        deployment.blocked_reason
    )
    await pipeline.confirm(deployment_id)
    deployment = await pipeline.deployment(deployment_id)
    assert deployment.status == DeploymentStatus.live.value, (
        deployment.blocked_reason
    )
    return deployment_id


async def test_the_model_derived_from_alembic_is_the_model_derived_from_sql(
    pipeline, live
):
    """Same schema, same answers, same rules. The tool used is not a variable."""
    model = (await pipeline.model_of(live)).model_json

    assert set(model["tables"]) == {"users", "todos"}
    assert model["tables"]["todos"]["template"] == "owner_column"
    assert model["tables"]["todos"]["column"] == "owner_id"
    assert model["tables"]["users"]["template"] == "owner_column"


async def test_the_version_table_is_not_in_the_access_model(pipeline, live):
    """It holds a revision id. There is no answer to "who may see it"."""
    model = (await pipeline.model_of(live)).model_json
    graph = (await pipeline.runs(live))["runtime"].report_json

    assert "alembic_version" not in model["tables"]
    assert "alembic_version" not in json.dumps(graph)


async def test_the_version_table_is_still_shut(pipeline, live):
    """Out of the graph is not the same as out of reach.

    Nothing grants it, and RLS plus FORCE is on it anyway, so there are two
    independent reasons the app and the agent see nothing in it.
    """
    app = await pipeline.app_of(live)
    admin = await asyncpg.connect(pipeline.config.app_admin_dsn)
    try:
        enabled, forced = await admin.fetchrow(
            "SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = $1 AND c.relname = 'alembic_version'",
            app.app_id,
        )
        assert enabled and forced

        for role in (f"{app.app_id}_runtime", f"{app.app_id}_agent"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert not await admin.fetchval(
                    "SELECT has_table_privilege($1, $2, $3)",
                    role,
                    f'"{app.app_id}".alembic_version',
                    privilege,
                ), f"{role} holds {privilege} on alembic_version"
    finally:
        await admin.close()


async def test_the_migrations_really_ran_as_alembic(pipeline, live):
    """The revision is recorded, so this was Alembic and not a lucky `.sql`."""
    app = await pipeline.app_of(live)
    admin = await asyncpg.connect(pipeline.config.app_admin_dsn)
    try:
        assert (
            await admin.fetchval(
                f'SELECT version_num FROM "{app.app_id}".alembic_version'
            )
            == "0001"
        )
    finally:
        await admin.close()


async def test_two_people_are_separated_in_an_alembic_app(pipeline, live):
    """The point of the whole thing, on an app nobody wrote any auth for."""
    app = await pipeline.app_of(live)
    alice = await pipeline.add_person(live, "alice@example.com", "correct-horse-1")
    bob = await pipeline.add_person(live, "bob@example.com", "correct-horse-2")
    await pipeline.give_todo(app.app_id, alice.principal_key, "alice buys milk")
    await pipeline.give_todo(app.app_id, bob.principal_key, "bob buys bread")

    endpoint = (await pipeline.deployment(live)).endpoint
    browser = await sign_in(endpoint, "alice@example.com", "correct-horse-1")
    try:
        titles = [
            row["title"] for row in (await browser.get("/todos")).json()["todos"]
        ]
    finally:
        await browser.aclose()

    assert titles == ["alice buys milk"]
    assert bob.principal_key != alice.principal_key
