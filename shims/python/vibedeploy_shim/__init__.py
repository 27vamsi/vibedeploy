"""vibedeploy identity shim for Python. Readme.md section 14.

One startup line is printed on install. The worker reads it out of the app's
logs: if it is missing, or says a part is not active, the app is marked
Unprotected. We never claim protection we did not confirm.
"""

from __future__ import annotations

import sys

from . import db, frameworks, identity
from .identity import Identity, current

__all__ = ["Identity", "current", "install", "status_line"]

STATUS_PREFIX = "vibedeploy-shim"

_status: str | None = None


def status_line(*, framework: str | None, db_lib: str | None, key: bool) -> str:
    parts = [
        STATUS_PREFIX,
        "active" if (framework and db_lib and key) else "inactive",
        "lang=python",
        f"db={db_lib or 'none'}",
        f"framework={framework or 'none'}",
    ]
    if not key:
        parts.append("reason=no-identity-key")
    elif not db_lib:
        parts.append("reason=unsupported-database-library")
    elif not framework:
        parts.append("reason=unsupported-framework")
    return " ".join(parts)


def install() -> str:
    """Idempotent. Returns the startup line it printed."""
    global _status
    if _status is not None:
        return _status

    framework = frameworks.patch_all()
    db_lib = "sqlalchemy" if db.install() else None
    key = identity.load_key() is not None

    _status = status_line(framework=framework, db_lib=db_lib, key=key)
    print(_status, file=sys.stdout, flush=True)
    return _status
