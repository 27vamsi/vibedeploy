"""Applying a proven access model to the real database. Readme.md 18.2.

The build job proved something in a database it was free to destroy. This is
where that proof is cashed in against the database an app actually lives in, and
the order is the whole argument:

    2. create the schema and the four roles, if the app is new
    3. run the app's migrations in a one-off task, never in this process
    5. introspect and hash; a mismatch blocks before anything is granted
    4. RLS, FORCE, policies, indexes, grants last
    6. structural checks on the real schema

Steps 5 and 4 are swapped from the Readme's numbering on purpose. The hash is of
the schema the app's migrations produced, so it can only be taken before the
kernel writes to it, and checking it first means a schema that changed under us
is refused while the runtime role still cannot read a single table.
"""

from __future__ import annotations

import dataclasses
import json
import secrets as _secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import asyncpg

from buildjob.attack import structural
from buildjob.introspect import introspect, schema_hash
from buildjob.policies import apply_model
from control_plane import secrets as secret_store
from control_plane.config import ControlPlaneConfig
from control_plane.secrets import SecretStore
from kernel.dsn import dsn_as
from kernel.provision import (
    AppRoles,
    create_app_roles,
    create_helpers,
    reassign_objects_to_owner,
)
from worker.pipeline.build import child_env, launch

SCHEMA_CHANGED = (
    "The database schema in production is not the one the rules were proved"
    " against, so nothing was changed. Deploy again from a clean checkout."
)
MIGRATION_TASK_FAILED = (
    "The app's migrations could not be applied to the live database, so the"
    " previous version is still running."
)


class DeployBlocked(RuntimeError):
    """Stop, and say why in one sentence a builder can act on."""


@dataclass(frozen=True)
class Provisioned:
    roles: AppRoles
    fresh: bool


@dataclass(frozen=True)
class Enforced:
    """What the real schema turned out to be, and what is wrong with it.

    `failures` empty is the only thing that may be followed by a deploy. The
    graph comes back too because this is the one moment it is known to describe
    the schema the rules were proved against, and section 20.1 builds the
    agent's allowlist out of it.
    """

    failures: list[str]
    graph: dict[str, Any]


# ---------------------------------------------------------------------------
# Step 2: the schema and the four roles
# ---------------------------------------------------------------------------


def _store(config: ControlPlaneConfig) -> SecretStore:
    return SecretStore(config.state_root / "secrets")


def _role_secret(dsn: str, user: str, password: str) -> dict[str, Any]:
    return {"user": user, "password": password, "dsn": dsn_as(dsn, user, password)}


async def ensure_roles(
    admin: asyncpg.Connection, config: ControlPlaneConfig, app_id: str
) -> Provisioned:
    """The four roles of section 9.1, created once and reused after that.

    Their passwords exist in exactly one place, the secret store, and are read
    back from it on every later deploy. Regenerating them each time would be
    tidier to write and would silently break the app that is still running on
    the old ones.
    """
    store = _store(config)
    name = secret_store.secret_name(app_id, secret_store.MIGRATOR)
    try:
        existing = store.get(name)
    except secret_store.SecretNotFound:
        pass
    else:
        stored = store.get(secret_store.secret_name(app_id, secret_store.RUNTIME))
        agent = store.get(secret_store.secret_name(app_id, secret_store.AGENT))
        roles = dataclasses.replace(
            AppRoles.generate(app_id),
            migrator_password=existing["password"],
            runtime_password=stored["password"],
            agent_password=agent["password"],
        )
        return Provisioned(roles=roles, fresh=False)

    roles = AppRoles.generate(app_id)
    await create_app_roles(admin, roles)

    dsn = config.app_admin_dsn
    store.put(
        secret_store.secret_name(app_id, secret_store.MIGRATOR),
        _role_secret(dsn, roles.migrator, roles.migrator_password),
    )
    store.put(
        secret_store.secret_name(app_id, secret_store.RUNTIME),
        _role_secret(dsn, roles.runtime, roles.runtime_password),
    )
    store.put(
        secret_store.secret_name(app_id, secret_store.AGENT),
        _role_secret(dsn, roles.agent, roles.agent_password),
    )
    # Section 16. The identity key is shared with the runtime secret at read
    # time, not copied: the sidecar signs with it and the shim verifies with it,
    # and there must be exactly one of it.
    store.put(
        secret_store.secret_name(app_id, secret_store.SIDECAR),
        {"identity_key": _secrets.token_hex(32), "session_key": _secrets.token_hex(32)},
    )
    return Provisioned(roles=roles, fresh=True)


# ---------------------------------------------------------------------------
# Step 3: the app's own migrations, in their own process
# ---------------------------------------------------------------------------


async def run_migration_task(
    config: ControlPlaneConfig,
    roles: AppRoles,
    *,
    checkout: Path,
    migrations_kind: str,
    migrations_path: str,
    workdir: Path,
) -> None:
    result_path = workdir / "migrate-result.json"
    outcome = await launch(
        [sys.executable, "-m", "buildjob.migrate_task"],
        env=child_env(
            # In the environment, never in argv: argv is world-readable.
            VD_MIGRATOR_DSN=dsn_as(
                config.app_admin_dsn, roles.migrator, roles.migrator_password
            ),
            VD_REPO_DIR=str(checkout),
            VD_MIGRATIONS_KIND=migrations_kind,
            VD_MIGRATIONS_PATH=migrations_path,
            VD_RESULT_PATH=str(result_path),
        ),
        log=workdir / "migrate.log",
    )
    if outcome.ok:
        return

    try:
        reason = json.loads(result_path.read_text(encoding="utf-8"))["reason"]
    except (OSError, ValueError, KeyError):
        reason = MIGRATION_TASK_FAILED
    raise DeployBlocked(reason)


# ---------------------------------------------------------------------------
# Steps 5, 4 and 6
# ---------------------------------------------------------------------------


async def enforce(
    admin: asyncpg.Connection,
    roles: AppRoles,
    model: Mapping[str, Any],
    *,
    fresh: bool,
) -> Enforced:
    """Hash, then protect, then check what we actually built."""
    await reassign_objects_to_owner(admin, roles)

    graph = await introspect(admin, roles.schema)
    if schema_hash(graph) != model.get("schema_hash"):
        raise DeployBlocked(SCHEMA_CHANGED)

    if fresh:
        # `vd_user_id()` and `vd_role()` are created once per schema and the
        # policies are written against them. A later deploy finds them already
        # there; a key type that changed would change the schema hash first and
        # never reach here.
        await create_helpers(admin, roles, key_type=model["principal"]["key_type"])

    await apply_model(admin, roles, model)

    checks = await structural(admin, roles, model)
    return Enforced(
        failures=[check.message or check.name for check in checks if not check.ok],
        graph=graph,
    )
