"""A stand-in for the control plane's two internal endpoints. Readme.md 16.

Only the shape of the contract matters here: the sidecar must send its API key,
must accept `sub` / `role` / `session_version`, and must stop trusting a session
whose version has moved on. The real control plane (argon2, a users table, rate
limits of its own) arrives with M7; this exists so the gate can be proven now
instead of after it.

Users arrive as JSON in `VD_STUB_USERS` because the test only learns the seeded
principal keys at runtime.
"""

from __future__ import annotations

import json
import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

APP_ID = os.environ["VD_APP_ID"]
API_KEY = os.environ["VD_SIDECAR_API_KEY"]

# email -> {"password", "sub", "role", "session_version"}
USERS: dict[str, dict] = json.loads(os.environ["VD_STUB_USERS"])
BY_SUB = {u["sub"]: u for u in USERS.values()}


def _authorised(request: Request) -> bool:
    return request.headers.get("x-vd-sidecar-key") == API_KEY


async def login(request: Request):
    if not _authorised(request) or request.path_params["app_id"] != APP_ID:
        return JSONResponse({"error": "no"}, status_code=403)
    body = await request.json()
    user = USERS.get(str(body.get("email", "")).lower())
    if user is None or user["password"] != body.get("password"):
        return JSONResponse({"error": "no"}, status_code=401)
    return JSONResponse(
        {
            "sub": user["sub"],
            "role": user["role"],
            "session_version": user["session_version"],
        }
    )


async def session_version(request: Request):
    if not _authorised(request) or request.path_params["app_id"] != APP_ID:
        return JSONResponse({"error": "no"}, status_code=403)
    user = BY_SUB.get(request.path_params["sub"])
    if user is None:
        return JSONResponse({"error": "no such user"}, status_code=404)
    return JSONResponse({"session_version": user["session_version"]})


async def bump(request: Request):
    """Test-only: what removing a user does to their open sessions."""
    user = BY_SUB.get(request.path_params["sub"])
    if user is None:
        return JSONResponse({"error": "no such user"}, status_code=404)
    user["session_version"] += 1
    return JSONResponse({"session_version": user["session_version"]})


async def health(request: Request):
    return JSONResponse({"ok": True})


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/internal/apps/{app_id}/login", login, methods=["POST"]),
        Route(
            "/internal/apps/{app_id}/users/{sub}/session-version",
            session_version,
            methods=["GET"],
        ),
        Route("/test/bump/{sub}", bump, methods=["POST"]),
    ]
)
