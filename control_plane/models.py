"""The control plane's tables. Readme.md section 22.

Section 22's last line is the rule that shapes all of this: **no secret values
in the control plane database.** Role passwords, identity keys and session keys
live in the secret store; what is kept here is the name to look them up by. An
agent key is the same: only its argon2 hash is here, and it is shown to a
builder exactly once.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "vd_control"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Everything lives in its own schema, so `public` stays empty and a grant
    made by mistake has nothing to land on."""


Base.metadata.schema = SCHEMA


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ---------------------------------------------------------------------------
# Status vocabularies. Strings in the database, enums in code, checked in both.
# ---------------------------------------------------------------------------


class DeploymentStatus(str, enum.Enum):
    """The life of one attempt to put a commit live.

    `blocked` is the one that matters: it is the terminal state for anything
    that failed to prove itself, and it always carries a `blocked_reason` a
    builder can read.
    """

    queued = "queued"
    building = "building"
    awaiting_answers = "awaiting_answers"
    awaiting_confirmation = "awaiting_confirmation"
    verifying = "verifying"
    blocked = "blocked"
    deploying = "deploying"
    live = "live"
    superseded = "superseded"
    error = "error"


TERMINAL_DEPLOYMENT_STATUSES = frozenset(
    {
        DeploymentStatus.blocked,
        DeploymentStatus.live,
        DeploymentStatus.superseded,
        DeploymentStatus.error,
    }
)


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    waiting = "waiting"
    done = "done"
    failed = "failed"


class Protection(str, enum.Enum):
    """Readme.md section 4: a stack we cannot protect still deploys, but it is
    labelled, never silently treated as safe."""

    unknown = "unknown"
    protected = "protected"
    unprotected = "unprotected"


class BuildPhase(str, enum.Enum):
    """The build job runs twice for a deployment.

    `plan` reads the schema in a throwaway place and proposes an access model.
    `verify` provisions for real, seeds, applies the policies and attacks them.
    Splitting them is what makes "confirm before we build it" possible, and it
    is why a confirmed model is bound to the schema hash `plan` saw.
    """

    plan = "plan"
    verify = "verify"


class AgentStatus(str, enum.Enum):
    active = "active"
    disabled = "disabled"


class ActionStatus(str, enum.Enum):
    """Contract 7.6. The names are the contract's; the edges live in
    `gateway.actions.LEGAL`, which is the only place a status may change."""

    received = "received"
    denied = "denied"
    dry_run_done = "dry_run_done"
    pending_approval = "pending_approval"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"
    executing = "executing"
    verified = "verified"
    failed = "failed"
    rolled_back = "rolled_back"
    undo_failed = "undo_failed"
    undone = "undone"


def _enum_check(column: str, values: type[enum.Enum]) -> CheckConstraint:
    listed = ", ".join(f"'{member.value}'" for member in values)
    return CheckConstraint(f"{column} IN ({listed})", name=f"ck_{column}")


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class Builder(Base):
    """A person who deploys apps here."""

    __tablename__ = "builders"

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()


