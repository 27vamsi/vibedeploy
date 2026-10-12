"""The prod installation's wiring, where it is a claim rather than a comment.

Two of these claims used to be comments and were both wrong, which is the
reason this file exists:

  - **Caddy's admin API was a port on a network every app could reach.** The
    compose file said "reachable from the compose network and nowhere else" as
    if that were a boundary. It is not: Caddy dials each app's login gate by
    container name, so it has to share a network with every gate, and a gate
    shares its network namespace with the app container. So `caddy:2019` — the
    one endpoint that rewrites where all traffic goes, and which hostnames get
    certificates — was reachable by unreviewed app code. It is a unix socket in
    a volume two services mount now.

  - **The health gate could not have passed.** `apprt` probed the gate on
    `127.0.0.1:<published port>`, which is right for a worker that is a process
    on the host and wrong for one in a container, where `127.0.0.1` is the
    worker. Every deploy on the prod stack would have failed in the health
    gate — a thing no test could have caught, because no test ran the worker in
    a container.

So these are mostly consistency tests between files that have to agree and
cannot check each other: `env.example`, `docker-compose.yml` and the
`Caddyfile` all name the same socket path, and `VD_APP_NETWORK` has to name a
network the compose file actually defines. Each of those is a one-word typo
away from a stack that comes up and then cannot deploy.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from control_plane.config import REPO_ROOT, ControlPlaneConfig
from kernel.provision import AppRoles
from worker.pipeline import build, provision
from worker.runtimes import proxy

PROD = REPO_ROOT / "infra" / "prod"
BUILDJOB = REPO_ROOT / "infra" / "local" / "buildjob" / "Dockerfile"

# Our two uids and the group they share. Three files have to agree on these and
# none of them can see the others.
WORKER_UID = 10000
WORKER_GID = 10000
BUILD_UID = 10001

# Every `docker run --network` the worker makes is named by one of these, and
# none of them is a network Docker will create for us.
NETWORK_VARIABLES = ["VD_APP_NETWORK", "VD_BUILD_NETWORK", "VD_MIGRATE_NETWORK"]

# Enough to get a config out of `from_env`; the fields under test are the ones
# each test sets for itself.
MINIMAL = {
    "VD_SIDECAR_API_KEY": "sidecar-key",
    "VD_SESSION_SECRET": "session-secret",
    "VD_SECRET_KEY": "secret-key",
}


def _env_example() -> dict[str, str]:
    """`env.example` as the environment it is a template for."""
    values = {}
    for line in (PROD / "env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name] = value
    return values


def _config(**env: str) -> ControlPlaneConfig:
    return ControlPlaneConfig.from_env({**MINIMAL, **env})


# ---------------------------------------------------------------------------
# The admin API has no network address
# ---------------------------------------------------------------------------


def test_a_unix_admin_url_gets_a_socket_transport():
    """The point of the whole change: no TCP connection is attempted at all."""
    client, base = proxy._client(
        _config(VD_PROXY_ADMIN_URL="unix:///run/caddy-admin/admin.sock")
    )
    assert base == proxy.UNIX_AUTHORITY
    # httpx keeps the socket path on the transport's pool. Reaching for a
    # private attribute is the price of asserting the thing that matters —
    # that the path arrived — rather than that a constructor was called.
    assert client._transport._pool._uds == "/run/caddy-admin/admin.sock"


def test_a_tcp_admin_url_is_left_alone():
    """The local stack still uses a port, and this is what keeps it working."""
    client, base = proxy._client(_config(VD_PROXY_ADMIN_URL="http://caddy:2019"))
    assert base == "http://caddy:2019"
    assert getattr(client._transport._pool, "_uds", None) is None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="AF_UNIX: the behaviour under test is the errno, which Windows has"
    " no way to produce. Prod is Linux; this is not a skipped assertion, it is"
    " an assertion about a syscall this host does not have.",
)
async def test_an_unreachable_socket_blocks_the_deploy(tmp_path: Path):
    """A missing or unwritable socket is `ProxyUnavailable`, not a traceback.

    This is the fail-closed path, and it moved: over TCP the failure was an
    `httpx.HTTPError`, but a socket that is absent — or that this uid may not
    write to, which is the likely one on a real box — fails before HTTP starts.
    Without `OSError` in the `except`, a permissions problem on the socket came
    out of a deploy as a bare `PermissionError` instead of the sentence that
    tells an operator what to do.
    """
    missing = tmp_path / "nothing.sock"
    with pytest.raises(proxy.ProxyUnavailable):
        await proxy.require(_config(VD_PROXY_ADMIN_URL=f"unix://{missing}"))


def test_prod_points_the_worker_at_a_socket():
    assert _env_example()["VD_PROXY_ADMIN_URL"].startswith(proxy.UNIX_SCHEME)


def test_all_three_files_name_the_same_socket():
    """`env.example`, the compose mount and the Caddyfile cannot check each
    other, and a disagreement between any two of them is a stack that comes up
    and then blocks every deploy."""
    socket = _env_example()["VD_PROXY_ADMIN_URL"][len(proxy.UNIX_SCHEME) :]
    directory = str(Path(socket).parent).replace("\\", "/")

    caddyfile = (PROD / "Caddyfile").read_text(encoding="utf-8")
    compose = (PROD / "docker-compose.yml").read_text(encoding="utf-8")

    # Caddy's own spelling of a unix address is `unix/` plus the path, so an
    # absolute path shows up with the doubled slash.
    assert f"unix/{socket}" in caddyfile
    # Mounted into exactly two services. Counting is the assertion: a third
    # `caddy_admin:` line would be a third process that can rewrite every
    # route, which is the property this file is here to protect.
    assert compose.count(f"caddy_admin:{directory}") == 2


def test_only_80_and_443_are_published():
    """The claim in the compose file's own header, as something checkable.

    A `ports:` line is the only form of "reachable from the internet" anybody
    can audit, so the set of them is asserted exactly — including that Caddy's
    admin port is not among them, which is what regressing this change would
    look like.
    """
    compose = (PROD / "docker-compose.yml").read_text(encoding="utf-8")
    published = [
        line.strip().removeprefix("- ").strip('"')
        for line in compose.splitlines()
        if line.strip().startswith('- "') and ":" in line
    ]
    assert published == ["80:80", "443:443"]


# ---------------------------------------------------------------------------
# Three networks, and the variables that have to name them
# ---------------------------------------------------------------------------


def test_the_three_networks_are_three_networks():
    """The separation `env.example` used to describe and then undo by setting
    two of these to the same value.

    `VD_MIGRATE_NETWORK` is the one that matters most here: it is the only one
    of the three that reaches the customer database, so setting it to
    `VD_BUILD_NETWORK` would hand the build job — unreviewed migrations against
    a throwaway — the real one.
    """
    env = _env_example()
    networks = [env[name] for name in NETWORK_VARIABLES]
    assert len(set(networks)) == len(networks)


@pytest.mark.parametrize("variable", NETWORK_VARIABLES)
def test_the_network_variables_name_networks_compose_defines(variable: str):
    """Compose prefixes a network with the project name, so these two values
    are a derived fact that nothing in Docker will correct: a wrong one means
    `docker run --network` against a network that does not exist, and every
    deploy failing at the first container."""
    compose = (PROD / "docker-compose.yml").read_text(encoding="utf-8")
    project = next(
        line.partition(":")[2].strip()
        for line in compose.splitlines()
        if line.startswith("name:")
    )
    network = _env_example()[variable]
    assert network.startswith(f"{project}_")
    # The bare name, as the top-level `networks:` block declares it.
    assert f"\n  {network[len(project) + 1 :]}:\n" in compose


# ---------------------------------------------------------------------------
# The one file two containers share
# ---------------------------------------------------------------------------
#
# The worker writes the build job's spec and the build job reads it, as two
# different users in two different containers. That only works if a uid in one
# image, a gid in a second and a file mode in a third line up — and it is the
# kind of agreement that holds on a laptop for the wrong reason, because Docker
# Desktop's bind mounts have no Unix modes to honour. It failed on the first
# Linux box, in the build job, as `PermissionError: '/vd/spec.json'`.


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="The mode is the assertion, and Windows does not keep one.",
)
def test_the_build_job_can_read_its_own_spec(tmp_path: Path):
    """Group-readable, because the reader is another uid; not world-readable,
    because the spec holds the scratch DSN and a callback token."""
    spec = build.write_spec(tmp_path / "plan-spec.json", {"phase": "plan"})
    assert spec.stat().st_mode & 0o777 == 0o640


def test_the_two_images_agree_on_who_may_read_it():
    """The group in the mode above, named in both Dockerfiles.

    Asserted as text because the alternative is building two images to ask
    them. A `useradd` without an explicit `--gid` is the regression this
    catches: it picks the next free gid, which was the right one by luck.
    """
    worker = (PROD / "Dockerfile").read_text(encoding="utf-8")
    buildjob = BUILDJOB.read_text(encoding="utf-8")

    assert f"groupadd --gid {WORKER_GID}" in worker
    assert f"--uid {WORKER_UID} --gid {WORKER_GID}" in worker

    # The build job is its own user in the worker's group, and not the other way
    # around: `--append --groups` leaves its primary group alone, so what an
    # app's migrations create stays out of the worker's reach.
    assert f"groupadd --gid {WORKER_GID}" in buildjob
    assert f"--uid {BUILD_UID}" in buildjob
    assert "usermod --append --groups" in buildjob


def test_app_secrets_are_not_in_that_group():
    """The group says \"this one file\", and the thing it must not come to mean
    is \"anything the worker wrote\". App database credentials live in the same
    state root and stay readable by their owner alone."""
    secrets = (REPO_ROOT / "control_plane" / "secrets.py").read_text(encoding="utf-8")
    assert "0o600" in secrets


def test_only_the_database_is_on_the_migrate_network():
    """The claim that makes a fourth network worth having.

    This task runs an app's own migrations while holding `migrator`, the one
    role that bypasses RLS. Putting it on `apps` would have worked and would
    have given unreviewed code a name for Caddy, the control plane and every
    other running app's gate.
    """
    compose = (PROD / "docker-compose.yml").read_text(encoding="utf-8")
    # One service lists it, and `networks:` declares it. Two mentions, no more.
    assert compose.count("- migrate\n") == 1
    assert "\n  migrate:\n" in compose


# ---------------------------------------------------------------------------
# The one task that is allowed near the real database
# ---------------------------------------------------------------------------


async def test_the_migration_task_is_told_where_the_database_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """It used to get the default bridge, where a compose service name resolves
    to nothing — so a prod deploy passed the whole attack suite and then died
    on `Name or service not known`."""
    seen: dict[str, object] = {}

    async def fake_run(**kwargs: object) -> object:
        seen.update(kwargs)
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(provision.container, "run", fake_run)
    monkeypatch.setattr(provision, "MIGRATE_NETWORK", "vibedeploy_migrate")

    await provision.run_migration_task(
        _config(),
        AppRoles.generate("app_test"),
        checkout=tmp_path / "src",
        migrations_kind="sql",
        migrations_path="migrations",
        workdir=tmp_path,
    )
    assert seen["network"] == "vibedeploy_migrate"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="The mode is the assertion, and Windows does not keep one.",
)
async def test_the_migration_task_can_write_its_own_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Its output directory belongs to the worker and is written by the build
    job's uid, so it has to be group-writable. At 0755 every failure reached a
    builder as the one generic sentence, whatever had actually gone wrong."""

    async def fake_run(**kwargs: object) -> object:
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(provision.container, "run", fake_run)
    await provision.run_migration_task(
        _config(),
        AppRoles.generate("app_test"),
        checkout=tmp_path / "src",
        migrations_kind="sql",
        migrations_path="migrations",
        workdir=tmp_path,
    )
    assert (tmp_path / "migrate").stat().st_mode & 0o070 == 0o070


# ---------------------------------------------------------------------------
# Where the worker thinks it is
# ---------------------------------------------------------------------------


def test_prod_tells_the_worker_it_is_containerised():
    """Unset, the gate publishes a loopback port the worker cannot reach from
    inside its own container, and the health gate spends `VD_HEALTH_TIMEOUT`
    discovering it."""
    assert _config(**_env_example()).worker_in_container is True


def test_a_laptop_worker_is_not_containerised():
    """The default has to stay false: the suite runs the worker as a host
    process, and probing a container name from there resolves nothing."""
    assert _config().worker_in_container is False


def test_the_flag_only_changes_how_the_gate_is_reached():
    """Nothing else about an installation moves with it — in particular it is
    not a second way to say `VD_ENV=prod`."""
    local = _config()
    assert replace(local, worker_in_container=True).env == local.env
