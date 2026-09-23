"""The shim must install itself into an app that never asked for it.

Readme.md section 14: the app does not import the shim. If auto-install fails
silently, the app runs with no identity, and under RLS that means it returns
nothing rather than leaking, but the deploy should have been blocked instead.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.applications import Starlette

import vibedeploy_shim
from vibedeploy_shim.asgi import IdentityMiddleware

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "shims" / "python" / "bootstrap"
SHIM_PKG = REPO_ROOT / "shims" / "python"


@pytest.fixture(autouse=True)
def installed():
    vibedeploy_shim.install()


def test_a_plain_fastapi_app_gets_the_middleware():
    """FastAPI reimplements build_middleware_stack, so this is the case that
    the Readme's suggested `Starlette.__init__` patch would have missed."""
    app = FastAPI()
    assert isinstance(app.build_middleware_stack(), IdentityMiddleware)


def test_a_plain_starlette_app_gets_the_middleware():
    app = Starlette()
    assert isinstance(app.build_middleware_stack(), IdentityMiddleware)


def test_rebuilding_the_stack_does_not_nest_a_second_copy():
    """Apps that add middleware after construction rebuild the stack."""
    app = FastAPI()
    once = app.build_middleware_stack()
    twice = app.build_middleware_stack()
    assert isinstance(twice, IdentityMiddleware)
    assert not isinstance(twice.app, IdentityMiddleware)
    assert not isinstance(once.app, IdentityMiddleware)


def test_installing_twice_is_a_no_op():
    vibedeploy_shim.install()
    vibedeploy_shim.install()
    app = FastAPI()
    stack = app.build_middleware_stack()
    assert not isinstance(stack.app, IdentityMiddleware)


def test_sitecustomize_prints_the_startup_line_the_worker_greps_for():
    """The worker treats a missing line as Unprotected. Readme.md section 14.3."""
    env = {
        "PYTHONPATH": f"{BOOTSTRAP}{';' if sys.platform == 'win32' else ':'}{SHIM_PKG}",
        "PATH": "",
        "SYSTEMROOT": "C:\\Windows",
    }
    result = subprocess.run(
        [sys.executable, "-c", "import fastapi"],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )
    assert "vibedeploy-shim active" in result.stdout, result.stderr
    assert "lang=python" in result.stdout
    assert "db=sqlalchemy" in result.stdout
