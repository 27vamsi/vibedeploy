"""Put the identity into every transaction. Readme.md section 14.

The listener is attached to the `Engine` class, not to one engine instance, so
it covers engines the app creates later, including the sync engine that sits
inside an AsyncEngine.

`set_config(..., true)` is transaction-local: it is gone at COMMIT, which is
exactly when a transaction pooler hands the connection to the next user. Bind
parameters, never string formatting. No identity means `''`, which
`vd_user_id()` turns into NULL, which matches no row.
"""

from __future__ import annotations

from typing import Any

from .identity import current

SET_IDENTITY_SQL = (
    "SELECT set_config('app.user_id', :vd_user_id, true), "
    "set_config('app.role', :vd_role, true)"
)

_installed = False


def _on_begin(conn: Any) -> None:
    from sqlalchemy import text

    who = current()
    conn.execute(
        text(SET_IDENTITY_SQL),
        {
            "vd_user_id": who.sub if who else "",
            "vd_role": who.role if who else "",
        },
    )


def install() -> bool:
    """Returns True if SQLAlchemy is present and the hook is attached."""
    global _installed
    if _installed:
        return True
    try:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine
    except ImportError:
        return False

    event.listen(Engine, "begin", _on_begin)
    _installed = True
    return True
