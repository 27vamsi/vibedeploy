"""Install the middleware without the app asking. Readme.md section 14.

We patch the framework's constructor rather than the app's own code because we
never see the app's code: the buildpack image is opaque to us and the builder
is not expected to edit anything.
"""

from __future__ import annotations

import functools
from typing import Any

from . import identity as identity_module
from .asgi import IdentityMiddleware
from .identity import HEADER_NAME, verify

_PATCHED = "_vibedeploy_patched"


def _patch_build_middleware_stack(cls: Any) -> None:
    """Wrap whatever the class builds, so we end up outermost.

    Readme.md section 14 says to patch `Starlette.__init__`. That no longer
    reaches FastAPI: current FastAPI versions neither call `Starlette.__init__`
    nor `super().build_middleware_stack()`, they reimplement both. So we patch
    `build_middleware_stack` on each class that defines it.

    The guard keeps this to exactly one wrap on any framework where the
    subclass does delegate to super().
    """
    original = cls.build_middleware_stack

    @functools.wraps(original)
    def build_middleware_stack(self: Any) -> Any:
        stack = original(self)
        if isinstance(stack, IdentityMiddleware):
            return stack
        return IdentityMiddleware(stack)

    setattr(build_middleware_stack, _PATCHED, True)
    cls.build_middleware_stack = build_middleware_stack


def _already_patched(cls: Any) -> bool:
    return getattr(cls.__dict__.get("build_middleware_stack"), _PATCHED, False)


def patch_starlette() -> str | None:
    try:
        from starlette.applications import Starlette
    except ImportError:
        return None

    if not _already_patched(Starlette):
        _patch_build_middleware_stack(Starlette)

    try:
        from fastapi.applications import FastAPI
    except ImportError:
        return "starlette"

    if "build_middleware_stack" in FastAPI.__dict__ and not _already_patched(FastAPI):
        _patch_build_middleware_stack(FastAPI)
    return "fastapi"


def _flask_before_request() -> None:
    from flask import g, request

    raw = request.headers.get(HEADER_NAME)
    g._vibedeploy_token = identity_module.set_current(
        verify(raw, identity_module.load_key())
    )


def _flask_teardown_request(_exception: BaseException | None) -> None:
    from flask import g

    token = g.pop("_vibedeploy_token", None)
    if token is not None:
        identity_module.reset_current(token)


def patch_flask() -> str | None:
    try:
        import flask
    except ImportError:
        return None

    if not getattr(flask.Flask, _PATCHED, False):
        original = flask.Flask.__init__

        @functools.wraps(original)
        def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
            original(self, *args, **kwargs)
            self.before_request(_flask_before_request)
            self.teardown_request(_flask_teardown_request)

        flask.Flask.__init__ = __init__
        setattr(flask.Flask, _PATCHED, True)

    return "flask"


def patch_all() -> str | None:
    return patch_starlette() or patch_flask()
