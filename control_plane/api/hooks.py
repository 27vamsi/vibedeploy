"""The one endpoint a stranger can reach. Readme.md section 15.

A push arrives here, and by the time it leaves either a deployment is queued or
there is a row saying why not. The order of the three checks is the whole of the
security of it:

  1. the signature, over the raw bytes, before anything else is read;
  2. the delivery id, because without one a retry cannot be told from a second
     push and the retry is the normal case;
  3. the insert of the delivery row, which is what makes handling it happen at
     most once.

Only then is the payload acted on. Everything after step 1 is already known to
have come from GitHub, and everything after step 3 is known to be the first time
we have seen it.

Two kinds of "no" come out of here and they are deliberately different. A
request we cannot attribute to GitHub gets a 401 and leaves no trace, because a
row per unsigned request would let anybody fill this table. A delivery that is
genuinely GitHub's but asks for nothing we do — a tag, a `ping`, a branch
nobody deploys — gets a 200, because GitHub retries anything that is not a 2xx
and being retried for ever over something we chose to skip is not a refusal, it
is a loop.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import service, webhooks
from control_plane.api.deps import get_config, get_session
from control_plane.config import ControlPlaneConfig
from control_plane.models import App, WebhookDelivery

router = APIRouter(prefix="/hooks", tags=["hooks"])


@router.post("/github")
async def github(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    config: Annotated[ControlPlaneConfig, Depends(get_config)],
) -> dict[str, Any]:
    body = await request.body()
    if not webhooks.verify(
        config.github_webhook_secret, body, request.headers.get(webhooks.SIGNATURE_HEADER)
    ):
        raise HTTPException(status_code=401, detail="no")

    delivery_id = (request.headers.get(webhooks.DELIVERY_HEADER) or "").strip()
    if not delivery_id:
        raise HTTPException(status_code=400, detail=webhooks.NO_DELIVERY_ID)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=webhooks.NOT_JSON) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=webhooks.NOT_JSON)

    event = (request.headers.get(webhooks.EVENT_HEADER) or "").strip()
    delivery = WebhookDelivery(
        source=webhooks.GITHUB, delivery_id=delivery_id, event=event
    )
    session.add(delivery)
    try:
        await session.flush()
    except IntegrityError:
        # Already in the table, so it has already been acted on. Said out loud
        # rather than swallowed: "already handled" and "ignored" are different
        # answers and GitHub's delivery log is where a builder reads them.
        await session.rollback()
        return {"ok": True, "outcome": webhooks.ALREADY_HANDLED}

    push = webhooks.parse_push(payload)
    delivery.ref = push.branch
    delivery.head_sha = push.head
    outcome, detail, started = await _act(session, event, push)
    delivery.outcome = outcome
    delivery.detail = detail
    delivery.deployments = [str(one) for one in started]
    # Committed here rather than on the way out of the request, because the
    # worker may claim the job this created before the response is even read.
    await session.commit()
    return {
        "ok": True,
        "outcome": outcome,
        "detail": detail,
        "deployments": [str(one) for one in started],
    }


async def _act(
    session: AsyncSession, event: str, push: webhooks.Push
) -> tuple[str, str | None, list[uuid.UUID]]:
    """Decide what this delivery deserves, and do it. Never raises.

    Returning the outcome rather than raising is what keeps the recording and
    the acting in one transaction: a refusal is a row, not a lost request.
    """
    if event != webhooks.PUSH:
        return webhooks.NOT_A_PUSH, f"{event or 'that event'} is not a push.", []
    if push.deleted:
        return webhooks.BRANCH_DELETED, "That push deleted a branch.", []
    if push.branch is None:
        return webhooks.NOT_A_BRANCH, "That push was not to a branch.", []

    apps = await _apps_for(session, push)
    if not apps:
        return webhooks.NO_SUCH_APP, "No app here is built from that repository.", []

    tracking = [app for app in apps if app.tracked_ref == push.branch]
    if not tracking:
        wanted = ", ".join(sorted({app.tracked_ref for app in apps}))
        return (
            webhooks.NOT_TRACKED,
            f"That push was to {push.branch}, and these apps deploy {wanted}.",
            [],
        )

    started: list[uuid.UUID] = []
    refusals: list[str] = []
    for app in tracking:
        try:
            deployment = await service.start_deployment(
                session, app=app, ref=push.branch
            )
        except service.ServiceError as exc:
            # Section 3 rule 5: a push is not permission to guess at the
            # answers. The sentence is the service's own, because it is the one
            # that names what is missing.
            refusals.append(str(exc))
        else:
            started.append(deployment.id)

    if started:
        return webhooks.DEPLOYED, " ".join(refusals) or None, started
    return webhooks.REFUSED, " ".join(refusals), []


async def _apps_for(session: AsyncSession, push: webhooks.Push) -> list[App]:
    """Every app built from the repository this push was about.

    Compared in Python rather than in SQL because the comparison is between two
    normalised names, not two strings: an app's `repo` is whatever a builder
    pasted, and the same repository arrives from GitHub in four more spellings.
    Storing the normalised form alongside would make this a `WHERE`, and is not
    worth a column while an installation has tens of apps rather than millions.
    """
    rows = (await session.execute(select(App))).scalars().all()
    return [app for app in rows if webhooks.repo_key(app.repo) in push.repos]
