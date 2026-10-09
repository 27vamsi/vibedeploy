"""Contract 7.5, parsed. Frozen shape; do not change without asking.

Three rules from the contract do all the work here:

  - **Anything not listed is `forbid`.** So the parser never invents a mode. A
    table the policy does not mention falls to `postgres.default`, and a
    `postgres` block that is missing altogether forbids every database tool.
  - **A policy can only narrow.** It cannot widen, because nothing it says is
    handed to the database. The database is connected to as the app's
    unprivileged `agent` role with the acts-for person's identity set, so a
    policy saying `read: auto` on a table still returns only that person's rows.
  - **`forbid` tools are not exposed.** `tools()` in the connector is given the
    policy and never lists them, so an agent is not told what it may not do.

One more, from 19.3: a policy may not mark a **high risk** action `auto` unless
a person ticked "I understand". Deleting is high risk whatever the row count, so
`delete: auto` without the tick is refused here, at load time, rather than at the
moment somebody's rows are about to go.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import yaml

MODES = ("auto", "approve", "forbid")
FORBID = "forbid"

# The four database operations of section 20.1 and the tool that performs each.
OPERATIONS = ("read", "create", "update", "delete")
TOOL_FOR_OPERATION = {
    "read": "db_query",
    "create": "db_create",
    "update": "db_update",
    "delete": "db_delete",
}
OPERATION_FOR_TOOL = {tool: op for op, tool in TOOL_FOR_OPERATION.items()}

# Section 20.2. Parsed now so a policy round-trips whole; the connector that
# acts on them arrives with M13.
AWS_ACTIONS = ("app_status", "read_logs", "restart_app", "rollback_deploy")
TOOL_FOR_AWS = {action: f"aws_{action}" for action in AWS_ACTIONS}

# Deleting anything, and rolling a deployment back, are high risk however few
# rows or tasks are involved (19.3). Marking either `auto` needs the tick.
ALWAYS_HIGH_RISK = ("delete",)
ALWAYS_HIGH_RISK_AWS = ("rollback_deploy",)

DEFAULT_SESSION_TTL_MINUTES = 30
DEFAULT_RATE_LIMIT_PER_MINUTE = 60
DEFAULT_MAX_ROWS_READ = 200
DEFAULT_MAX_ROWS_CHANGED = 50


class PolicyError(ValueError):
    """The policy does not parse, so the agent has no policy, so it is denied.

    Readme.md M10: "missing policy: denied". A policy we cannot read is a
    missing policy. It is never partially applied.
    """


@dataclass(frozen=True)
class TableRules:
    read: str = FORBID
    create: str = FORBID
    update: str = FORBID
    delete: str = FORBID

    def mode(self, operation: str) -> str:
        return getattr(self, operation, FORBID)


FORBID_ALL = TableRules()


@dataclass(frozen=True)
class Limits:
    max_rows_read: int = DEFAULT_MAX_ROWS_READ
    max_rows_changed: int = DEFAULT_MAX_ROWS_CHANGED


@dataclass(frozen=True)
class AwsRule:
    mode: str = FORBID
    max_per_hour: int | None = None


@dataclass(frozen=True)
class AgentPolicy:
    version: int
    agent: str
    acts_for: str
    session_ttl_minutes: int
    rate_limit_per_minute: int
    default: TableRules
    tables: Mapping[str, TableRules]
    limits: Limits
    aws: Mapping[str, AwsRule]
    high_risk_ack: bool

    def postgres_mode(self, tool: str, table: str) -> str:
        operation = OPERATION_FOR_TOOL.get(tool)
        if operation is None:
            return FORBID
        rules = self.tables.get(table, self.default)
        return rules.mode(operation)

    def aws_mode(self, tool: str) -> str:
        for action, name in TOOL_FOR_AWS.items():
            if name == tool:
                return self.aws.get(action, AwsRule()).mode
        return FORBID

    def mode_for(self, connector: str, tool: str, table: str | None) -> str:
        if connector == "postgres":
            return FORBID if table is None else self.postgres_mode(tool, table)
        if connector == "aws":
            return self.aws_mode(tool)
        return FORBID

    def allows(self, connector: str, tool: str, table: str | None) -> bool:
        return self.mode_for(connector, tool, table) != FORBID

    def tables_allowing(self, operation: str) -> list[str]:
        """Named tables whose rules permit `operation`. Used to word denials."""
        return sorted(
            name for name, rules in self.tables.items() if rules.mode(operation) != FORBID
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError(f"`{where}` must be a block of settings.")
    return value


def _mode(value: Any, where: str) -> str:
    if value not in MODES:
        raise PolicyError(
            f"`{where}` is `{value!r}`, which is not one of {', '.join(MODES)}."
        )
    return value


def _positive_int(value: Any, where: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PolicyError(f"`{where}` must be a whole number above zero.")
    return value


def _table_rules(value: Any, where: str) -> TableRules:
    block = _mapping(value, where)
    unknown = sorted(set(block) - set(OPERATIONS))
    if unknown:
        # A typo like `reads: auto` would otherwise be silently forbidden, and
        # the builder would spend an afternoon wondering why.
        raise PolicyError(
            f"`{where}` mentions {', '.join(unknown)}, which is not one of"
            f" {', '.join(OPERATIONS)}."
        )
    return TableRules(
        **{op: _mode(block.get(op, FORBID), f"{where}.{op}") for op in OPERATIONS}
    )


def _aws_rule(value: Any, where: str) -> AwsRule:
    if isinstance(value, str):
        return AwsRule(mode=_mode(value, where))
    block = _mapping(value, where)
    unknown = sorted(set(block) - {"mode", "max_per_hour"})
    if unknown:
        raise PolicyError(f"`{where}` mentions {', '.join(unknown)}, which means nothing.")
    max_per_hour = block.get("max_per_hour")
    if max_per_hour is not None:
        max_per_hour = _positive_int(max_per_hour, f"{where}.max_per_hour", 1)
    return AwsRule(mode=_mode(block.get("mode", FORBID), f"{where}.mode"), max_per_hour=max_per_hour)


def _check_high_risk(policy_tables, default, aws, high_risk_ack: bool) -> None:
    if high_risk_ack:
        return
    offenders = []
    for operation in ALWAYS_HIGH_RISK:
        if default.mode(operation) == "auto":
            offenders.append(f"postgres.default.{operation}")
        offenders.extend(
            f"postgres.tables.{name}.{operation}"
            for name, rules in sorted(policy_tables.items())
            if rules.mode(operation) == "auto"
        )
    offenders.extend(
        f"aws.{action}"
        for action in ALWAYS_HIGH_RISK_AWS
        if aws.get(action, AwsRule()).mode == "auto"
    )
    if offenders:
        raise PolicyError(
            "This policy lets the agent do something high risk without anyone"
            f" being asked: {', '.join(offenders)}. Either change it to"
            " `approve`, or tick the box saying you understand."
        )


def load(policy_yaml: str, *, version: int, high_risk_ack: bool = False) -> AgentPolicy:
    """Parse one stored version. Raises `PolicyError` rather than guessing."""
    try:
        raw = yaml.safe_load(policy_yaml)
    except yaml.YAMLError as exc:
        raise PolicyError(f"This policy is not valid YAML: {exc}") from exc

    document = _mapping(raw, "policy")
    if not document:
        raise PolicyError("This policy is empty, so the agent may do nothing.")

    agent = document.get("agent")
    acts_for = document.get("acts_for")
    if not isinstance(agent, str) or not agent.strip():
        raise PolicyError("`agent` is missing, so there is nothing to apply this to.")
    if not isinstance(acts_for, str) or not acts_for.strip():
        raise PolicyError(
            "`acts_for` is missing. An agent with nobody to act for has no"
            " access at all, so this is refused rather than applied."
        )

    postgres = _mapping(document.get("postgres"), "postgres")
    unknown = sorted(set(postgres) - {"default", "tables", "limits"})
    if unknown:
        raise PolicyError(f"`postgres` mentions {', '.join(unknown)}, which means nothing.")

    tables = {
        str(name): _table_rules(rules, f"postgres.tables.{name}")
        for name, rules in _mapping(postgres.get("tables"), "postgres.tables").items()
    }
    default = _table_rules(postgres.get("default"), "postgres.default")

    limit_block = _mapping(postgres.get("limits"), "postgres.limits")
    unknown = sorted(set(limit_block) - {"max_rows_read", "max_rows_changed"})
    if unknown:
        raise PolicyError(
            f"`postgres.limits` mentions {', '.join(unknown)}, which means nothing."
        )
    limits = Limits(
        max_rows_read=_positive_int(
            limit_block.get("max_rows_read"),
            "postgres.limits.max_rows_read",
            DEFAULT_MAX_ROWS_READ,
        ),
        max_rows_changed=_positive_int(
            limit_block.get("max_rows_changed"),
            "postgres.limits.max_rows_changed",
            DEFAULT_MAX_ROWS_CHANGED,
        ),
    )

    aws_block = _mapping(document.get("aws"), "aws")
    unknown = sorted(set(aws_block) - set(AWS_ACTIONS))
    if unknown:
        raise PolicyError(f"`aws` mentions {', '.join(unknown)}, which is not a tool.")
    aws = {
        action: _aws_rule(rule, f"aws.{action}") for action, rule in aws_block.items()
    }

    _check_high_risk(tables, default, aws, high_risk_ack)

    return AgentPolicy(
        version=version,
        agent=agent.strip(),
        acts_for=acts_for.strip().lower(),
        session_ttl_minutes=_positive_int(
            document.get("session_ttl_minutes"),
            "session_ttl_minutes",
            DEFAULT_SESSION_TTL_MINUTES,
        ),
        rate_limit_per_minute=_positive_int(
            document.get("rate_limit_per_minute"),
            "rate_limit_per_minute",
            DEFAULT_RATE_LIMIT_PER_MINUTE,
        ),
        default=default,
        tables=tables,
        limits=limits,
        aws=aws,
        high_risk_ack=high_risk_ack,
    )
