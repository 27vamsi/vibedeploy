"""Reads the identity header once per request. Readme.md section 14.1.

The sidecar has already stripped any client-supplied copy of the header and
signed its own, so whatever arrives here either verifies against the per-app key
or is discarded.
"""

from __future__ import annotations

import logging
import os

from vibedeploy_shim.identity import (
    HEADER_NAME,
    reset_current_identity,
    set_current_identity,
    verify_identity,
)

log = logging.getLogger("vibedeploy_shim")


def _key() -> bytes:
    return os.environ.get("VD_IDENTITY_KEY", "").encode()


class IdentityMiddleware:
    """Pure ASGI, so it works under Starlette, FastAPI or anything else."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw = None
        for name, value in scope.get("headers", []):
            if name.decode("latin-1").lower() == HEADER_NAME:
                raw = value.decode("latin-1")
                break

        identity = verify_identity(
            raw, _key(), app_id=os.environ.get("VD_APP_ID") or None
        )
        token = set_current_identity(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            # Unconditional: a request that raised must not leave its identity
            # behind for whatever runs next on this task.
            reset_current_identity(token)
