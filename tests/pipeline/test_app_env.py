"""Settings a builder's app needs. Readme.md 18.1, "builder env vars".

The `.env` next to somebody's code never travels into a deployment, because an
app that builds its own engine out of its own `DATABASE_URL` reaches a database
with no identity on it and no policies on it. That left a real gap: an app with
a Stripe key had nowhere to put it.

So the platform carries them instead, and the interesting half of this file is
everything it refuses to carry:

  - `DATABASE_URL` and `VD_*` decide what database the app talks to and who it
    thinks the request is for. A builder who could set those could opt out of
    the product;
  - `PYTHON*` decides which code runs, including whether the shim's
    `sitecustomize` is found at all;
  - `LD_*` / `DYLD_*` load libraries into the process before any of it runs;
  - `DOCKER*` names the daemon our own `docker` client talks to, and is the one
    on this list that containerising *created* rather than found.

A deployment's own variables win over a builder's in every case, so even a
stored name that should never have been accepted cannot take effect.
"""

from __future__ import annotations

import shutil

import httpx
import pytest
import pytest_asyncio

from control_plane import appenv, secrets as secret_store, service
from control_plane.models import DeploymentStatus
from tests.pipeline.conftest import REPO, _operator, console, settle, sign_in
from worker.pipeline import clone
from worker.runtimes import serving

ALICE = ("alice@example.com", "alice-password-1")

PROBE = '''

@app.get("/__probe")
async def probe():
    return {
        "setting": os.environ.get("MY_SETTING"),
        "database_url": os.environ.get("DATABASE_URL"),
        "vd_database_url": os.environ.get("VD_DATABASE_URL"),
    }
'''


# ---------------------------------------------------------------------------
# What may be set, and what may not
# ---------------------------------------------------------------------------


def test_ordinary_settings_are_accepted():
    assert appenv.validate({"STRIPE_KEY": "sk_test_1", "FEATURE_X": "on"}) == {
        "STRIPE_KEY": "sk_test_1",
        "FEATURE_X": "on",
    }


@pytest.mark.parametrize(
    "name",
    [
        "DATABASE_URL",
        "VD_DATABASE_URL",
        "VD_IDENTITY_KEY",
        "VD_SHIM_DISABLED",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "DOCKER_CERT_PATH",
    ],
)
def test_names_that_would_opt_out_of_the_product_are_refused(name):
    with pytest.raises(appenv.InvalidEnv) as caught:
        appenv.validate({name: "anything"})

    # A refusal has to say which name and why, or a builder just tries again.
    assert name in str(caught.value)


def test_a_builder_cannot_choose_the_daemon_their_app_is_started_on():
    """The one reservation that containerising created rather than found.

    With bare processes `DOCKER_HOST` meant nothing at all: nothing in the
    start path was a `docker` client. Now the worker starts an app by running
    `docker`, and `docker` is a client that is told which daemon to talk to by
    this variable — so a builder who could set it would be choosing the daemon
    their own deployment is created on, with the worker's access to it. That
    turns a setting into a privilege escalation.

    Both halves, because the two guards exist for different moments: `validate`
    refuses the name on the way in, and `safe` drops it on the way out, so a
    name stored before this rule existed still cannot take effect.
    """
    with pytest.raises(appenv.InvalidEnv):
        appenv.validate({"DOCKER_HOST": "tcp://attacker.example:2375"})

    assert appenv.safe({"DOCKER_HOST": "tcp://attacker.example:2375"}) == {}


@pytest.mark.parametrize("name", ["", "1ABC", "a-b", "a b", "A=B", "A.B", "a\nb"])
def test_names_that_are_not_names_are_refused(name):
    with pytest.raises(appenv.InvalidEnv):
        appenv.validate({name: "x"})


def test_control_characters_in_a_value_are_refused():
    with pytest.raises(appenv.InvalidEnv):
        appenv.validate({"OK": "line\x00break"})


