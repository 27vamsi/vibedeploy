"""The build job. Readme.md sections 8 M3-M5 and 18.2 step 1.

This is the only process that runs an app's own migrations, so it is the least
trusted thing we operate and it is deliberately walled off:

  - it is started as a separate process and never imported by the worker;
  - everything it does happens in a **throwaway** database it is free to
    destroy, never in the customer database an app will actually live in;
  - it holds no control plane credentials. Its single way to report anything is
    one POST with a one-time token (contract 7.4).

It runs twice per deployment.

`plan` migrates the throwaway schema, reads it back, and derives an access
model. It answers with either the questions a builder still has to settle or a
model to confirm. It decides nothing on their behalf.

`verify` does it all again from scratch, checks the schema still hashes to what
was confirmed, seeds known data, applies the policies and attacks them as the
runtime role and as the agent role. Its verdict is what blocks or clears the
deploy.

Nothing here is allowed to be clever. Any exception at all is reported as
`error`, which blocks, because a build job that fell over proved nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import asyncpg
import httpx

from buildjob.attack import Check, Report
from buildjob.attack import run as run_attack
from buildjob.derive import Answers, derive
from buildjob.introspect import introspect, schema_hash
from buildjob.migrate_scratch import MigrationError, apply_sql_migrations
from buildjob.policies import apply_model
from buildjob.seed import seed
from kernel.dsn import dsn_as
from kernel.provision import (
    AppRoles,
    create_app_roles,
    create_helpers,
    drop_app_roles,
    reassign_objects_to_owner,
)

TOKEN_HEADER = "X-VD-Build-Token"
CALLBACK_TIMEOUT = httpx.Timeout(30.0)

# Kept in the same words the dashboard shows, so a builder reads one sentence
# and not a traceback. Anything that can fail without a check name of its own
# gets one here.
SCHEMA_CHANGED = (
    "The database schema changed after the rules were confirmed, so the rules"
    " were never checked against this code. Confirm the rules again."
)
NO_ANSWERS = (
    "The questions about who may see what were never answered, so there is no"
    " access model to enforce."
)
UNSUPPORTED_MIGRATIONS = (
    "Only a folder of plain `.sql` migrations can be run by this build job."
    " Alembic support arrives with the containerised build."
)


class BuildFailure(RuntimeError):
    """Something that blocks, in words a builder can act on."""


@dataclass(frozen=True)
class BuildSpec:
    """Everything the build job is told. It looks nothing else up."""

    phase: str
    deployment_id: str
    app_id: str
    repo_dir: Path
    migrations_kind: str
    migrations_path: str
    # Admin of the throwaway database. Never the customer database's admin:
    # section 18.2 keeps that with the worker.
    scratch_dsn: str
    callback_url: str
    job_token: str
    answers: dict[str, Any] | None = None
    access_model: dict[str, Any] | None = None
    stack: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "BuildSpec":
        migrations = raw["migrations"]
        return cls(
            phase=raw["phase"],
            deployment_id=raw["deployment_id"],
            app_id=raw["app_id"],
            repo_dir=Path(raw["repo_dir"]),
            migrations_kind=migrations["kind"],
            migrations_path=migrations["path"],
            scratch_dsn=raw["scratch_dsn"],
            callback_url=raw["callback_url"],
            job_token=raw["job_token"],
            answers=raw.get("answers"),
            access_model=raw.get("access_model"),
            stack=raw.get("stack"),
        )


@asynccontextmanager
async def scratch_app(spec: BuildSpec):
    """A complete four-role app in a database we are about to throw away.

    The schema name is random and unrelated to the real one on purpose: the
    schema hash must not depend on where the schema happened to be built, or a
    hash confirmed in `plan` could never match the one `verify` computes.
    """
    admin = await asyncpg.connect(spec.scratch_dsn)
    roles = AppRoles.generate(f"app_b{secrets.token_hex(4)}")
    await drop_app_roles(admin, roles)
    await create_app_roles(admin, roles)
    try:
        yield admin, roles
    finally:
        try:
            await drop_app_roles(admin, roles)
        finally:
            await admin.close()


async def migrate(spec: BuildSpec, roles: AppRoles) -> None:
    """Run the app's own migrations, as the migrator, in the throwaway schema.

    This is the untrusted step. It is why this file is a separate process.
    """
    if spec.migrations_kind != "sql":
        raise BuildFailure(UNSUPPORTED_MIGRATIONS)

    source = spec.repo_dir / spec.migrations_path
    conn = await asyncpg.connect(
        dsn_as(spec.scratch_dsn, roles.migrator, roles.migrator_password)
    )
    try:
        await apply_sql_migrations(conn, source)
    except MigrationError as exc:
        raise BuildFailure(f"The app's migrations did not run: {exc}") from exc
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Phase 1: read the schema, propose an access model
# ---------------------------------------------------------------------------


async def plan(spec: BuildSpec) -> dict[str, Any]:
    if not spec.answers:
        raise BuildFailure(NO_ANSWERS)

    async with scratch_app(spec) as (admin, roles):
        await migrate(spec, roles)
        graph = await introspect(admin, roles.schema)
        digest = schema_hash(graph)

        follow_ups = {
            key: value
            for key, value in spec.answers.items()
            if key not in ("audience", "size", "visibility", "sensitive")
        }
        derivation = derive(
            graph,
            Answers(
                audience=spec.answers["audience"],
                size=spec.answers["size"],
                visibility=spec.answers["visibility"],
                sensitive=bool(spec.answers["sensitive"]),
                follow_ups=follow_ups,
            ),
            app_id=spec.app_id,
            schema_hash=digest,
        )

    result: dict[str, Any] = {
        "deployment_id": spec.deployment_id,
        "phase": "plan",
        "schema_hash": digest,
        "questions": [
            {
                "id": q.id,
                "kind": q.kind,
                "table": q.table,
                "text": q.text,
                "options": list(q.options),
            }
            for q in derivation.questions
        ],
        "refusals": [
            {"table": r.table, "reason": r.reason} for r in derivation.refusals
        ],
        "explanation": list(derivation.explanation),
        "access_model": derivation.model,
    }

    # A refusal outranks a question. There is no answer that makes a refused
    # schema deployable, so asking would be pretending otherwise.
    if derivation.refusals:
        result["status"] = "blocked"
        result["blocked_reason"] = " ".join(r.reason for r in derivation.refusals)
    elif derivation.questions:
        result["status"] = "needs_answers"
    else:
        result["status"] = "derived"
    return result


# ---------------------------------------------------------------------------
# Phase 2: build it for real and attack it
# ---------------------------------------------------------------------------


def _for_role(report: Report, label: str, role_name: str) -> dict[str, Any]:
    """One role's view of the run.

    A check belongs to a role if it names it, either by the label the
    behavioural runs use or by the actual Postgres role name the structural
    checks use. Checks that name no role belong to both, because both runs
    depend on them being true.
    """
    mine = [c for c in report.checks if c.role in (None, label, role_name)]
    return Report(checks=mine).to_contract()


def _blocked_reason(report: Report) -> str:
    """The first failure, and how many others there were.

    One sentence, because a builder needs somewhere to start, not a wall. The
    rest are in the report.
    """
    failures: list[Check] = report.failures
    first = failures[0].message or failures[0].name
    if len(failures) == 1:
        return first
    return f"{first} ({len(failures) - 1} more problem(s) in the report.)"


async def verify(spec: BuildSpec) -> dict[str, Any]:
    model = spec.access_model
    if not model:
        raise BuildFailure(NO_ANSWERS)

    async with scratch_app(spec) as (admin, roles):
        await migrate(spec, roles)
        graph = await introspect(admin, roles.schema)
        digest = schema_hash(graph)
        if digest != model.get("schema_hash"):
            raise BuildFailure(SCHEMA_CHANGED)

        await create_helpers(admin, roles, key_type=model["principal"]["key_type"])
        await reassign_objects_to_owner(admin, roles)

        # Section 12: fake data goes in as the migrator, before any policy
        # exists. The plan it returns is the answer key the attack grades
        # against; nothing reads the policies back to decide what to expect.
        migrator = await asyncpg.connect(
            dsn_as(spec.scratch_dsn, roles.migrator, roles.migrator_password)
        )
        try:
            plan_ = await seed(migrator, roles, graph, model)
        finally:
            await migrator.close()

        await apply_model(admin, roles, model)

        runtime = await asyncpg.connect(
            dsn_as(spec.scratch_dsn, roles.runtime, roles.runtime_password)
        )
        agent = await asyncpg.connect(
            dsn_as(spec.scratch_dsn, roles.agent, roles.agent_password)
        )
        try:
            report = await run_attack(
                admin=admin,
                runtime=runtime,
                agent=agent,
                roles=roles,
                graph=graph,
                model=model,
                plan=plan_,
            )
        finally:
            await runtime.close()
            await agent.close()

    return {
        "deployment_id": spec.deployment_id,
        "phase": "verify",
        "status": "passed" if report.ok else "blocked",
        "blocked_reason": None if report.ok else _blocked_reason(report),
        "schema_hash": digest,
        "access_model": model,
        "verification": report.to_contract(),
        "runs": {
            "runtime": _for_role(report, "runtime", roles.runtime),
            "agent": _for_role(report, "agent", roles.agent),
        },
        # No image is built locally; M8 fills this in from the buildpack.
        "image_digest": None,
        "shim": spec.stack,
    }


# ---------------------------------------------------------------------------
# Reporting home
# ---------------------------------------------------------------------------


async def report_result(spec: BuildSpec, result: Mapping[str, Any]) -> None:
    async with httpx.AsyncClient(timeout=CALLBACK_TIMEOUT) as client:
        response = await client.post(
            spec.callback_url,
            json=dict(result),
            headers={TOKEN_HEADER: spec.job_token},
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"the control plane refused the build result: {response.status_code}"
            f" {response.text[:400]}"
        )


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spec",
        type=Path,
        help="JSON spec file. Omit to read it from stdin.",
    )
    args = parser.parse_args(argv)

    raw = json.loads(
        args.spec.read_text(encoding="utf-8") if args.spec else sys.stdin.read()
    )
    spec = BuildSpec.from_json(raw)

    try:
        result = await (plan(spec) if spec.phase == "plan" else verify(spec))
    except BuildFailure as exc:
        result = {
            "deployment_id": spec.deployment_id,
            "phase": spec.phase,
            "status": "blocked",
            "blocked_reason": str(exc),
        }
    except Exception:  # noqa: BLE001 - a crashed build proved nothing
        traceback.print_exc()
        result = {
            "deployment_id": spec.deployment_id,
            "phase": spec.phase,
            "status": "error",
            "blocked_reason": (
                "The build job stopped before it could prove anything, so"
                " nothing was deployed."
            ),
        }

    await report_result(spec, result)
    print(json.dumps({"status": result["status"], "phase": spec.phase}), file=sys.stderr)
    return 0 if result["status"] in ("passed", "derived", "needs_answers") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
