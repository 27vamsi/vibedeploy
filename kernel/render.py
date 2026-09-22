"""Safe SQL text assembly for the kernel.

Roles, schemas, policies and grants cannot be written with bind parameters, so
every identifier that reaches these templates goes through quote_ident first
(Readme.md section 9, rule: never plain string formatting).
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

SQL_DIR = Path(__file__).parent / "sql"

# Principal key types we know how to cast to in vd_user_id().
ALLOWED_KEY_TYPES = frozenset({"uuid", "bigint", "integer", "text"})

_APP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_TEMPLATE_NAME_RE = re.compile(r"^[a-z0-9_]+(/[a-z0-9_]+)*\.sql$")
_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def validate_app_id(app_id: str) -> str:
    """App ids become role and schema names, so keep them boring."""
    if not _APP_ID_RE.match(app_id or ""):
        raise ValueError(f"invalid app id: {app_id!r}")
    return app_id


def quote_ident(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("identifier must be a non-empty string")
    if "\x00" in name:
        raise ValueError("identifier contains a NUL byte")
    if len(name.encode("utf-8")) > 63:
        raise ValueError(f"identifier longer than 63 bytes: {name!r}")
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("literal must be a string")
    if "\x00" in value:
        raise ValueError("literal contains a NUL byte")
    return "'" + value.replace("'", "''") + "'"


def key_type(name: str) -> str:
    if name not in ALLOWED_KEY_TYPES:
        raise ValueError(
            f"unsupported principal key type {name!r}; "
            f"supported: {sorted(ALLOWED_KEY_TYPES)}"
        )
    return name


def generate_password() -> str:
    """token_urlsafe only emits [A-Za-z0-9_-], so it survives quote_literal intact."""
    return secrets.token_urlsafe(32)


def render(template_name: str, **params: str) -> str:
    """Substitute {placeholders} in a kernel SQL file.

    Values must already be quoted - this function deliberately does no quoting
    of its own, so that an unquoted value is a visible mistake at the call site.
    """
    if not _TEMPLATE_NAME_RE.match(template_name):
        raise ValueError(f"invalid template name: {template_name!r}")

    sql = (SQL_DIR / template_name).read_text(encoding="utf-8")

    used: set[str] = set()

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            raise KeyError(f"{template_name} needs a value for {{{name}}}")
        used.add(name)
        return params[name]

    out = _PLACEHOLDER_RE.sub(substitute, sql)

    unused = set(params) - used
    if unused:
        raise ValueError(f"{template_name} does not use: {sorted(unused)}")
    return out
