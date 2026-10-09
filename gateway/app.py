"""The gateway process. Readme.md section 6, section 21.

One ASGI app, so that this is the whole deployment:

    VD_SIDECAR_API_KEY=... python -m uvicorn gateway.app:build --factory --port 8200

A factory rather than a module-level `app` because the configuration is read
from the environment and refuses to guess (`ConfigError`), and a module that
cannot be imported without a full environment cannot be imported by a test
either.

Two doors, and they take different keys, because they are used by different
kinds of caller:

  - `POST /mcp` — an agent, with `Authorization: Bearer vd_agent_...`.
  - `/internal/*` — the console, with `X-VD-Console-Key`. Approving an action
    is a person's act, so an agent key is not the credential for it.

Section 21 also names a REST equivalent of `/mcp` (`POST /v1/actions`), and it
is not here because nothing in the milestones or the demo calls it: writing it
now would mean a second untested path into the pipeline, which is exactly what
"MCP is just another front door" is warning against.

The gateway is given the control plane's database and the secrets root, and
nothing else. In particular it is given no app credentials: it reads
`vd/apps/*/agent-*` and only that, through `control_plane.secrets`, per the
rule in CLAUDE.md. There is no configuration here that could widen that.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.routing import Mount

from control_plane.config import ControlPlaneConfig
from control_plane.db import make_engine, make_sessionmaker
from gateway import api, mcp_server
from gateway.pipeline import Gateway

INTERNAL = "/internal"


def build(config: ControlPlaneConfig | None = None):
    config = config or ControlPlaneConfig.from_env()
    sessions = make_sessionmaker(make_engine(config))
    gateway = Gateway(
        sessionmaker=sessions, secrets_root=config.state_root / "secrets"
    )
    return mcp_server.build(
        gateway, extra_routes=[Mount(INTERNAL, app=internal(gateway, config))]
    )


def internal(gateway: Gateway, config: ControlPlaneConfig) -> FastAPI:
    """The console's door. A sub-application so that its dependencies, its
    validation and its error shapes are FastAPI's and not hand-rolled."""
    app = FastAPI(title="vibedeploy gateway (internal)", docs_url=None, redoc_url=None)
    app.include_router(api.router)
    app.state.gateway = gateway
    app.state.console_key = config.gateway_api_key

    @app.get("/health")
    async def health() -> dict[str, bool]:
        """Unguarded, and says nothing but that the process is up.

        A load balancer has no console key, and the gateway's only other paths
        are an agent's door and an administrator's. Both of those are 401 or
        403 to a health check, which a target group reads as a dead task.
        """
        return {"ok": True}

    return app
