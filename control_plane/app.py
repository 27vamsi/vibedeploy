"""The control plane process.

It owns records and decisions about records. It does not clone repositories, it
does not run migrations and it never connects to an app's schema to read app
data: the worker and the build job do that, on the other side of a process
boundary.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from control_plane.api import internal
from control_plane.config import ControlPlaneConfig
from control_plane.db import make_engine, make_sessionmaker
from control_plane.gateway_client import GatewayClient
from control_plane.web import routes as web


def create_app(config: ControlPlaneConfig | None = None) -> FastAPI:
    config = config or ControlPlaneConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(config)
        app.state.config = config
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        # Everything about agents in flight — approvals, activity, the audit
        # chain, the evidence report — is the gateway's to answer, because the
        # console holds no app credentials with which to answer it.
        app.state.gateway = GatewayClient(
            base_url=config.gateway_url, api_key=config.gateway_api_key
        )
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="vibedeploy control plane", lifespan=lifespan)
    app.include_router(internal.router)
    app.include_router(web.router)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    return app
