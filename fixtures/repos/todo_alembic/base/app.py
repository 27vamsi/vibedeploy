"""The same unprotected todo app as `../../todo/base/app.py`.

`SELECT ... FROM todos` with no WHERE clause, no login, no session, no user
object. It is here so that an Alembic app can be deployed all the way to live
and separated person by person, not just migrated.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

engine = create_async_engine(
    os.environ["VD_DATABASE_URL"],
    connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
    pool_size=5,
    max_overflow=5,
)

app = FastAPI()

# No filter. On purpose.
ALL_TODOS = text("SELECT id, title, done FROM todos ORDER BY title")
WHOAMI = text("SELECT vd_user_id() AS sub")


async def _rows(statement):
    async with engine.begin() as conn:
        return [dict(row) for row in (await conn.execute(statement)).mappings()]


@app.get("/todos")
async def todos():
    rows = await _rows(ALL_TODOS)
    return {"todos": [{**row, "id": str(row["id"])} for row in rows]}


@app.get("/whoami")
async def whoami():
    rows = await _rows(WHOAMI)
    return {"sub": str(rows[0]["sub"]) if rows[0]["sub"] else None}


@app.get("/", response_class=HTMLResponse)
async def index():
    rows = await _rows(ALL_TODOS)
    who = await _rows(WHOAMI)
    items = "".join(f"<li>{row['title']}</li>" for row in rows)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Todos</title>"
        f"</head><body><p>Signed in as {who[0]['sub'] or 'nobody'}</p>"
        f"<p>{len(rows)} todo(s)</p><ul>{items}</ul>"
        "<form method='post' action='/__vd/logout'>"
        "<button type='submit'>Sign out</button></form></body></html>"
    )
