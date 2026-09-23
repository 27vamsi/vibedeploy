"""Installs the middleware without the app asking. Readme.md section 14.

Readme section 14 says to patch `Starlette.__init__`. That does not reach
FastAPI: current FastAPI reimplements both `__init__` and
`build_middleware_stack` and calls neither via `super()`, so an app built with
`FastAPI()` would come out unprotected while still looking patched.

Patching `build_middleware_stack` instead works for both, and catches apps that
add their own middleware after construction, because the stack is rebuilt then.
"""

from __future__ import annotations

import logging

from vibedeploy_shim.asgi import IdentityMiddleware

log = logging.getLogger("vibedeploy_shim")


def _patch(cls) -> None:
    original = getattr(cls, "build_middleware_stack", None)
    if original is None or getattr(original, "__vd_patched__", False):
        # Already patched, possibly via a base class we patched first.
        return

    def build_middleware_stack(self):
        stack = original(self)
        # Idempotence: rebuilding the stack must not nest a second copy.
        if isinstance(stack, IdentityMiddleware):
            return stack
        return IdentityMiddleware(stack)

    build_middleware_stack.__vd_patched__ = True
    cls.build_middleware_stack = build_middleware_stack


def install_frameworks() -> str:
    """Patch whatever is importable and report what was found."""
    found = []

    try:
        from starlette.applications import Starlette
    except ImportError:
        pass
    else:
        _patch(Starlette)
        found.append("starlette")

    try:
        from fastapi import FastAPI
    except ImportError:
        pass
    else:
        _patch(FastAPI)
        found.append("fastapi")

    # FastAPI is the more specific answer when both are present.
    if "fastapi" in found:
        return "fastapi"
    if "starlette" in found:
        return "starlette"
    return "unknown"
