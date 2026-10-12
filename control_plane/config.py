"""Everything the control plane and the worker are told.

Two different databases on purpose:

  - `database_url` is the control plane's own, holding apps, deployments,
    access models and the job queue.
  - `app_admin_dsn` is the customer database, where every app gets its own
    schema and its own four roles. The control plane never reads app data
    through it; the worker uses it to provision.
  - `build_dsn` is the build job's throwaway, and it is a third instance rather
    than a third schema because the build job runs an app's own migrations and
    is therefore handed admin of whatever it is pointed at. Section 18.2 keeps
    the customer database's admin with the worker, so that is not what the
    build job gets.

Keeping them apart means a hole in the dashboard cannot reach into an app's
rows, an app's roles have no name to even mention the control plane's tables,
and code we have never read cannot drop a schema somebody is live on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]

# The local compose stack, documented in infra/local/docker-compose.yml. These
# are in the repo already and are never used anywhere but a developer laptop,
# so defaulting to them costs nothing. Secrets get no defaults.
LOCAL_PG = "127.0.0.1:55432"
LOCAL_BUILD_PG = "127.0.0.1:55434"
LOCAL_ADMIN = "vd_admin:vd_local_password"

DEFAULT_CONTROL_URL = f"postgresql+asyncpg://{LOCAL_ADMIN}@{LOCAL_PG}/vibedeploy_control"
DEFAULT_APP_DSN = f"postgresql://{LOCAL_ADMIN}@{LOCAL_PG}/vibedeploy"
DEFAULT_BUILD_DSN = f"postgresql://{LOCAL_ADMIN}@{LOCAL_BUILD_PG}/vibedeploy_build"


class ConfigError(RuntimeError):
    """The control plane cannot start safely, so it must not start at all."""


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise ConfigError(
            f"{name} is not set. The control plane refuses to start without it"
            " rather than fall back to a default nobody chose."
        )
    return value


def _in_prod(env: Mapping[str, str]) -> bool:
    """`VD_ENV=prod` turns every convenience below into a refusal to start.

    The defaults in this file are good defaults for a laptop and catastrophic
    ones anywhere else — `LOCAL_ADMIN` is a password that is in the repository,
    and an empty `gateway_api_key` or webhook secret is a door with no lock on
    a box that has a public address. Rather than remove them and make the test
    suite carry fifteen environment variables, one switch decides which of the
    two installations this is.

    It is opt-in, which means a careless operator still gets the laptop
    defaults. The alternative — default to strict — makes `pytest` the thing
    that has to be configured, and a suite nobody can run is worse than this.
    So prod packaging sets it in the env file, and `infra/prod` is what carries
    that responsibility.
    """
    return (env.get("VD_ENV") or "").strip().lower() in ("prod", "production")


def _truthy(value: str | None) -> bool:
    """The same three spellings `VD_MIGRATE_ON_BOOT` accepts."""
    return (value or "").strip().lower() in ("1", "true", "yes")


def _secret(env: Mapping[str, str], name: str, local: str = "") -> str:
    """Required in prod, whatever the laptop default is otherwise."""
    if _in_prod(env):
        return _required(env, name)
    return env.get(name) or local


def _secret_key(env: Mapping[str, str]) -> str:
    """The key app secrets are encrypted with, from the env or from a file.

    A file is offered because this is the one secret that must *not* change
    between restarts — change it and every stored credential becomes
    unreadable — and a file is easier to keep out of a process listing and a
    container inspect than an environment variable is.
    """
    path = env.get("VD_SECRET_KEY_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return _secret(env, "VD_SECRET_KEY")


@dataclass(frozen=True)
class ControlPlaneConfig:
    database_url: str
    app_admin_dsn: str
    sidecar_api_key: str
    # Where the worker keeps per-app secrets and per-deployment working trees.
    # Stands in for Secrets Manager and the build cache until M8.
    state_root: Path
    # What a build job posts its result back to. It runs as a separate process
    # with no database credentials of its own, so this is its only way home.
    public_url: str

    # What console session cookies are signed with. There is no default and
    # there is no "unsigned" mode: an empty secret here would make every
    # session forgeable, which is indistinguishable from having no login at
    # all. `from_env` requires it, and `control_plane.app.create_app` refuses a
    # config that does not carry one.
    session_secret: str = ""

    # The key app secrets are encrypted with at rest, and the scheme the
    # console is reached over. The scheme is not decoration: it decides whether
    # the session cookie is marked `Secure`, and a `Secure` cookie over plain
    # HTTP is a cookie the browser never sends back.
    secret_key: str = ""
    public_scheme: str = "http"

    # The first operator, created on boot if the installation has none. Absent,
    # a fresh installation shows a one-time setup page instead — which is the
    # right answer for a laptop and the wrong one for a box with a public
    # address, where whoever finds it first would become the operator.
    bootstrap_email: str = ""
    bootstrap_password: str = ""

    # Whether the defaults in this module are allowed to apply at all.
    env: str = "local"

    # The gateway, and the one key the console is allowed to talk to it with.
    #
    # Two processes rather than one because of what each may read: the gateway
    # holds `vd/apps/*/agent-*` and the console holds none of it. Approving an
    # action redoes the dry run, which needs that secret, so the console asks
    # the gateway to do it instead of reaching for the secret itself. In AWS
    # these are separate tasks with separate roles and this split is the only
    # thing that makes that worth anything.
    gateway_url: str = "http://127.0.0.1:8200"
    gateway_api_key: str = ""

    # What GitHub signs its push deliveries with (section 15). Empty is not
    # "unchecked", it is "nobody gets in": `/hooks/github` is the one endpoint a
    # stranger can reach, and an unsigned one queues builds for whoever finds
    # it. So this has no default, and a laptop with no webhook configured
    # simply has an endpoint that answers 401.
    github_webhook_secret: str = ""

    # Admin of the throwaway database the build job is let loose in, and the
    # image it runs in. Both are only ever used by `worker.pipeline.build`.
    build_dsn: str = DEFAULT_BUILD_DSN
    build_image: str = "vibedeploy/buildjob:local"

    # The image a deployed app and its login gate run in. One generic image for
    # every app, not one per app: everything that differs between two apps is
    # mounted or passed in, so a deploy costs one `pip install` into a volume
    # the build job has to fill anyway rather than a second `docker build`.
    # Per-app images are the better production answer and are one module away —
    # `container.build_image` already exists and the runtime picks the image by
    # name, which is this field.
    app_image: str = "vibedeploy/apprt:local"

    # The proxy every app is reached through (section 16). An app is published
    # at `http://<subdomain>.<app_domain>:<app_port>` and at no other address:
    # there is no fallback that hands out the sidecar's port, because then
    # "reachable only through its gate" would depend on nobody guessing.
    #
    # `proxy_admin_url` can rewrite the whole proxy configuration, so it is a
    # loopback address here and would be a private one anywhere else.
    proxy_admin_url: str = "http://127.0.0.1:55019"
    app_domain: str = "localhost"
    app_port: int = 55080

    # The hostname the console itself answers on, when it is published through
    # the same proxy the apps are. Empty means it is not: on a laptop the
    # console is reached directly on 8100 and there is nothing to route.
    #
    # It goes through the one proxy rather than getting a listener of its own
    # because two servers cannot share `:443` — and routing it with the apps
    # means the reconciler restates the console's own route every pass, so a
    # wiped proxy does not leave the operator locked out of the thing that
    # would fix it.
    console_domain: str = ""

    # The cutover. A new version is started, then watched, then given the
    # traffic, then the old one is drained and stopped — `worker/pipeline/
    # cutover.py` argues for each of these numbers.
    #
    # `health_passes` is *consecutive* passes: one probe only says the process
    # answered once, which an app that crashes on its second request also does.
    # `drain_seconds` has to be longer than the longest request an app serves,
    # because it is the whole of what stands between a cutover and somebody's
    # request ending in a 502.
    health_passes: int = 3
    health_interval: float = 0.5
    health_timeout: float = 30.0
    drain_seconds: float = 2.0

    # How long the reconciler waits between passes. It is also the worst case
    # for every claim the loop makes — "reaped within one interval" is this
    # number — so it is short enough to be a believable recovery time and long
    # enough that a laptop is not spending itself on a sweep that finds nothing.
    reconcile_interval: float = 10.0

    # Whether the worker itself is in a container, which decides how it reaches
    # an app's login gate before that app has a route.
    #
    # It is one fact with two consequences, which is why it is not two knobs.
    # On a laptop the worker is a process on the host, so the gate publishes a
    # port on `127.0.0.1` and the worker probes that. In `infra/prod` the worker
    # is a container, where `127.0.0.1` is the worker — so a published host port
    # is both unreachable (a port bound to the host's loopback is not reachable
    # from the bridge, which is what `container.reachable_from_container`'s own
    # caveat says) and pointless. There the gate publishes nothing at all and is
    # probed by container name on `VD_APP_NETWORK`.
    #
    # Getting this wrong is loud, not subtle: every deploy fails in the health
    # gate, which is the correct direction to fail in.
    worker_in_container: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ControlPlaneConfig":
        env = os.environ if env is None else env
        state_root = env.get("VD_STATE_ROOT")
        prod = _in_prod(env)
        return cls(
            # In prod there is no `LOCAL_ADMIN` to fall back to. That password
            # is in this repository, so a DSN defaulted from it on a public box
            # is an open database, not a convenience.
            database_url=(
                _required(env, "VD_CONTROL_DATABASE_URL")
                if prod
                else env.get("VD_CONTROL_DATABASE_URL") or DEFAULT_CONTROL_URL
            ),
            app_admin_dsn=(
                _required(env, "VD_APP_ADMIN_DSN")
                if prod
                else env.get("VD_APP_ADMIN_DSN") or DEFAULT_APP_DSN
            ),
            sidecar_api_key=_required(env, "VD_SIDECAR_API_KEY"),
            state_root=Path(state_root) if state_root else REPO_ROOT / ".vd-state",
            public_url=(env.get("VD_CONTROL_PLANE_URL") or "http://127.0.0.1:8100").rstrip(
                "/"
            ),
            # No local default, in any environment. This one is not a trade-off
            # between convenience and safety: a shared default signing key is
            # the same thing as no login.
            session_secret=_required(env, "VD_SESSION_SECRET"),
            secret_key=_secret_key(env),
            public_scheme=(env.get("VD_PUBLIC_SCHEME") or "http").strip().lower(),
            bootstrap_email=env.get("VD_BOOTSTRAP_EMAIL") or "",
            bootstrap_password=env.get("VD_BOOTSTRAP_PASSWORD") or "",
            env="prod" if prod else (env.get("VD_ENV") or "local"),
            gateway_url=(env.get("VD_GATEWAY_URL") or "http://127.0.0.1:8200").rstrip("/"),
            gateway_api_key=_secret(env, "VD_GATEWAY_API_KEY"),
            github_webhook_secret=_secret(env, "VD_GITHUB_WEBHOOK_SECRET"),
            build_dsn=(
                _required(env, "VD_BUILD_DATABASE_URL")
                if prod
                else env.get("VD_BUILD_DATABASE_URL") or DEFAULT_BUILD_DSN
            ),
            build_image=env.get("VD_BUILD_IMAGE") or "vibedeploy/buildjob:local",
            app_image=env.get("VD_APP_IMAGE") or "vibedeploy/apprt:local",
            proxy_admin_url=(
                env.get("VD_PROXY_ADMIN_URL") or "http://127.0.0.1:55019"
            ).rstrip("/"),
            app_domain=env.get("VD_APP_DOMAIN") or "localhost",
            app_port=int(env.get("VD_APP_PORT") or 55080),
            console_domain=env.get("VD_CONSOLE_DOMAIN") or "",
            health_passes=int(env.get("VD_HEALTH_PASSES") or 3),
            health_interval=float(env.get("VD_HEALTH_INTERVAL") or 0.5),
            health_timeout=float(env.get("VD_HEALTH_TIMEOUT") or 30.0),
            drain_seconds=float(env.get("VD_DRAIN_SECONDS") or 2.0),
            reconcile_interval=float(env.get("VD_RECONCILE_INTERVAL") or 10.0),
            worker_in_container=_truthy(env.get("VD_WORKER_IN_CONTAINER")),
        )

    @property
    def sync_database_url(self) -> str:
        """The same control plane database for tools that cannot do asyncio."""
        return self.database_url.replace("+asyncpg", "")

    @property
    def cookies_secure(self) -> bool:
        """Whether cookies may be marked `Secure`.

        Decided by the scheme and nothing else. Marked `Secure` over plain
        HTTP, the browser stops sending the cookie and the console stops
        working; not marked over HTTPS, the session is one downgrade away from
        being readable. Neither is a choice worth offering separately.
        """
        return self.public_scheme == "https"
