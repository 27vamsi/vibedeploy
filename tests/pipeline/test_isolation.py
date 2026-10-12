"""Where an app's own code is allowed to run. Readme.md 3 rule 11, 18.2, 8 M6.

The worker holds the customer database's admin credentials. An app's migrations
are code we have never read, so two things have to be true of them and neither
was:

  - they run in a container, not as a subprocess of the worker. A subprocess
    shares the worker's filesystem, its environment and therefore its secrets;
  - the database they are let loose in is a throwaway of its own, not the
    instance every other app lives in. A scratch *schema* in the customer
    instance is not isolation: `scratch_dsn` was that instance's admin, which is
    the one credential section 18.2 says stays with the worker.

So the interesting assertions here are about what the build job is *not* given.
Whether it then works is proved by the whole of `test_clean.py`, which now runs
every build this way.
"""

from __future__ import annotations

import json
import uuid

import asyncpg
import pytest_asyncio

from control_plane.models import DeploymentStatus
from worker.pipeline import build, container
from worker.pipeline.deploy import State, _spec
from worker.pipeline.detect import Stack


def test_the_build_job_runs_in_a_container():
    argv = container.docker_run_argv(
        image="vibedeploy/buildjob:test",
        command=["python", "-m", "buildjob.run"],
        mounts={},
        env={},
    )

    assert argv[:2] == ["docker", "run"]
    # Nothing of ours is left behind to be reused by the next app's code.
    assert "--rm" in argv
    assert argv[-4:] == ["vibedeploy/buildjob:test", "python", "-m", "buildjob.run"]


def test_the_container_gets_no_shell_and_no_root_of_ours():
    argv = container.docker_run_argv(
        image="x", command=["true"], mounts={}, env={}
    )

    # Section 3 rule 11 is about what untrusted code can reach, so the things
    # that would make reaching easy are refused up front.
    assert "--network" in argv
    assert argv[argv.index("--network") + 1] != "host"
    assert "--privileged" not in argv


def test_credentials_never_travel_in_argv(tmp_path):
    """`argv` is readable by every process on the box. The spec file is 0600."""
    argv = container.docker_run_argv(
        image="x",
        command=["true"],
        mounts={tmp_path: "/vd/repo"},
        env={"PGPASSWORD": "hunter2"},
    )

    assert "hunter2" not in " ".join(argv)
    # Passed by name, for `docker` to read out of our own environment.
    assert "--env" in argv and "PGPASSWORD" in argv


def test_a_mount_is_read_only_unless_it_is_asked_not_to_be(tmp_path):
    argv = container.docker_run_argv(
        image="x", command=["true"], mounts={tmp_path: "/vd/repo"}, env={}
    )
    joined = " ".join(argv)

    assert f"{container.host_path(tmp_path)}:/vd/repo:ro" in joined


def test_the_apps_own_packages_go_in_a_volume_and_not_on_the_host(tmp_path):
    """The one writable mount lives nowhere of ours, and holds nothing of ours.

    A host path under the deployment workdir would put an app's `pip install`
    in the same tree as `plan-spec.json`, which carries the build job's one-time
    token. On Docker Desktop it is also three times slower, which is what made
    this worth doing rather than worth arguing about.
    """
    workdir = tmp_path / "deployments" / "11111111-1111-1111-1111-111111111111"
    volume = build.deps_volume(workdir)

    assert isinstance(volume, container.Volume)
    assert volume.name == "vd-deps-11111111-1111-1111-1111-111111111111"
    # Nothing was created on this machine to back it.
    assert not workdir.exists()

    argv = container.docker_run_argv(
        image="x",
        command=["true"],
        mounts={tmp_path: "/vd/repo", volume: build.DEPS_IN_CONTAINER},
        env={},
        writable=[build.DEPS_IN_CONTAINER],
    )
    joined = " ".join(argv)

    assert f"{volume.name}:{build.DEPS_IN_CONTAINER}" in joined
    assert f"{volume.name}:{build.DEPS_IN_CONTAINER}:ro" not in joined
    # The checkout beside it is still read-only, so "writable" named one mount.
    assert f"{container.host_path(tmp_path)}:/vd/repo:ro" in joined


