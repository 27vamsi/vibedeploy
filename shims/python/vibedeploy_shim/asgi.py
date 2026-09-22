"""Pure ASGI middleware that pins the identity to one request. Readme.md 14.

Pure ASGI rather than Starlette's BaseHTTPMiddleware on purpose: BaseHTTPMiddleware
runs the downstream app in a separate task, which would give it a copy of the
context and put the identity out of reach of code that resets it.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, MutableMapping

from . import identity as identity_module
from .identity import HEADER_NAME_BYTES, verify

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class IdentityMiddleware:
    def __init__(self, app: Any, key: bytes | None = None) -> None:
        self.app = app
        self._key = key

    @property
    def key(self) -> bytes | None:
        # Read lazily: the secret is in the environment before the app starts,
        # but importing the shim may happen earlier still.
        if self._key is None:
            self._key = identity_module.load_key()
        return self._key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        raw = None
        for name, value in scope.get("headers", ()):
            if name.lower() == HEADER_NAME_BYTES:
                raw = value
                break

        token = identity_module.set_current(verify(raw, self.key))
        try:
            await self.app(scope, receive, send)
        finally:
            identity_module.reset_current(token)
