"""Everything the control plane and the worker are told.

Two different databases on purpose:

  - `database_url` is the control plane's own, holding apps, deployments,
    access models and the job queue.
  - `app_admin_dsn` is the customer database, where every app gets its own
    schema and its own four roles. The control plane never reads app data
    through it; the worker uses it to provision, and the build job uses it to
    migrate, seed and attack.

Keeping them apart means a hole in the dashboard cannot reach into an app's
rows, and an app's roles have no name to even mention the control plane's
tables.
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
LOCAL_ADMIN = "vd_admin:vd_local_password"

DEFAULT_CONTROL_URL = f"postgresql+asyncpg://{LOCAL_ADMIN}@{LOCAL_PG}/vibedeploy_control"
DEFAULT_APP_DSN = f"postgresql://{LOCAL_ADMIN}@{LOCAL_PG}/vibedeploy"


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

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ControlPlaneConfig":
        env = os.environ if env is None else env
        state_root = env.get("VD_STATE_ROOT")
        return cls(
            database_url=env.get("VD_CONTROL_DATABASE_URL") or DEFAULT_CONTROL_URL,
            app_admin_dsn=env.get("VD_APP_ADMIN_DSN") or DEFAULT_APP_DSN,
            sidecar_api_key=_required(env, "VD_SIDECAR_API_KEY"),
            state_root=Path(state_root) if state_root else REPO_ROOT / ".vd-state",
            public_url=(env.get("VD_CONTROL_PLANE_URL") or "http://127.0.0.1:8100").rstrip(
                "/"
            ),
            gateway_url=(env.get("VD_GATEWAY_URL") or "http://127.0.0.1:8200").rstrip("/"),
            gateway_api_key=env.get("VD_GATEWAY_API_KEY") or "",
        )

    @property
    def sync_database_url(self) -> str:
        """The same control plane database for tools that cannot do asyncio."""
        return self.database_url.replace("+asyncpg", "")
