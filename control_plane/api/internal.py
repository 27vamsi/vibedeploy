"""Endpoints our own components call. Never reachable from the public internet.

Two callers, two very different levels of trust:

  - the **sidecar** asks who a person is. It holds a per-app API key and is
    told only `sub`, `role` and `session_version`. It never sees a hash.
  - the **build job** reports a verdict. It holds one token, for one
    deployment, for one phase, and the token dies the moment it is used
    (contract 7.4).

Neither is allowed to learn anything from a refusal beyond "no". A wrong key, a
wrong app, an unknown user and a wrong password all come back the same.
"""

from __future__ import annotations

import hmac
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane import tokens
from control_plane.api.deps import get_config, get_session
from control_plane.config import ControlPlaneConfig
from control_plane.models import (
    AccessModel,
    App,
    AppUser,
    BuildPhase,
    Deployment,
    DeploymentStatus,
    VerificationRun,
)
from control_plane.passwords import hash_password, verify_password

router = APIRouter(prefix="/internal", tags=["internal"])

# Compared against a wrong password so that an unknown email costs the same as
# a known one. Without it, response time tells an attacker which of the two it
# was.
_DUMMY_HASH = hash_password("a password nobody has")


def _no() -> HTTPException:
    return HTTPException(status_code=401, detail="no")


async def _app_or_404(session: AsyncSession, app_id: str) -> App:
    app = (
        await session.execute(select(App).where(App.app_id == app_id))
    ).scalar_one_or_none()
    if app is None:
        raise _no()
    return app


def _check_key(config: ControlPlaneConfig, presented: str | None) -> None:
    if not presented or not hmac.compare_digest(presented, config.sidecar_api_key):
        raise HTTPException(status_code=403, detail="no")


# ---------------------------------------------------------------------------
# The login gate's two questions. Readme.md section 16.
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str


class Principal(BaseModel):
    sub: str
    role: str
    session_version: int


@router.post("/apps/{app_id}/login", response_model=Principal)
async def login(
    app_id: str,
    body: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    config: Annotated[ControlPlaneConfig, Depends(get_config)],
    x_vd_sidecar_key: Annotated[str | None, Header()] = None,
) -> Principal:
    _check_key(config, x_vd_sidecar_key)
    app = await _app_or_404(session, app_id)

    user = (
        await session.execute(
            select(AppUser).where(
                AppUser.app_id == app.id, AppUser.email == body.email.strip().lower()
            )
        )
    ).scalar_one_or_none()

    if user is None:
        verify_password(_DUMMY_HASH, body.password)
        raise _no()
    if user.disabled or not verify_password(user.password_hash, body.password):
        raise _no()

    return Principal(
        sub=user.principal_key,
        role=user.role,
        session_version=user.session_version,
    )


class SessionVersion(BaseModel):
    session_version: int


@router.get("/apps/{app_id}/users/{sub}/session-version", response_model=SessionVersion)
async def session_version(
    app_id: str,
    sub: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    config: Annotated[ControlPlaneConfig, Depends(get_config)],
    x_vd_sidecar_key: Annotated[str | None, Header()] = None,
) -> SessionVersion:
    _check_key(config, x_vd_sidecar_key)
    app = await _app_or_404(session, app_id)
    user = (
        await session.execute(
            select(AppUser).where(
                AppUser.app_id == app.id, AppUser.principal_key == sub
            )
        )
    ).scalar_one_or_none()
    # A removed user has no version, and a session we cannot confirm is not a
    # session. The sidecar turns this into a logout within 60 seconds.
    if user is None or user.disabled:
        raise HTTPException(status_code=404, detail="no such user")
    return SessionVersion(session_version=user.session_version)


# ---------------------------------------------------------------------------
# The build job's verdict. Contract 7.4.
# ---------------------------------------------------------------------------


class Verification(BaseModel):
    total: int
    passed: int
    failures: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def failed(self) -> int:
        return len(self.failures)