def test_a_served_app_is_not_thrown_away_when_it_dies():
    """`--rm` is a choice per container, not a property of the builder.

    A build job keeps it: it runs to completion and leaves nothing of ours
    behind. A served app must not have it, because "live, and nothing
    listening" is the whole of what `lost` means and a container that removed
    itself on exit makes that indistinguishable from one that was never
    started.
    """
    build_job = container.docker_run_argv(
        image="x", command=["true"], mounts={}, env={}
    )
    served = container.docker_run_argv(
        image="x", command=["true"], mounts={}, env={}, rm=False, name="vd-app-g1"
    )

    assert "--rm" in build_job
    assert "--rm" not in served
    assert served[served.index("--name") + 1] == "vd-app-g1"


def test_an_app_that_joins_the_gates_namespace_is_not_given_a_resolver():
    """`--add-host` and `--network container:` are a combination docker refuses.

    Which is the point rather than an inconvenience: a container that has
    joined another's network namespace has no interface and no resolver of its
    own to add anything to. Everything it can reach, it reaches through the
    gate.
    """
    joined = container.docker_run_argv(
        image="x",
        command=["true"],
        mounts={},
        env={},
        network="container:vd-sidecar-g1",
        rm=False,
    )

    assert "--add-host" not in joined
    assert joined[joined.index("--network") + 1] == "container:vd-sidecar-g1"
    # And the ordinary case still gets it, or the build job stops being able to
    # call home from a Linux host.
    ordinary = container.docker_run_argv(image="x", command=["true"], mounts={}, env={})
    assert "--add-host" in ordinary


def test_every_container_is_bounded():
    """Not an isolation claim — a blast-radius one.

    Several apps share one host, and the kernel's OOM killer chooses its victim
    by heuristic: an app that allocates without limit is as likely to take down
    the worker or Postgres as itself. These four flags make the offender the
    one that dies. Asserted for both shapes because they are set in the one
    place every `docker run` in this file goes through, and the way to lose
    them is for a later caller to build its own argv instead.
    """
    for argv in (
        container.docker_run_argv(image="x", command=["true"], mounts={}, env={}),
        container.docker_run_argv(
            image="x",
            command=["true"],
            mounts={},
            env={},
            network="container:vd-sidecar-g1",
            rm=False,
        ),
    ):
        assert argv[argv.index("--memory") + 1] == container.CONTAINER_MEMORY
        assert argv[argv.index("--pids-limit") + 1] == container.CONTAINER_PIDS
        assert argv[argv.index("--cpus") + 1] == container.CONTAINER_CPUS
        # Equal to `--memory`, so the limit cannot be exceeded slowly by
        # swapping. A larger value here would make the memory limit advisory.
        assert argv[argv.index("--memory-swap") + 1] == container.CONTAINER_MEMORY


def test_only_the_gate_is_published():
    published = container.docker_run_argv(
        image="x",
        command=["true"],
        mounts={},
        env={},
        rm=False,
        publish=["127.0.0.1:0:8080"],
    )
    # Bound to loopback, and to a port the kernel picks: the worker probes the
    # health gate from the host, and nothing else has any business reaching it.
    assert published[published.index("--publish") + 1] == "127.0.0.1:0:8080"


def test_the_host_the_container_calls_home_on_is_not_its_own_loopback():
    """Inside a container `127.0.0.1` is the container. It means nothing here."""
    assert (
        container.reachable_from_container("http://127.0.0.1:8100/internal/builds/1")
        == "http://host.docker.internal:8100/internal/builds/1"
    )
    assert (
        container.reachable_from_container(
            "postgresql://vd_admin:pw@127.0.0.1:55434/vibedeploy_build"
        )
        == "postgresql://vd_admin:pw@host.docker.internal:55434/vibedeploy_build"
    )
    # A host that is already reachable is left exactly as it is.
    assert (
        container.reachable_from_container("http://control-plane:8100/x")
        == "http://control-plane:8100/x"
    )


