"""The sloppy app the M2 proof runs against. Readme.md section 8, M2.

`/notes` is deliberately unfiltered - `SELECT id FROM notes`, no WHERE. If the
responses still contain only the caller's rows, that is the database doing it.

Two modes, chosen by VD_APP_MODE:

  shim    nothing in this file touches identity. `sitecustomize.py` is on
          PYTHONPATH, so the shim patched Starlette and SQLAlchemy before this
          module was imported, and every transaction opens with a
          transaction-local `set_config(..., true)`.

  leaky   the negative control. Identity is set session-level
          (`set_config(..., false)`) and committed, then the query runs in the
          next transaction - the "set it once for the connection" shortcut.
          Under transaction pooling that next transaction can land on a
          different server connection, so the query reads whatever identity
          some other request left behind. The suite must catch that.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

MODE = os.environ.get("VD_APP_MODE", "shim")

engine = create_async_engine(
    os.environ["VD_APP_DATABASE_URL"],
    pool_size=10,
    max_overflow=0,
    # PgBouncer in transaction mode cannot carry server-side prepared
    # statements across transactions, so both caches have to be off.
    connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
)
Session = async_sessionmaker(engine, expire_on_commit=False)

app = FastAPI()

SET_SESSION_IDENTITY = text(
    "SELECT set_config('app.user_id', :vd_user_id, false), "
    "set_config('app.role', :vd_role, false)"
)


async def _set_session_identity(session) -> None:
    from vibedeploy_shim.identity import current

    who = current()
    await session.execute(
        SET_SESSION_IDENTITY,
        {"vd_user_id": who.sub if who else "", "vd_role": who.role if who else ""},
    )
    # Without the commit the session-level setting would roll back with the
    # transaction and the mistake would be invisible.
    await session.commit()


@app.get("/notes")
async def notes() -> dict[str, list[str]]:
    async with Session() as session:
        if MODE == "leaky":
            await _set_session_identity(session)
        rows = await session.execute(text("SELECT id FROM notes"))
        return {"ids": [str(row[0]) for row in rows]}


if MODE == "leaky":
    from vibedeploy_shim.asgi import IdentityMiddleware

    app.add_middleware(IdentityMiddleware)
