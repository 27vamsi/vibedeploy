"""Binds the request's identity to every database transaction. Readme.md 14.2.

The hook is registered on the `Engine` *class*, so it covers every engine the
app creates, including the sync engine that an AsyncEngine wraps, and engines
created before the shim was imported.

This is the single most important function in the shim. If it does not run, the
transaction carries no identity and RLS returns nothing; if it runs with the
wrong scope, one user's identity leaks to the next request on that connection.
"""

from __future__ import annotations

import logging

from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from vibedeploy_shim.identity import current_identity

log = logging.getLogger("vibedeploy_shim")

# Transaction-local (the `true` third argument) is the whole game. A session
# level setting would survive being handed to the next request by a transaction
# mode pooler. Values are bound, never formatted into the SQL.
_SET_IDENTITY = text(
    "SELECT set_config('app.user_id', :user_id, true), "
    "       set_config('app.role', :role, true)"
)

_installed = False


def _on_begin(conn) -> None:
    identity = current_identity()
    # No identity is expressed as empty strings rather than by skipping the
    # statement. vd_user_id() turns '' into NULL, which matches no owner.
    conn.execute(
        _SET_IDENTITY,
        {
            "user_id": identity.sub if identity else "",
            "role": identity.role if identity else "",
        },
    )


def install_db_hook() -> None:
    global _installed
    if _installed:
        return
    event.listen(Engine, "begin", _on_begin)
    _installed = True
