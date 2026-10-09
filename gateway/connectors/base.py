"""Contract 7.7. Frozen; do not change without asking.

The one thing worth saying about this interface is why `dry_run` and `execute`
are separate calls rather than one call with a flag. A flag would mean the same
code path decides, at the last moment, whether to commit — and an approval given
for what the dry run showed would be approval for whatever the second run
happens to find. Keeping them apart lets `execute` be handed the dry run it is
allowed to perform and refuse anything else (19.4).

`risk` takes only the tool and its arguments because that is all a connector
knows before the dry run. The row count changes the answer for `db_update`, so
the pipeline scores again once the dry run has been done. A connector never
lowers a risk, only names the one it can see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from control_plane.models import Action
from gateway.policy import AgentPolicy


@dataclass(frozen=True)
class ToolSpec:
    """One thing an agent may call. Listed only if the policy does not forbid it."""

    name: str
    connector: str
    description: str
    input_schema: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connector": self.connector,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True)
class DryRun:
    """What would happen, proved by doing it and rolling back.

    `diff_hash` is what an approval is bound to. `before` and `after` are what
    `verify` and `undo` compare against, so they are full rows; the audit log
    only ever gets the hash and the key.
    """

    affected: int
    diff: list[dict[str, Any]] = field(default_factory=list)
    diff_hash: str = ""
    before: list[dict[str, Any]] = field(default_factory=list)
    after: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "affected": self.affected,
            "diff": self.diff,
            "diff_hash": self.diff_hash,
            "before": self.before,
            "after": self.after,
            "note": self.note,
        }


@dataclass(frozen=True)
class Result:
    ok: bool
    affected: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "affected": self.affected,
            "rows": self.rows,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class UndoResult:
    ok: bool
    reason: str | None = None
    possible: bool = True

    @classmethod
    def impossible(cls, reason: str) -> "UndoResult":
        return cls(ok=False, reason=reason, possible=False)

    def as_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "possible": self.possible}


class Connector(Protocol):
    name: str

    def tools(self, policy: AgentPolicy) -> list[ToolSpec]: ...

    def risk(self, tool: str, args: dict[str, Any]) -> str: ...

    async def scoped_credentials(self, session: Any) -> Any: ...

    async def dry_run(self, creds: Any, tool: str, args: dict[str, Any]) -> DryRun: ...

    async def execute(
        self, creds: Any, tool: str, args: dict[str, Any], expected: DryRun
    ) -> Result: ...

    async def verify(
        self, creds: Any, tool: str, args: dict[str, Any], result: Result
    ) -> bool: ...

    async def undo(self, creds: Any, action: Action) -> UndoResult: ...