class App(Base):
    __tablename__ = "apps"
    __table_args__ = (
        _enum_check("protection", Protection),
        # `app_id` is not decoration: it becomes the Postgres schema name and
        # the stem of all four role names, so it is validated by
        # kernel.render.validate_app_id before it ever gets here.
        UniqueConstraint("app_id", name="uq_apps_app_id"),
        UniqueConstraint("subdomain", name="uq_apps_subdomain"),
    )

    id: Mapped[uuid.UUID] = _pk()
    builder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.builders.id", ondelete="CASCADE"), nullable=False
    )
    app_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    subdomain: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    # Which branch a push has to be on to deploy itself (section 15). One ref,
    # not "whatever was pushed": every topic branch redeploying production is
    # not a feature, and a deploy nobody asked for is the kind a person is not
    # watching.
    tracked_ref: Mapped[str] = mapped_column(
        Text, nullable=False, default="main", server_default="main"
    )
    installation_id: Mapped[str | None] = mapped_column(Text)
    stack: Mapped[dict | None] = mapped_column(JSONB)
    protection: Mapped[str] = mapped_column(
        Text, nullable=False, default=Protection.unknown.value
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="new")
    agents_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Section 10: "Answers are reused; only new or changed tables trigger new
    # questions." This is where they are reused from.
    answers: Mapped[dict | None] = mapped_column(JSONB)
    live_deployment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # The fence for everything outside this database. `jobs.fence` guards writes
    # to a job row; this guards *the route*. It goes up once per deployment
    # queued, the deployment carries the number it was queued under, and the one
    # write that grants traffic names it in its `WHERE`. A worker holding an old
    # number can still start a process — nothing can stop it — but it can never
    # make that process the one requests go to. `control_plane/generations.py`
    # makes the argument in full.
    desired_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = _created()


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (
        _enum_check("status", DeploymentStatus),
        Index("ix_deployments_app", "app_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    app_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.apps.id", ondelete="CASCADE"), nullable=False
    )
    # What was asked for: a branch, a tag or a commit. A branch moves, so this
    # is not what was built.
    ref: Mapped[str] = mapped_column(Text, nullable=False)
    # What was actually built, resolved by git at checkout time. Null until the
    # clone succeeds, and null forever for a fixture tree, which has no commit.
    # `verify` re-checks out this commit rather than the ref, so a push between
    # confirming the rules and building them cannot slip past.
    commit_sha: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default=DeploymentStatus.queued.value
    )
    image_digest: Mapped[str | None] = mapped_column(Text)
    schema_hash: Mapped[str | None] = mapped_column(Text)
    # The introspected graph the hash was taken over, written only once the
    # real schema has matched it. Section 20.1 builds the agent's table and
    # column allowlist from this: an agent may only name something that was
    # present in the schema this deployment actually proved.
    schema_graph: Mapped[dict | None] = mapped_column(JSONB)
    # Plain English, from kernel/messages.py or a derivation refusal. A blocked
    # deployment without one is a bug: the builder would have nothing to act on.
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    answers: Mapped[dict | None] = mapped_column(JSONB)
    questions: Mapped[list | None] = mapped_column(JSONB)
    explanation: Mapped[list | None] = mapped_column(JSONB)
    stack: Mapped[dict | None] = mapped_column(JSONB)
    endpoint: Mapped[str | None] = mapped_column(Text)
    # The app's `desired_generation` at the moment this deployment was queued.
    # Carried rather than re-read, because the whole point is to notice that it
    # has moved on: a worker re-reading it would always agree with itself.
    generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AccessModel(Base):
    """Contract 7.2, saved on every run. Readme.md section 3 rule 8."""

    __tablename__ = "access_models"
    __table_args__ = (
        UniqueConstraint("deployment_id", name="uq_access_models_deployment"),
    )

    id: Mapped[uuid.UUID] = _pk()
    app_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.apps.id", ondelete="CASCADE"), nullable=False
    )
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.deployments.id", ondelete="CASCADE"), nullable=False
    )
    model_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class VerificationRun(Base):
    """One pass of the attack suite, as one role.

    There are always two rows per verified deployment, `runtime` and `agent`,
    and section 13 requires them to agree. Storing them separately is what lets
    a builder see that they did.
    """

    __tablename__ = "verification_runs"
    __table_args__ = (
        CheckConstraint("role IN ('runtime', 'agent')", name="ck_verification_role"),
        UniqueConstraint("deployment_id", "role", name="uq_verification_run"),
    )

    id: Mapped[uuid.UUID] = _pk()
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.deployments.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    passed: Mapped[int] = mapped_column(Integer, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, nullable=False)
    report_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created()


class AppUser(Base):
    """A person who logs into a deployed app, through the gate.

    `principal_key` is the value the shim puts into `app.user_id`, so it is the
    only link between a browser session and a row in the app's own database.
    """

    __tablename__ = "app_users"
    __table_args__ = (
        UniqueConstraint("app_id", "email", name="uq_app_users_email"),
    )

    id: Mapped[uuid.UUID] = _pk()
    app_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.apps.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="member")
    principal_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Bumped when the person is removed or their access changes. The sidecar
    # caches it for 60s, so a bump logs them out within a minute.
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created()


