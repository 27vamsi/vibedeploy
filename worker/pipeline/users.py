"""Adding a person to a deployed app. Readme.md section 16.

"Adding a user inserts a row into the app's `users` table, as migrator." The
control plane cannot do that: it holds no migrator credentials, by design. So it
creates the person **disabled** and leaves this job behind, and until the row
exists in the app's own database the login endpoint fails closed rather than
handing out a session that maps to nothing.

The key the database gives that row is what the sidecar will put in
`app.user_id`, so it is read back from the INSERT rather than invented here.
Inventing it would mean the control plane and the database disagreeing about who
somebody is, which is the one disagreement none of the policies could survive.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from sqlalchemy import select

from control_plane import secrets as secret_store
from control_plane.config import ControlPlaneConfig
from control_plane.models import AccessModel, App, AppUser, Job
from control_plane.secrets import SecretStore
from kernel.render import quote_ident
from worker.pipeline import DONE, Sessions, Verdict

NOT_LIVE = (
    "This app is not live yet, so there is no database to add a person to."
)
NO_MODEL = "This app has no confirmed access model, so nobody can be added to it."

_COLUMNS = """
SELECT column_name FROM information_schema.columns
WHERE table_schema = $1 AND table_name = $2
"""


async def handle(
    config: ControlPlaneConfig, sessions: Sessions, job_id: uuid.UUID
) -> Verdict:
    async with sessions() as session:
        job = await session.get(Job, job_id)
        user = await session.get(AppUser, uuid.UUID(job.payload["app_user_id"]))
        app = await session.get(App, user.app_id)
        if app.live_deployment_id is None:
            return Verdict("failed", error=NOT_LIVE)
        model = (
            await session.execute(
                select(AccessModel).where(
                    AccessModel.deployment_id == app.live_deployment_id
                )
            )
        ).scalar_one_or_none()
        if model is None:
            return Verdict("failed", error=NO_MODEL)
        principal = model.model_json["principal"]
        app_id, email, user_id = app.app_id, user.email, user.id

    store = SecretStore(config.state_root / "secrets")
    migrator = store.get(secret_store.secret_name(app_id, secret_store.MIGRATOR))

    conn = await asyncpg.connect(migrator["dsn"])
    try:
        key = await _insert(conn, app_id, principal, email)
    except asyncpg.PostgresError as exc:
        return Verdict("failed", error=f"could not add {email}: {exc}")
    finally:
        await conn.close()

    async with sessions() as session, session.begin():
        stored = await session.get(AppUser, user_id)
        stored.principal_key = str(key)
        stored.disabled = False
    return DONE


async def _insert(
    conn: asyncpg.Connection, schema: str, principal: dict[str, Any], email: str
) -> Any:
    table, key = principal["table"], principal["key"]
    columns = {
        row["column_name"]
        for row in await conn.fetch(_COLUMNS, schema, table)
    }
    target = f"{quote_ident(schema)}.{quote_ident(table)}"
    returning = quote_ident(key)

    # Only columns we can honestly fill. Everything else is left to the app's
    # own defaults: guessing at a column we know nothing about would be putting
    # made-up data in somebody's production database.
    values: dict[str, Any] = {}
    if "email" in columns:
        values["email"] = email
    if "name" in columns:
        values["name"] = email.split("@", 1)[0]

    if not values:
        return await conn.fetchval(
            f"INSERT INTO {target} DEFAULT VALUES RETURNING {returning}"
        )
    names = sorted(values)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(names)))
    return await conn.fetchval(
        f"INSERT INTO {target} ({', '.join(quote_ident(n) for n in names)})"
        f" VALUES ({placeholders}) RETURNING {returning}",
        *(values[n] for n in names),
    )
