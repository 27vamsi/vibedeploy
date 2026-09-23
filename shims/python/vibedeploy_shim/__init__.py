"""vibedeploy identity shim. Readme.md section 14.

The app never imports this. It is delivered as an extra image layer with a
`sitecustomize.py` on PYTHONPATH, so it installs itself before the app's own
code runs.

The startup line is a contract: the worker greps for it, and an app that does
not print it is marked Unprotected and its deploy is blocked when protection
was expected.
"""

from __future__ import annotations

import logging

from vibedeploy_shim.identity import (
    Identity,
    current_identity,
    sign_identity,
    verify_identity,
)

__all__ = [
    "Identity",
    "current_identity",
    "install",
    "sign_identity",
    "verify_identity",
]

log = logging.getLogger("vibedeploy_shim")

_installed = False


def _detect_db() -> str:
    try:
        import sqlalchemy  # noqa: F401
    except ImportError:
        return "unknown"
    return "sqlalchemy"


def install() -> None:
    """Idempotent: importing twice, or a sitecustomize that runs again under a
    reloader, must not stack two middlewares or two DB hooks."""
    global _installed
    if _installed:
        return

    db = _detect_db()
    if db == "sqlalchemy":
        from vibedeploy_shim.db import install_db_hook

        install_db_hook()

    from vibedeploy_shim.frameworks import install_frameworks

    framework = install_frameworks()
    _installed = True

    # Printed, not just logged: the app may configure logging however it likes,
    # and the worker has to be able to see this.
    print(
        f"vibedeploy-shim active lang=python db={db} framework={framework}",
        flush=True,
    )
