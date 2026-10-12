"""Removing an app for good.

The console gained this for one reason: strangers can sign up and push their
own repositories, so the administrator needs a way to take one away that
actually takes it away. "Delete the row" would not have been that — it would
have left two schemas, four Postgres roles, a set of encrypted secrets and a
pair of running containers belonging to an app nobody can see any more.

Five things, in this order, and the order is the whole design:

1. **Stop serving it.** `reap(keep=set())` stops every generation. First,
   because everything below pulls the ground out from under a running process,
   and a container still answering requests against a database whose roles have
   just been dropped is a 500 loop rather than a deleted app.
2. **Drop the schemas and the roles.** `kernel.provision.drop_app_roles` takes
   both schemas (`app_<id>` and `app_<id>_vd`) and all four roles with
   `DROP OWNED BY ... CASCADE`. This is the irreversible step and it is why the
   console makes a person type the app id.
3. **Delete the secrets.** By directory, not by the four names this code knows:
   a builder's own settings are stored alongside them under names they chose.
4. **Delete the row**, which cascades to deployments, access models,
   verification runs, people, agents, policies and the app's other jobs.
5. Let the reconciler notice. The route table is restated from the database
   every pass, so a deleted app's hostname stops resolving without this code
   having to know anything about the proxy.

Every step is written to survive being run twice, because a job that is
reclaimed after its worker died has to be able to start again from the top.
`DROP ... IF EXISTS`, a reap of nothing, a secret directory that is not there
and a row that has already gone are all successes here, not errors.

What is deliberately *not* deleted is `audit_log`. The agent gateway's chain is
append-only and `audit_log.action_id` carries no foreign key, so dropping an
app leaves the record of what its agents did intact. Deleting an app is a thing
an administrator does; it is not a way to erase the evidence of one.
"""

from __future__ import annotations

import uuid

import asyncpg
from sqlalchemy import delete

from control_plane import secrets as secret_store
from control_plane.config import ControlPlaneConfig
from control_plane.models import App, Job
from kernel.provision import AppRoles, drop_app_roles
from worker.pipeline import DONE, Sessions, Verdict
from worker.runtimes import serving

NO_APP = "That job does not say which app to remove, so nothing was removed."


async def handle(
    config: ControlPlaneConfig, sessions: Sessions, job_id: uuid.UUID
) -> Verdict:
    async with sessions() as session:
        job = await session.get(Job, job_id)
        app_id = (job.payload or {}).get("app_id") if job else None
    if not app_id:
        return Verdict("failed", error=NO_APP)

    # 1. Nothing of this app is left running. `keep=set()` rather than a
    #    generation number: there is no version of a deleted app worth keeping.
    await serving.reap(config, app_id, keep=set())

    # 2. The irreversible half. As the admin role, because dropping a role
    #    cannot be done by that role.
    conn = await asyncpg.connect(config.app_admin_dsn)
    try:
        await drop_app_roles(conn, AppRoles.generate(app_id))
    except asyncpg.PostgresError as exc:
        # Left queued-and-failed with the app row still present, on purpose: a
        # half-dropped app that still has a row is something an administrator
        # can see and retry. Deleting the row first would have made it
        # invisible and left the schemas behind forever.
        return Verdict("failed", error=f"could not drop {app_id}: {exc}")
    finally:
        await conn.close()

    # 3. Credentials for roles that no longer exist.
    secret_store.store_for(config).forget(app_id)

    # 4. The row, and everything that hangs off it.
    async with sessions() as session, session.begin():
        await session.execute(delete(App).where(App.app_id == app_id))
    return DONE
