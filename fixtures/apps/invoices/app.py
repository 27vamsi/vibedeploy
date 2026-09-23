"""The app a customer would have vibe-coded. Readme.md section 8 M6.

This is the thing being demonstrated, not a helper, and it is deliberately
naive:

  - `/invoices` runs `SELECT ... FROM invoices` with **no WHERE clause**.
  - There is no login code, no session, no user object, no tenant id, no
    authorisation of any kind anywhere in this file.
  - It never imports the shim and never looks at a header.

Alice and Bob nonetheless see different pages. Everything that makes that true
lives outside this file: the sidecar decides who the request is for, the shim
puts that on the connection, and Postgres row level security does the filtering.

Run behind the sidecar on `127.0.0.1:3000`, never exposed directly.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

engine = create_async_engine(
    os.environ["VD_DATABASE_URL"],
    # Required behind PgBouncer in transaction mode.
    connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
    pool_size=5,
    max_overflow=5,
)

app = FastAPI()

# No filter. On purpose. This is the line the whole product is about.
ALL_INVOICES = text(
    "SELECT i.id, i.amount, i.status, c.name AS customer"
    " FROM invoices i"
    " JOIN orders o ON o.id = i.order_id"
    " JOIN customers c ON c.id = o.customer_id"
    " ORDER BY i.id"
)

ALL_CUSTOMERS = text("SELECT id, name FROM customers ORDER BY id")

WHOAMI = text("SELECT vd_user_id() AS sub, vd_role() AS role")


@app.get("/health")
async def health():
    return {"ok": True}


async def _rows(statement):
    async with engine.begin() as conn:
        return [dict(r) for r in (await conn.execute(statement)).mappings()]


@app.get("/invoices")
async def invoices():
    rows = await _rows(ALL_INVOICES)
    return {"invoices": [{**r, "id": str(r["id"]), "amount": str(r["amount"])}
                         for r in rows]}


@app.get("/customers")
async def customers():
    rows = await _rows(ALL_CUSTOMERS)
    return {"customers": [{**r, "id": str(r["id"])} for r in rows]}


@app.get("/whoami")
async def whoami():
    """What the database thinks this request is. Handy in a browser; note the
    app still does not use it for anything."""
    rows = await _rows(WHOAMI)
    return {"sub": str(rows[0]["sub"]) if rows[0]["sub"] else None,
            "role": rows[0]["role"]}


@app.get("/", response_class=HTMLResponse)
async def index():
    rows = await _rows(ALL_INVOICES)
    who = await _rows(WHOAMI)
    items = "".join(
        f"<tr><td>{r['customer']}</td><td>{r['amount']}</td>"
        f"<td>{r['status']}</td></tr>"
        for r in rows
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Invoices</title></head><body>"
        f"<p>Signed in as {who[0]['sub'] or 'nobody'}</p>"
        f"<p>{len(rows)} invoice(s)</p>"
        "<table border='1'><tr><th>Customer</th><th>Amount</th>"
        f"<th>Status</th></tr>{items}</table>"
        "<form method='post' action='/__vd/logout'>"
        "<button type='submit'>Sign out</button></form>"
        "</body></html>"
    )
