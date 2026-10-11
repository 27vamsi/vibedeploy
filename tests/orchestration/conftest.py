"""The queue, on its own, with the worst timing we can arrange.

Deliberately much lighter than `tests/pipeline/conftest.py`: no uvicorn, no
Docker, no app schemas, no provisioning. `Job.app_id` and `Job.deployment_id`
are both nullable, so these tests enqueue bare jobs and the whole area runs in
about a second. What is being tested here is the queue's own behaviour under
crash and takeover, and nothing about a real deployment makes that sharper.

`lease_expires_at` is pushed into the past rather than waiting for a real lease
to run out. That is not a shortcut around the mechanism: a lease in the past is
exactly the state a worker that was killed leaves behind, and it is the state
the queue has to recover from.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest_asyncio
from sqlalchemy import select, text, update

from control_plane import jobs, migrate
from control_plane.config import DEFAULT_APP_DSN, ControlPlaneConfig
from control_plane.db import make_engine, make_sessionmaker
from control_plane.models import (
    SCHEMA,
    App,
    Builder,
    Deployment,
    Job,
    WebhookDelivery,
)

# The same database the pipeline tests use, because it is the one that exists
# and is safe to empty. Only `jobs` is ever truncated here.
CONTROL_DB = "vibedeploy_pipeline_test"
CONTROL_URL = (
    "postgresql+asyncpg://vd_admin:vd_local_password@127.0.0.1:55432/" + CONTROL_DB
)

# The four questions of section 10, already answered. An app that has not
# answered them cannot deploy at all, so most tests here need them out of the
# way — and one test needs them missing, which is why this is a value and not a
# default buried in a helper.
ANSWERED = {
    "audience": "me",
    "size": "small",
    "visibility": "own_data",
    "sensitive": False,
}


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Queue:
    config: ControlPlaneConfig
    sessions: Any

    async def enqueue(self, kind: str = jobs.KIND_DEPLOY) -> uuid.UUID:
        async with self.sessions() as session, session.begin():
            job = await jobs.enqueue(session, kind=kind)
            return job.id

    async def enqueue_for_a_deployment(self) -> tuple[uuid.UUID, uuid.UUID]:
        """A job attached to a real deployment, which is what `wake` matches on.

        Three rows of paperwork rather than `service.start_deployment`, because
        nothing here deploys anything and nothing here is provisioned. The only
        thing that matters is that a `deployment_id` exists to key `wake` on.
        """
        suffix = uuid.uuid4().hex[:8]
        async with self.sessions() as session, session.begin():
            builder = Builder(email=f"{suffix}@example.test", password_hash="x")
            session.add(builder)
            await session.flush()
            app = App(
                builder_id=builder.id,
                app_id=f"app_{suffix}",
                name=suffix,
                subdomain=suffix,
                repo="/nowhere",
            )
            session.add(app)
            await session.flush()
            deployment = Deployment(app_id=app.id, ref="main")
            session.add(deployment)
            await session.flush()
            job = await jobs.enqueue(
                session, app_id=app.id, deployment_id=deployment.id
            )
            return job.id, deployment.id

    async def make_app(
        self,
        *,
        repo: str,
        answers: Any = ANSWERED,
        tracked_ref: str = "main",
    ) -> App:
        """An app a push could plausibly arrive for, and nothing more.

        No provisioning, no schema, no roles: a webhook's whole job is to turn a
        delivery into a queued deployment, and that is decided entirely from
        rows in this database.
        """
        suffix = uuid.uuid4().hex[:8]
        async with self.sessions() as session, session.begin():
            builder = Builder(email=f"{suffix}@example.test", password_hash="x")
            session.add(builder)
            await session.flush()
            app = App(
                builder_id=builder.id,
                app_id=f"app_{suffix}",
                name=suffix,
                subdomain=suffix,
                repo=repo,
                tracked_ref=tracked_ref,
                answers=answers,
            )
            session.add(app)
            await session.flush()
            return app

    async def deployments_of(self, app_pk: uuid.UUID) -> list[Deployment]:
        async with self.sessions() as session:
            rows = await session.execute(
                select(Deployment)
                .where(Deployment.app_id == app_pk)
                .order_by(Deployment.created_at)
            )
            return list(rows.scalars())

    async def jobs_of(self, app_pk: uuid.UUID) -> list[Job]:
        async with self.sessions() as session:
            rows = await session.execute(select(Job).where(Job.app_id == app_pk))
            return list(rows.scalars())

    async def deliveries(self) -> list[WebhookDelivery]:
        async with self.sessions() as session:
            rows = await session.execute(
                select(WebhookDelivery).order_by(WebhookDelivery.created_at)
            )
            return list(rows.scalars())

    async def claim(self, worker: str) -> Job | None:
        async with self.sessions() as session, session.begin():
            return await jobs.claim(session, worker)

    async def read(self, job_id: uuid.UUID) -> Job:
        async with self.sessions() as session:
            job = await session.get(Job, job_id)
            assert job is not None
            return job

    async def expire(self, job_id: uuid.UUID) -> None:
        """What a worker that was killed leaves behind: a lease nobody renews."""
        async with self.sessions() as session, session.begin():
            await session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(lease_expires_at=now() - timedelta(seconds=1))
            )


@pytest_asyncio.fixture(scope="session")
async def _queue_engine(tmp_path_factory):
    config = ControlPlaneConfig(
        database_url=CONTROL_URL,
        app_admin_dsn=DEFAULT_APP_DSN,
        sidecar_api_key="unused-here",
        session_secret="orchestration-session-secret-not-a-default",
        state_root=tmp_path_factory.mktemp("orchestration"),
        public_url="http://127.0.0.1:0",
    )
    await migrate.prepare(config)
    engine = make_engine(config)
    try:
        yield config, engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def queue(_queue_engine):
    config, engine = _queue_engine
    async with engine.begin() as conn:
        # `builders` cascades to apps, deployments and their jobs; `jobs` is
        # named as well because a bare job here belongs to neither, and so is
        # `webhook_deliveries`, because a delivery that matched no app belongs
        # to nothing at all.
        await conn.execute(
            text(
                f'TRUNCATE "{SCHEMA}".builders, "{SCHEMA}".jobs,'
                f' "{SCHEMA}".webhook_deliveries CASCADE'
            )
        )
    return Queue(config=config, sessions=make_sessionmaker(engine))
