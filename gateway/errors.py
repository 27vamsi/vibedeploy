"""The one way this package says no.

Readme.md 19.2: "Any exception at any step = deny or stop, recorded. Fail
closed." So every refusal is the same type, carries a short machine-readable
`code` for the evidence report (19.7 breaks denials down by reason) and a
sentence a person can read.

The reason is deliberately not shaped by what the agent asked for. An agent that
learns "that table exists but you may not read it" has learned something; the
allowlist answers "no such table" to both cases.
"""

from __future__ import annotations


class Denied(Exception):
    """This call will not happen, and this is why."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


class StatusError(RuntimeError):
    """An action was asked to move somewhere contract 7.6 does not allow.

    Never shown to an agent. It means our own code tried an illegal transition,
    which is a bug in us, not an attack.
    """