async def test_no_container_means_no_deploy(pipeline, monkeypatch):
    """Protected. Rule 11 has no second path, so there is no fallback to try.

    A containerised runner that quietly ran a subprocess when `docker` was
    missing would pass every other test in this file, so this one takes
    `docker` away and insists the deploy stops — and that it stops saying
    something the person reading the page can act on.
    """
    monkeypatch.setattr(container, "DOCKER", "definitely-not-docker")

    deployment_id = await pipeline.deploy("clean", name="nodocker")
    deployment = await pipeline.deployment(deployment_id)

    assert deployment.status == DeploymentStatus.blocked.value
    assert deployment.blocked_reason == container.NO_DOCKER


# ---------------------------------------------------------------------------
# The throwaway database is a different database
# ---------------------------------------------------------------------------


def test_the_build_job_is_not_given_the_customer_database(pipeline):
    """The spec is the whole of what the build job is told, so read it."""
    spec = _spec(
        pipeline.config,
        _state_of(pipeline),
        _stack(),
        "plan",
        "token",
        pipeline.config.state_root,
    )

    assert spec["scratch_dsn"] == pipeline.config.build_dsn
    assert pipeline.config.app_admin_dsn not in str(spec)
    assert pipeline.config.build_dsn != pipeline.config.app_admin_dsn


async def test_no_app_ever_lives_in_the_throwaway_database(pipeline, live):
    """Proof the two are really different instances, not two names for one.

    A live app's schema is in the customer database. If it can also be seen
    from the throwaway, then the build job — which is admin there, and runs code
    we have never read — was handed the instance people are live on.
    """
    app = await pipeline.app_of(live)
    query = "select count(*) from pg_namespace where nspname = $1"

    throwaway = await asyncpg.connect(pipeline.config.build_dsn)
    customer = await asyncpg.connect(pipeline.config.app_admin_dsn)
    try:
        assert await customer.fetchval(query, app.app_id) == 1
        assert await throwaway.fetchval(query, app.app_id) == 0
        # And nothing else's either: the build job's own scratch schemas are
        # dropped behind it, so this database is empty between builds.
        assert (
            await throwaway.fetchval(
                "select count(*) from pg_namespace where nspname like 'app\\_%'"
            )
            == 0
        )
    finally:
        await throwaway.close()
        await customer.close()


@pytest_asyncio.fixture(scope="session")
async def live(pipeline):
    """One real deployment, all the way to live, built in containers."""
    deployment_id = await pipeline.deploy("clean", name="isolation")
    assert (await pipeline.deployment(deployment_id)).status == (
        DeploymentStatus.awaiting_confirmation.value
    )
    await pipeline.confirm(deployment_id)
    assert (await pipeline.deployment(deployment_id)).status == (
        DeploymentStatus.live.value
    ), (await pipeline.deployment(deployment_id)).blocked_reason
    return deployment_id


async def test_a_real_deployment_really_went_through_a_container(pipeline, live):
    """Read what was actually handed to the build job on a real deploy.

    The assertions above are about a spec built in this process. This one is
    about the file a container was started with, because a containerised runner
    that quietly fell back to a subprocess would pass every other test in this
    file.
    """
    deployment_id = live
    spec = json.loads(
        (
            pipeline.config.state_root
            / "deployments"
            / str(deployment_id)
            / "plan-spec.json"
        ).read_text(encoding="utf-8")
    )

    # Paths and addresses as the container sees them, which is the only reason
    # these differ from what `_spec` wrote.
    assert spec["repo_dir"] == build.REPO_IN_CONTAINER
    assert container.HOST_ALIAS in spec["scratch_dsn"]
    assert container.HOST_ALIAS in spec["callback_url"]
    assert "vibedeploy_build" in spec["scratch_dsn"]


# ---------------------------------------------------------------------------
# Helpers that keep the spec test honest without deploying anything
# ---------------------------------------------------------------------------


def _state_of(pipeline):
    return State(
        deployment_id=uuid.uuid4(),
        app_pk=uuid.uuid4(),
        app_id="app_isolation",
        repo="repo",
        subdomain="isolation",
        ref="main",
        commit_sha=None,
        status="queued",
        answers={},
        stack=None,
        generation=1,
    )


def _stack():
    return Stack(
        language="python",
        framework="fastapi",
        db_lib="sqlalchemy",
        migrations_kind="sql",
        migrations_path="migrations",
        python_version=None,
        entrypoint="app:app",
        protection="protected",
        unprotected_reason=None,
    )
