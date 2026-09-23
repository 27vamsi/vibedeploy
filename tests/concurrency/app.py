"""A deliberately sloppy app. This is the thing under test, not a helper.

It does everything wrong on purpose:
  - the endpoint selects from `notes` with no WHERE clause at all
  - it never imports the shim, never mentions identity, never filters anything

If a user still only sees their own rows, that is the database doing it. That is
the entire claim of the product.

Set VD_TEST_LEAK=1 to get the broken variant used as the negative control: it
sets identity at *session* scope and commits, which is the classic mistake that
a transaction-mode pooler turns into a cross-user data leak.
"""

from __future__ import annotations

import base64
import json
import os

from fastapi import FastAPI, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DSN = os.environ["VD_TEST_DSN"]
LEAK = os.environ.get("VD_TEST_LEAK") == "1"

engine = create_async_engine(
    DSN,
    # Both are required behind PgBouncer in transaction mode: asyncpg would
    # otherwise prepare statements on a server connection it does not keep.
    connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
    pool_size=10,
    max_overflow=10,
)

app = FastAPI()

UNFILTERED = text("SELECT id FROM notes")


@app.get("/health")
async def health():
    return {"ok": True}


def _unverified_sub(request: Request) -> str:
    """Only the leaky variant does this. A real app never parses the header."""
    raw = request.headers.get("x-vd-identity", "")
    part = raw.partition(".")[0]
    if not part:
        return ""
    padded = part + "=" * (-len(part) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded)).get("sub", "")
    except Exception:
        return ""


@app.get("/notes")
async def notes(request: Request):
    if LEAK:
        # Session scope plus a commit: the setting outlives the transaction and
        # rides along with whichever pooled server connection happens to carry
        # it next.
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.user_id', :u, false)"),
                {"u": _unverified_sub(request)},
            )
            await conn.commit()

    async with engine.begin() as conn:
        rows = (await conn.execute(UNFILTERED)).fetchall()

    return {"ids": [str(r[0]) for r in rows]}
