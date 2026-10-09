"""One connection string, as somebody else.

The platform is handed a single admin URL per database and everything else is
derived from it, because the four role passwords are generated at provision time
and kept in the secret store, never written into a config file. Both the worker
and the build job need this and neither should import the other, so it lives
here with the rest of the shared plumbing.
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit


def dsn_as(base: str, user: str, password: str) -> str:
    """The same database as `base`, connecting as `user`.

    The credentials are percent-encoded rather than interpolated, so a generated
    password containing `@`, `/` or `:` cannot change which host is connected
    to.
    """
    parts = urlsplit(base)
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc))
