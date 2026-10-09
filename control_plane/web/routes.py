"""The dashboard. Readme.md section 23.

Plain forms and server-rendered HTML. There is no separate JSON API for the
same actions on purpose: one surface means one set of rules about what may be
answered and when, and no second path that quietly skips them.

The deployment page is the point of the whole milestone. It is where a build
stops and says what it does not know, where the rules are shown in a builder's
own words before anything is built from them, and where a blocked deploy says
why in one sentence.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import service
from control_plane.api.deps import get_session
from control_plane.gateway_client import GatewayClient, GatewayDown
from control_plane.models import (
    AccessModel,
    Agent,
    AgentPolicy,
    AgentStatus,
    App,
    AppUser,
    Builder,
    Deployment,
    DeploymentStatus,
    GlobalSettings,
    VerificationRun,
)
from control_plane.questions import BASE_QUESTIONS

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _redirect(url: str) -> RedirectResponse:
    # 303 so that a POST turns into a GET and a refresh cannot resubmit it.
    return RedirectResponse(url, status_code=303)


async def _sole_builder(session: AsyncSession) -> Builder:
    """V0.5 has one builder per installation and no sign-up flow yet.

    Isolated here rather than spread through the handlers, so that adding real
    builder sessions later touches one function.
    """
    builder = (await session.execute(select(Builder).limit(1))).scalar_one_or_none()
    if builder is None:
        builder = await service.create_builder(
            session, email="builder@localhost", password=uuid.uuid4().hex
        )
    return builder


async def _app_or_404(session: AsyncSession, app_id: str) -> App:
    app = (
        await session.execute(select(App).where(App.app_id == app_id))
    ).scalar_one_or_none()
    if app is None:
        raise HTTPException(status_code=404, detail="no such app")
    return app


async def _deployment_or_404(session: AsyncSession, deployment_id: uuid.UUID) -> Deployment:
    deployment = await session.get(Deployment, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="no such deployment")
    return deployment


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Any:
    apps = (await session.execute(select(App).order_by(App.created_at))).scalars().all()
    return templates.TemplateResponse(request, "apps.html", {"apps": apps})


@router.post("/apps")
async def create_app(
    session: Annotated[AsyncSession, Depends(get_session)],
    name: Annotated[str, Form()],
    repo: Annotated[str, Form()],
) -> Any:
    builder = await _sole_builder(session)
    app = await service.create_app(session, builder=builder, name=name, repo=repo)
    # Committed before the redirect, because the browser follows it straight to
    # a page that has to find this row.
    await session.commit()
    return _redirect(f"/apps/{app.app_id}")


@router.get("/apps/{app_id}", response_class=HTMLResponse)
async def app_page(
    request: Request,
    app_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    app = await _app_or_404(session, app_id)
    deployments = (
        (
            await session.execute(
                select(Deployment)
                .where(Deployment.app_id == app.id)
                .order_by(Deployment.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    users = (
        (
            await session.execute(
                select(AppUser).where(AppUser.app_id == app.id).order_by(AppUser.email)
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "app.html",
        {
            "app": app,
            "deployments": deployments,
            "users": users,
            "questions": BASE_QUESTIONS,
            "answers": app.answers or {},
        },
    )


@router.post("/apps/{app_id}/deploy")
async def deploy(
    request: Request,
    app_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    commit_sha: Annotated[str, Form()],
) -> Any:
    app = await _app_or_404(session, app_id)
    form = await request.form()
    answers: dict[str, Any] = {}
    for question in BASE_QUESTIONS:
        value = form.get(question.id)
        if value is None:
            continue
        answers[question.id] = value == "yes" if question.id == "sensitive" else value

    try:
        deployment = await service.start_deployment(
            session, app=app, commit_sha=commit_sha, answers=answers
        )
    except service.ServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return _redirect(f"/deployments/{deployment.id}")


@router.get("/deployments/{deployment_id}", response_class=HTMLResponse)
async def deployment_page(
    request: Request,
    deployment_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    deployment = await _deployment_or_404(session, deployment_id)
    app = await session.get(App, deployment.app_id)
    model = (
        await session.execute(
            select(AccessModel).where(AccessModel.deployment_id == deployment.id)
        )
    ).scalar_one_or_none()
    runs = (
        (
            await session.execute(
                select(VerificationRun)
                .where(VerificationRun.deployment_id == deployment.id)
                .order_by(VerificationRun.role)
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "deployment.html",
        {
            "app": app,
            "deployment": deployment,
            "model": model,
            "runs": runs,
            "statuses": DeploymentStatus,
        },
    )


@router.post("/deployments/{deployment_id}/answers")
async def answer(
    request: Request,
    deployment_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    deployment = await _deployment_or_404(session, deployment_id)
    form = await request.form()
    # Follow-up ids are generated by the derivation (`owner.orders`,
    # `unlinked.plans`), so they are read straight off the form rather than
    # enumerated here.
    answers = {
        str(key): str(value)
        for key, value in form.items()
        if str(key) not in ("csrf", "commit_sha") and value
    }
    if not answers:
        raise HTTPException(status_code=400, detail="nothing was answered")
    try:
        await service.save_answers(session, deployment=deployment, answers=answers)
    except service.ServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # The answer and the woken job land together or not at all: a job woken
    # without its answer would ask the same question again.
    await session.commit()
    return _redirect(f"/deployments/{deployment.id}")


@router.post("/deployments/{deployment_id}/confirm")
async def confirm(
    deployment_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    deployment = await _deployment_or_404(session, deployment_id)
    builder = await _sole_builder(session)
    try:
        await service.confirm_model(
            session, deployment=deployment, confirmed_by=builder.email
        )
    except service.ServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return _redirect(f"/deployments/{deployment.id}")


@router.post("/apps/{app_id}/users")
async def add_user(
    app_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()] = "member",
) -> Any:
    app = await _app_or_404(session, app_id)
    await service.add_app_user(
        session, app=app, email=email, password=password, role=role
    )
    await session.commit()
    return _redirect(f"/apps/{app.app_id}")


# ---------------------------------------------------------------------------
# Agents. Readme.md 19.1, 19.5, section 23.
#
# These pages read and write control plane records only, so they are handled
# here. Everything about an agent *in flight* is below, through the gateway.
# ---------------------------------------------------------------------------


async def _agent_or_404(session: AsyncSession, app: App, agent_id: uuid.UUID) -> Agent:
    agent = await session.get(Agent, agent_id)
    # The app is part of the lookup, not just the URL: an agent of somebody
    # else's app is not something this page may show or revoke.
    if agent is None or agent.app_id != app.id:
        raise HTTPException(status_code=404, detail="no such agent")
    return agent


async def _people(session: AsyncSession, app: App) -> list[AppUser]:
    return list(
        (
            await session.execute(
                select(AppUser).where(AppUser.app_id == app.id).order_by(AppUser.email)
            )
        )
        .scalars()
        .all()
    )


async def _kill_switch(session: AsyncSession) -> bool:
    settings = await session.get(GlobalSettings, 1)
    return bool(settings and settings.agent_kill_switch)


async def _agents_page(
    request: Request, session: AsyncSession, app: App, **extra: Any
) -> Any:
    agents = (
        (
            await session.execute(
                select(Agent).where(Agent.app_id == app.id).order_by(Agent.created_at)
            )
        )
        .scalars()
        .all()
    )
    people = {person.id: person for person in await _people(session, app)}
    return templates.TemplateResponse(
        request,
        "agents.html",
        {
            "app": app,
            "agents": agents,
            "people": people,
            "all_people": list(people.values()),
            "kill_switch": await _kill_switch(session),
            "statuses": AgentStatus,
            **extra,
        },
    )


@router.get("/apps/{app_id}/agents", response_class=HTMLResponse)
async def agents_page(
    request: Request,
    app_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    return await _agents_page(request, session, await _app_or_404(session, app_id))


@router.post("/apps/{app_id}/agents", response_class=HTMLResponse)
async def create_agent(
    request: Request,
    app_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    name: Annotated[str, Form()],
    acts_for: Annotated[uuid.UUID, Form()],
    policy_yaml: Annotated[str, Form()],
    high_risk_ack: Annotated[bool, Form()] = False,
) -> Any:
    """19.5: the key is shown once, so this renders instead of redirecting.

    A redirect would have to carry the key in a URL, which puts it in browser
    history and in every log between here and there. The cost is that a refresh
    re-posts the form, and that is the better of the two.
    """
    app = await _app_or_404(session, app_id)
    person = await session.get(AppUser, acts_for)
    if person is None or person.app_id != app.id:
        raise HTTPException(status_code=404, detail="no such person")
    builder = await _sole_builder(session)
    try:
        agent, key = await service.create_agent(
            session,
            app=app,
            name=name,
            acts_for=person,
            created_by=builder.email,
            policy_yaml=policy_yaml,
            high_risk_ack=high_risk_ack,
        )
    except service.ServiceError as exc:
        return await _agents_page(request, session, app, refused=str(exc))
    await session.commit()
    return await _agents_page(
        request, session, app, minted={"agent": agent.name, "key": key}
    )


@router.get("/apps/{app_id}/agents/{agent_id}", response_class=HTMLResponse)
async def agent_page(
    request: Request,
    app_id: str,
    agent_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    refused: str | None = None,
) -> Any:
    app = await _app_or_404(session, app_id)
    agent = await _agent_or_404(session, app, agent_id)
    versions = (
        (
            await session.execute(
                select(AgentPolicy)
                .where(AgentPolicy.agent_id == agent.id)
                .order_by(AgentPolicy.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "agent.html",
        {
            "app": app,
            "agent": agent,
            "acts_for": await session.get(AppUser, agent.acts_for_user_id),
            # Newest first, and the editor is seeded from it: contract 7.5 says
            # every edit is a new version, so the thing being edited is a copy
            # of the latest and never the latest itself.
            "versions": versions,
            "latest": versions[0] if versions else None,
            "statuses": AgentStatus,
            "refused": refused,
        },
    )


@router.post("/apps/{app_id}/agents/{agent_id}/policy")
async def save_policy(
    app_id: str,
    agent_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    policy_yaml: Annotated[str, Form()],
    high_risk_ack: Annotated[bool, Form()] = False,
) -> Any:
    app = await _app_or_404(session, app_id)
    agent = await _agent_or_404(session, app, agent_id)
    builder = await _sole_builder(session)
    try:
        await service.save_policy(
            session,
            agent=agent,
            policy_yaml=policy_yaml,
            author=builder.email,
            high_risk_ack=high_risk_ack,
        )
    except service.ServiceError as exc:
        # The refusal is the policy loader's own sentence, and it is the point:
        # a policy that would not parse is never saved as a version.
        return _redirect(
            f"/apps/{app.app_id}/agents/{agent.id}?refused={quote(str(exc))}"
        )
    await session.commit()
    return _redirect(f"/apps/{app.app_id}/agents/{agent.id}")


@router.post("/apps/{app_id}/agents/{agent_id}/revoke")
async def revoke_agent(
    app_id: str,
    agent_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Any:
    app = await _app_or_404(session, app_id)
    agent = await _agent_or_404(session, app, agent_id)
    await service.revoke_agent(session, agent=agent)
    await session.commit()
    return _redirect(f"/apps/{app.app_id}/agents")


@router.post("/apps/{app_id}/agents-enabled")
async def set_app_agents(
    app_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    on: Annotated[bool, Form()],
) -> Any:
    app = await _app_or_404(session, app_id)
    await service.set_app_agents(session, app=app, on=on)
    await session.commit()
    return _redirect(f"/apps/{app.app_id}/agents")


@router.post("/kill-switch")
async def kill_switch(
    session: Annotated[AsyncSession, Depends(get_session)],
    on: Annotated[bool, Form()],
    back: Annotated[str, Form()] = "/",
) -> Any:
    """Every agent everywhere. 19.5."""
    await service.set_kill_switch(session, on=on)
    await session.commit()
    # Only a path, never whatever was posted: `back` comes from a form, and an
    # absolute URL there would make this an open redirect.
    return _redirect(back if back.startswith("/") else "/")


# ---------------------------------------------------------------------------
# Approvals, activity, audit, evidence. Readme.md 19.4, 19.6, 19.7, section 23.
#
# None of these are computed here. Approving an action redoes its dry run
# against the app's database as the `agent` role, and CLAUDE.md gives that
# secret to the gateway and to nothing else — so this console asks, over HTTP,
# and renders the answer.
#
# A gateway that is not answering means a page cannot be filled in. It never
# means something is permitted: `GatewayDown` is shown as a sentence and the
# page renders without its numbers.
# ---------------------------------------------------------------------------


def _gateway(request: Request) -> GatewayClient:
    return request.app.state.gateway


async def _names(session: AsyncSession, app: App) -> dict[str, str]:
    """Who the ids in the gateway's answers are.

    The gateway answers with ids because names are not its to know: the agent
    and person records are the console's. So the lookup happens here, and an id
    with no name still shows as itself rather than as a blank.
    """
    agents = (
        (await session.execute(select(Agent).where(Agent.app_id == app.id)))
        .scalars()
        .all()
    )
    return {
        **{str(person.id): person.email for person in await _people(session, app)},
        **{str(agent.id): agent.name for agent in agents},
    }


@router.get("/apps/{app_id}/approvals", response_class=HTMLResponse)
async def approvals_page(
    request: Request,
    app_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    said: str | None = None,
) -> Any:
    app = await _app_or_404(session, app_id)
    waiting: list[dict[str, Any]] = []
    unavailable = None
    try:
        waiting = await _gateway(request).pending(app.id)
    except GatewayDown as exc:
        unavailable = str(exc)
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "app": app,
            "waiting": waiting,
            "unavailable": unavailable,
            "said": said,
            "names": await _names(session, app),
            # 19.4: the approver must be an admin of this app. The gateway
            # checks that again; this list only saves a person typing.
            "admins": [p for p in await _people(session, app) if p.role == "admin"],
        },
    )


@router.post("/apps/{app_id}/actions/{action_id}/{verdict}")
async def decide(
    request: Request,
    app_id: str,
    action_id: str,
    verdict: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    approver: Annotated[str, Form()],
    back: Annotated[str, Form()] = "",
) -> Any:
    """One click, one action. 19.4 has no bulk approve and neither does this.

    Whether the verdict is allowed is not decided here. The gateway re-runs the
    dry run, compares the `diff_hash`, checks the approver is an app admin and
    checks the switches are still off — on the same code path an agent's own
    call takes.
    """
    app = await _app_or_404(session, app_id)
    try:
        answer = await _gateway(request).decide(
            action_id, verdict=verdict, approver=approver
        )
        said = answer.get("reason") or answer.get("status") or ""
    except GatewayDown as exc:
        said = str(exc)
    where = back if back.startswith("/") else f"/apps/{app.app_id}/approvals"
    return _redirect(f"{where}?said={quote(said)}")


@router.get("/apps/{app_id}/activity", response_class=HTMLResponse)
async def activity_page(
    request: Request,
    app_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    said: str | None = None,
) -> Any:
    app = await _app_or_404(session, app_id)
    rows: list[dict[str, Any]] = []
    unavailable = None
    try:
        rows = await _gateway(request).activity(app.id)
    except GatewayDown as exc:
        unavailable = str(exc)
    return templates.TemplateResponse(
        request,
        "activity.html",
        {
            "app": app,
            "rows": rows,
            "unavailable": unavailable,
            "said": said,
            "names": await _names(session, app),
            "admins": [p for p in await _people(session, app) if p.role == "admin"],
        },
    )


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request) -> Any:
    chain = None
    unavailable = None
    try:
        chain = await _gateway(request).audit()
    except GatewayDown as exc:
        unavailable = str(exc)
    return templates.TemplateResponse(
        request, "audit.html", {"chain": chain, "unavailable": unavailable}
    )


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="that is not a date") from exc


@router.get("/apps/{app_id}/evidence", response_class=HTMLResponse)
async def evidence_page(
    request: Request,
    app_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    since: str | None = None,
    until: str | None = None,
) -> Any:
    app = await _app_or_404(session, app_id)
    report = None
    unavailable = None
    try:
        report = await _gateway(request).evidence(
            app.id, since=_when(since), until=_when(until)
        )
    except GatewayDown as exc:
        unavailable = str(exc)
    return templates.TemplateResponse(
        request,
        "evidence.html",
        {
            "app": app,
            "report": report,
            "unavailable": unavailable,
            "since": since or "",
            "until": until or "",
        },
    )


@router.get("/apps/{app_id}/evidence.json")
async def evidence_json(
    request: Request,
    app_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    since: str | None = None,
    until: str | None = None,
) -> Any:
    """19.7, downloadable. The same report the page shows, as a file.

    Not re-shaped on the way out: what an auditor downloads is what the gateway
    computed from `audit_log`, so the page and the file cannot disagree.
    """
    app = await _app_or_404(session, app_id)
    try:
        report = await _gateway(request).evidence(
            app.id, since=_when(since), until=_when(until)
        )
    except GatewayDown as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse(
        report,
        headers={
            "content-disposition": (
                f'attachment; filename="evidence-{app.app_id}.json"'
            )
        },
    )