def test_too_many_or_too_large_is_refused():
    with pytest.raises(appenv.InvalidEnv):
        appenv.validate({f"K{i}": "v" for i in range(appenv.MAX_COUNT + 1)})
    with pytest.raises(appenv.InvalidEnv):
        appenv.validate({"BIG": "x" * (appenv.MAX_VALUE + 1)})


def test_the_paste_a_dotenv_shape_is_understood():
    """Builders will paste the file we refused to copy. Read it properly."""
    parsed = appenv.parse(
        "\n".join(
            [
                "# a comment",
                "",
                "STRIPE_KEY=sk_test_1",
                "  SPACED = value with spaces  ",
                'QUOTED="double"',
                "SINGLE='single'",
                "EQUALS=a=b=c",
                "export EXPORTED=yes",
            ]
        )
    )

    assert parsed == {
        "STRIPE_KEY": "sk_test_1",
        "SPACED": "value with spaces",
        "QUOTED": "double",
        "SINGLE": "single",
        "EQUALS": "a=b=c",
        "EXPORTED": "yes",
    }


def test_safe_drops_rather_than_raises():
    """Belt and braces at injection time: a stored name can never take effect."""
    assert appenv.safe({"OK": "1", "DATABASE_URL": "postgres://theirs", "VD_X": "2"}) == {
        "OK": "1"
    }


# ---------------------------------------------------------------------------
# On the page
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def browser_app(pipeline):
    """One app and one browser, driven only through forms."""
    async with console(pipeline) as client:
        response = await client.post(
            "/apps", data={"name": "settings", "repo": str(REPO)}
        )
        app_id = str(response.url).rsplit("/", 1)[-1]
        pipeline.app_ids.append(app_id)
        yield client, app_id


async def test_the_page_shows_the_names_and_never_the_values(pipeline, browser_app):
    browser, app_id = browser_app
    page = await browser.post(
        f"/apps/{app_id}/env",
        data={"env": "STRIPE_KEY=sk_live_secret\n# a comment\nFEATURE_X=on"},
    )

    assert "STRIPE_KEY" in page.text
    assert "FEATURE_X" in page.text
    assert "sk_live_secret" not in page.text
    assert service.app_env(pipeline.config, app_id) == {
        "STRIPE_KEY": "sk_live_secret",
        "FEATURE_X": "on",
    }


async def test_a_refused_name_comes_back_in_words(pipeline, browser_app):
    browser, app_id = browser_app
    before = service.app_env(pipeline.config, app_id)

    page = await browser.post(
        f"/apps/{app_id}/env", data={"env": "DATABASE_URL=postgres://theirs"}
    )

    assert "DATABASE_URL" in page.text
    # All or nothing: a refusal stores none of it, not the acceptable half.
    assert service.app_env(pipeline.config, app_id) == before


async def test_sending_the_form_again_replaces_the_whole_set(pipeline, browser_app):
    browser, app_id = browser_app
    await browser.post(f"/apps/{app_id}/env", data={"env": "ONLY_ONE=1"})
    assert service.app_env(pipeline.config, app_id) == {"ONLY_ONE": "1"}


# ---------------------------------------------------------------------------
# All the way into a running app
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def probed_folder(tmp_path_factory):
    """The fixture app, plus one route that reports its own environment."""
    root = tmp_path_factory.mktemp("withenv") / "my-app"
    shutil.copytree(REPO / "base", root)
    with open(root / "app.py", "a", encoding="utf-8") as handle:
        handle.write(PROBE)
    return root


