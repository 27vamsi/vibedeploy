"""The control plane process.

It owns records and decisions about records. It does not clone repositories, it
does not run migrations and it never connects to an app's schema to read app
data: the worker and the build job do that, on the other side of a process
boundary.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from control_plane import auth, migrate, service
from control_plane.api import hooks, internal
from control_plane.config import ControlPlaneConfig
from control_plane.db import make_engine, make_sessionmaker
from control_plane.gateway_client import GatewayClient
from control_plane.web import routes as web


async def _bootstrap_operator(sessionmaker, config: ControlPlaneConfig) -> None:
    """Create the first operator from the environment, if there is none.

    This exists so that a box with a public address never has to serve the
    setup page, where the first stranger to find it would become the operator.
    With both variables set, the console comes up already owned.

    It is not an upsert: if a builder exists, the environment is ignored
    entirely. Otherwise `VD_BOOTSTRAP_PASSWORD` would be a standing password
    reset for anybody who can read the env file, and rotating the operator's
    password would silently undo itself on the next restart.
    """
    if not (config.bootstrap_email and config.bootstrap_password):
        return
    async with sessionmaker() as session:
        if await auth.sole_builder(session) is not None:
            return
        await service.ensure_builder(
            session, email=config.bootstrap_email, password=config.bootstrap_password
        )
        await session.commit()


def create_app(config: ControlPlaneConfig | None = None) -> FastAPI:
    config = config or ControlPlaneConfig.from_env()
    # Before anything is served, and outside the lifespan on purpose: an
    # unsigned console is not a degraded console, it is an open one, so this
    # has to be a refusal to exist rather than a warning in a log.
    sessions = auth.Sessions(
        secret=config.session_secret, secure=config.cookies_secure
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Before the engine, because there may be no database yet. Opt-in by
        # environment rather than always: the test suite migrates its own
        # database before it starts anything, and a second pass per spawned
        # process would be a lock to wait on for nothing. `infra/prod` sets it.
        if (os.environ.get("VD_MIGRATE_ON_BOOT") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            await migrate.prepare(config)
        engine = make_engine(config)
        app.state.config = config
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        app.state.sessions = sessions
        await _bootstrap_operator(app.state.sessionmaker, config)
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
    # The only router here anyone outside this machine may reach. It
    # authenticates every request itself, with GitHub's signature.
    app.include_router(hooks.router)
    # The login pages first, then everything that requires having used them.
    app.include_router(web.public)
    app.include_router(web.router)

    @app.get("/health")
    async def health() -> JSONResponse:
        """Answers for the database, not just for the process.

        `{"ok": true}` unconditionally made this endpoint worse than nothing
        once a restart policy and a load balancer were reading it: a process
        whose database has gone away answers every real request with a 500 and
        would have gone on reporting itself healthy, so nothing would ever
        restart it and traffic would keep arriving.

        503 rather than 500, because "not ready" is what a balancer knows how
        to act on. The reason is not included: this is reachable without a
        session.
        """
        try:
            async with app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"ok": False}, status_code=503)
        return JSONResponse({"ok": True})

    return app
