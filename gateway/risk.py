"""Readme.md 19.3, and nothing else.

A fixed table, on purpose. Risk is shown to people and counted in the evidence
report, so it has to mean the same thing every time it is used; a model that
scored it would make "medium" a mood. Nothing here is learned, configured or
overridable.

Risk does not decide allow or deny — the policy does that. Risk decides whether
a policy is allowed to have said `auto` without anybody ticking a box.
"""

from __future__ import annotations

LOW = "low"
MEDIUM = "medium"
HIGH = "high"

# "update > 5 rows" is high; five or fewer is medium.
UPDATE_HIGH_ABOVE = 5

_FIXED = {
    "db_query": LOW,
    "db_delete": HIGH,
    "aws_app_status": LOW,
    "aws_read_logs": LOW,
    "aws_restart_app": MEDIUM,
    "aws_rollback_deploy": HIGH,
}


def score(tool: str, affected: int | None = None) -> str:
    """The risk of doing `tool` to `affected` rows.

    `affected` comes from the dry run, so a call is scored against what would
    actually happen rather than what was asked for. Unknown tools score high:
    the only way to reach this with one is a bug in us, and a bug must not
    quietly come out `low`.
    """
    fixed = _FIXED.get(tool)
    if fixed is not None:
        return fixed
    if tool == "db_create":
        return MEDIUM
    if tool == "db_update":
        return HIGH if (affected or 0) > UPDATE_HIGH_ABOVE else MEDIUM
    return HIGH