class BuildResult(BaseModel):
    """Both phases in one shape. `verify` is contract 7.4; `plan` is the pause
    that has to exist before there is anything to verify."""

    deployment_id: uuid.UUID
    phase: Literal["plan", "verify"]
    status: Literal["needs_answers", "derived", "passed", "blocked", "error"]
    blocked_reason: str | None = None
    schema_hash: str | None = None
    access_model: dict[str, Any] | None = None
    questions: list[dict[str, Any]] = Field(default_factory=list)
    refusals: list[dict[str, Any]] = Field(default_factory=list)
    explanation: list[str] = Field(default_factory=list)
    verification: Verification | None = None
    runs: dict[str, Verification] = Field(default_factory=dict)
    image_digest: str | None = None
    shim: dict[str, Any] | None = None


@router.post("/builds/{deployment_id}/result", status_code=204)
async def build_result(
    deployment_id: uuid.UUID,
    body: BuildResult,
    session: Annotated[AsyncSession, Depends(get_session)],
    x_vd_build_token: Annotated[str | None, Header()] = None,
) -> Response:
    if not x_vd_build_token or body.deployment_id != deployment_id:
        raise HTTPException(status_code=403, detail="no")

    phase = await tokens.spend(
        session, deployment_id=deployment_id, token=x_vd_build_token
    )
    # Unknown, expired, already spent, or for a different deployment: all one
    # answer, so a replay learns nothing about which it was.
    if phase is None or phase.value != body.phase:
        raise HTTPException(status_code=403, detail="no")

    deployment = (
        await session.execute(
            select(Deployment).where(Deployment.id == deployment_id)
        )
    ).scalar_one_or_none()
    if deployment is None:
        raise HTTPException(status_code=403, detail="no")

    if phase is BuildPhase.plan:
        _record_plan(deployment, body, session)
    else:
        await _record_verify(session, deployment, body)

    deployment.updated_at = datetime.now(timezone.utc)
    # The build job treats this 204 as "the verdict is recorded" and exits, and
    # the worker reads the verdict back the moment it does. So the verdict is
    # durable before we say anything, not after.
    await session.commit()
    return Response(status_code=204)


def _record_plan(
    deployment: Deployment, body: BuildResult, session: AsyncSession
) -> None:
    deployment.schema_hash = body.schema_hash
    deployment.questions = body.questions
    deployment.explanation = body.explanation

    if body.status == "needs_answers":
        deployment.status = DeploymentStatus.awaiting_answers.value
        return
    if body.status == "derived" and body.access_model is not None:
        # Saved unconfirmed. Section 11 step 6: a model becomes something we
        # will build only once a person has said yes to it in plain English.
        session.add(
            AccessModel(
                app_id=deployment.app_id,
                deployment_id=deployment.id,
                model_json=body.access_model,
            )
        )
        deployment.status = DeploymentStatus.awaiting_confirmation.value
        return

    deployment.status = (
        DeploymentStatus.error.value
        if body.status == "error"
        else DeploymentStatus.blocked.value
    )
    deployment.blocked_reason = body.blocked_reason or _refusal_text(body)


def _refusal_text(body: BuildResult) -> str:
    reasons = [r.get("reason", "") for r in body.refusals if r.get("reason")]
    return " ".join(reasons) or "The build job gave no reason, so nothing was deployed."


async def _record_verify(
    session: AsyncSession, deployment: Deployment, body: BuildResult
) -> None:
    deployment.schema_hash = body.schema_hash or deployment.schema_hash
    deployment.image_digest = body.image_digest

    # Section 3 rule 8: the results are saved whatever the verdict. A blocked
    # deploy is exactly when a builder needs to read them.
    for role, run in body.runs.items():
        session.add(
            VerificationRun(
                deployment_id=deployment.id,
                role=role,
                total=run.total,
                passed=run.passed,
                failed=run.failed,
                report_json=run.model_dump(),
            )
        )

    if body.status == "passed":
        # Proved, not live. `deploying` is what the worker reads to know it may
        # now touch the real database: `verifying` still means "confirmed, go
        # and prove it", and a deployment that meant both would be re-verified
        # forever.
        deployment.status = DeploymentStatus.deploying.value
        deployment.blocked_reason = None
        return

    deployment.status = (
        DeploymentStatus.error.value
        if body.status == "error"
        else DeploymentStatus.blocked.value
    )
    deployment.blocked_reason = (
        body.blocked_reason
        or "The attack suite failed but named no reason, so nothing was deployed."
    )