@pytest_asyncio.fixture(scope="session")
async def live_with_env(pipeline, probed_folder):
    """Set the settings *before* the first deploy, then deploy."""
    async with pipeline.sessions() as session, session.begin():
        builder = await _operator(session)
        app = await service.create_app(
            session, builder=builder, name="with-env", repo=str(probed_folder)
        )
        app_id = app.app_id
        pipeline.app_ids.append(app_id)

    # Nothing has provisioned this app yet, so there is no runtime secret to
    # merge into. Setting them first has to work: it is the order a person
    # would use.
    service.set_app_env(pipeline.config, app_id, {"MY_SETTING": "hello"})

    async with pipeline.sessions() as session, session.begin():
        from control_plane.models import App
        from sqlalchemy import select

        app = (
            await session.execute(select(App).where(App.app_id == app_id))
        ).scalar_one()
        deployment = await service.start_deployment(
            session,
            app=app,
            ref=clone.WORKING_TREE,
            answers={
                "audience": "me",
                "size": "small",
                "visibility": "own_data",
                "sensitive": False,
            },
        )
        deployment_id = deployment.id
    await pipeline.drain()

    assert (await pipeline.deployment(deployment_id)).status == (
        DeploymentStatus.awaiting_confirmation.value
    )
    await pipeline.confirm(deployment_id)
    assert (await pipeline.deployment(deployment_id)).status == (
        DeploymentStatus.live.value
    ), (await pipeline.deployment(deployment_id)).blocked_reason
    return deployment_id


async def _probe(pipeline, deployment_id) -> dict:
    deployment = await pipeline.deployment(deployment_id)
    await settle()
    browser = await sign_in(deployment.endpoint, *ALICE)
    try:
        return (await browser.get("/__probe")).json()
    finally:
        await browser.aclose()


async def test_a_setting_reaches_the_running_app(pipeline, live_with_env):
    await pipeline.add_person(live_with_env, *ALICE)
    assert (await _probe(pipeline, live_with_env))["setting"] == "hello"


async def test_the_app_is_given_a_database_url_it_did_not_ask_for(
    pipeline, live_with_env
):
    """Most apps read `DATABASE_URL`. It is ours, and it is the runtime role."""
    probe = await _probe(pipeline, live_with_env)
    app = await pipeline.app_of(live_with_env)

    assert probe["database_url"]
    assert f"{app.app_id}_runtime" in probe["database_url"]
    assert probe["vd_database_url"].startswith("postgresql+asyncpg://")


async def test_a_builder_cannot_point_the_app_at_another_database(
    pipeline, live_with_env
):
    """The hole this whole file exists to close, written straight to the store.

    Going round the form is the point: the form's validation is a courtesy, and
    the thing that has to hold is the runtime refusing to apply a name it was
    somehow given.
    """
    app = await pipeline.app_of(live_with_env)
    store = secret_store.store_for(pipeline.config)
    name = secret_store.secret_name(app.app_id, secret_store.RUNTIME)
    secret = store.get(name)
    store.put(
        name,
        {
            **secret,
            "env": {
                "MY_SETTING": "hello",
                "DATABASE_URL": "postgresql://someone@localhost/their_own_db",
                "VD_SHIM_DISABLED": "1",
                "PYTHONPATH": "/tmp/mine",
            },
        },
    )

    # Start the app again, exactly as the deploy does, so it reads that secret.
    # Clearing the way first is now the caller's job: `serving.start`
    # deliberately leaves the previous version running, because stopping it is
    # a thing only a fence-protected cutover is allowed to do.
    #
    # `kill` and not `stop`, and the difference is the whole point of this
    # restart: `stop` is the end of a generation and takes its deps volume with
    # it, so the app that came back would have none of its own packages and
    # would fail on `import fastapi` before reaching the line under test. This
    # is the same version starting again, so its packages have to survive.
    await serving.kill(pipeline.config, app.app_id, app.desired_generation)
    running = await serving.start(
        pipeline.config,
        app_id=app.app_id,
        # The generation the deploy already used, so this is the same version of
        # the app starting again rather than a second one.
        generation=app.desired_generation,
        deployment_id=live_with_env,
        checkout=pipeline.config.state_root / "deployments" / str(live_with_env) / "src",
        entrypoint="app:app",
        logs=pipeline.config.state_root / "deployments" / str(live_with_env),
    )
    browser = await sign_in(running.endpoint, *ALICE)
    try:
        probe = (await browser.get("/__probe")).json()
    finally:
        await browser.aclose()

    assert "their_own_db" not in probe["database_url"]
    assert f"{app.app_id}_runtime" in probe["database_url"]
    # The one legitimate setting still arrived.
    assert probe["setting"] == "hello"