class Job(Base):
    """The queue. Readme.md section 5: a table, claimed with SKIP LOCKED.

    `waiting` is not a stalled job. It is the pause the whole milestone is
    about: the pipeline stops mid-flight because it does not know something,
    and only a builder's answer moves it on. Nothing guesses on their behalf.

    SKIP LOCKED protects the *claim* and nothing after it: the row lock ends
    when the claiming transaction commits, which `worker/main.py` does before
    any work starts. What protects the rest of the run is the pair of columns
    at the bottom — a lease, and a fence. `control_plane/jobs.py` says why.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        _enum_check("status", JobStatus),
        Index("ix_jobs_claim", "status", "run_after", "lease_expires_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    app_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.apps.id", ondelete="CASCADE")
    )
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.deployments.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default=JobStatus.queued.value
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the holder's claim stops meaning anything. Renewed by a heartbeat
    # while the worker is alive; null whenever nobody holds the job, so a
    # `waiting` job can never expire its way back onto the queue.
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # The fencing token. Bumped by every claim and never reset, so the number a
    # worker was given at claim time is proof it is still the holder. Every
    # write a worker makes to its own job names it, which is how a worker that
    # was declared dead and isn't finds out: its UPDATE matches no rows.
    fence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WebhookDelivery(Base):
    """One delivery from GitHub, and what it turned out to mean.

    The unique index is not bookkeeping, it is the mechanism: GitHub retries,
    and the INSERT of this row is what makes the second attempt a no-op. A
    handler that looked for an existing row and then inserted one would lose a
    race with its own retry, and the losing case is two builds of one commit.

    Rows are kept for deliveries that did nothing as well, because "why did my
    push not deploy?" needs an answer, and `outcome` plus `detail` are it.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("source", "delivery_id", name="uq_webhook_deliveries_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    source: Mapped[str] = mapped_column(Text, nullable=False, default="github")
    delivery_id: Mapped[str] = mapped_column(Text, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    ref: Mapped[str | None] = mapped_column(Text)
    head_sha: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    # Which deployments this delivery started, if any. A list because one repo
    # can back more than one app, and then one push really is two deployments.
    deployments: Mapped[list | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()


class BuildToken(Base):
    """Contract 7.4: "Authenticated with a one-time job token."

    Only the hash is stored, and `used_at` is set in the same statement that
    accepts a result, so a replayed callback cannot overwrite a verdict that
    has already been recorded.
    """

    __tablename__ = "build_tokens"
    __table_args__ = (
        _enum_check("phase", BuildPhase),
        UniqueConstraint("token_hash", name="uq_build_tokens_hash"),
    )

    id: Mapped[uuid.UUID] = _pk()
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.deployments.id", ondelete="CASCADE"), nullable=False
    )
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------------------
# The agent gateway. Readme.md sections 19 and 22.
# ---------------------------------------------------------------------------


class Agent(Base):
    """An AI agent that works for exactly one person in exactly one app.

    `acts_for_user_id` is not a label. It is the identity the gateway sets on
    every database transaction it opens, so the same RLS policies that hold the
    person in also hold the agent in. There is no second security model here.
    """

    __tablename__ = "agents"
    __table_args__ = (
        _enum_check("status", AgentStatus),
        UniqueConstraint("app_id", "name", name="uq_agents_name"),
    )

    id: Mapped[uuid.UUID] = _pk()
    app_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.apps.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    acts_for_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.app_users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default=AgentStatus.active.value
    )
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()


class AgentKey(Base):
    """`vd_agent_...`, shown once, stored as an argon2 hash. Section 19.1."""

    __tablename__ = "agent_keys"

    id: Mapped[uuid.UUID] = _pk()
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.agents.id", ondelete="CASCADE"), nullable=False
    )
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentSession(Base):
    """Bound to one agent, one app and one acts-for person, for 30 minutes.

    Expiry is stored rather than computed so that a session can also be ended
    early: revoking an agent kills its sessions in the same statement that
    deletes its key.
    """

    __tablename__ = "agent_sessions"

    id: Mapped[uuid.UUID] = _pk()
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.agents.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created()


class AgentPolicy(Base):
    """Contract 7.5, one row per version. Versions are never edited.

    An action records the version that decided it, so "why was this allowed?"
    has an answer that cannot be rewritten afterwards.
    """

    __tablename__ = "agent_policies"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_policies_version"),
    )

    id: Mapped[uuid.UUID] = _pk()
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.agents.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    # Section 19.3: a policy may not mark a high-risk action `auto` unless a
    # person ticked a box saying they understood. The tick is kept with the
    # version it applies to, not with the agent.
    high_risk_ack: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created()


class Action(Base):
    """Contract 7.6, one row per thing an agent asked to do.

    Written before anything happens and updated as the pipeline moves, so an
    action that crashed halfway is still a row somebody can read. The full diff
    is kept here for 30 days; the audit log keeps only its hash, and keeps it
    for ever.
    """

    __tablename__ = "actions"
    __table_args__ = (
        _enum_check("status", ActionStatus),
        Index("ix_actions_app", "app_id", "created_at"),
        Index("ix_actions_agent", "agent_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    app_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.apps.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.agents.id", ondelete="CASCADE"), nullable=False
    )
    acts_for: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connector: Mapped[str] = mapped_column(Text, nullable=False)
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    args: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    policy_version: Mapped[int | None] = mapped_column(Integer)
    decision: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    risk: Mapped[str | None] = mapped_column(Text)
    dry_run: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default=ActionStatus.received.value
    )
    approved_by: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    result: Mapped[dict | None] = mapped_column(JSONB)
    undo: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditLog(Base):
    """Section 19.6. Append only, and hash-chained so that it shows if it isn't.

    `hash = sha256(prev_hash || canonical_json(row without hash))`. The gateway
    only ever INSERTs and SELECTs here; the migration also puts a trigger on the
    table that refuses UPDATE and DELETE outright, so an editable audit log is
    not one privilege mistake away.
    """

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_action", "action_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    action_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    event: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    prev_hash: Mapped[str] = mapped_column(Text, nullable=False)
    hash: Mapped[str] = mapped_column(Text, nullable=False)


class GlobalSettings(Base):
    """One row. The switch that stops every agent everywhere at once."""

    __tablename__ = "global_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_global_settings_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    agent_kill_switch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
