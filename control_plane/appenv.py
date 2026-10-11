"""The settings a builder's app needs, and the ones they may not set.

Readme.md section 18.1: the runtime secret holds the runtime `DATABASE_URL`, the
identity key, and "builder env vars". This module is the last of those.

It exists because a `.env` next to somebody's code is never copied into a
deployment (section 15). An app that loads its own `.env` and builds its own
engine out of its own `DATABASE_URL` reaches a database with no identity set on
it and no policies on it, which is outside everything this product proves. But
refusing to carry the file left a real gap: an app with a Stripe key had nowhere
to put it. So the platform carries the variables instead, under rules.

The interesting half is what it refuses:

  - `DATABASE_URL` and `VD_*` decide what database the app talks to and who it
    thinks the request is for;
  - `PYTHON*` decides which code runs, including whether the shim's
    `sitecustomize` is found at all;
  - `PATH`, `LD_*` and `DYLD_*` put code or libraries into the process before
    any of the app runs;
  - `DOCKER*` names the daemon the worker's own `docker` client talks to.

Those are the platform's, not the builder's. `validate` refuses them with the
name in the message, so a form can say why. `safe` drops them without a word, and
is what the runtime uses: a name that should never have been stored still cannot
take effect.
"""

from __future__ import annotations

import re
from typing import Mapping

MAX_COUNT = 64
MAX_NAME = 128
MAX_VALUE = 8192

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Exact names the platform owns.
RESERVED = frozenset({"DATABASE_URL", "PATH"})

# Prefixes the platform owns. `VD_` is ours; the rest change what code runs.
#
# `DOCKER` is here for a sharper reason than the others, and it is the one that
# would be easiest to leave out. The worker starts an app by running `docker`,
# and `docker` is a client that is told which daemon to talk to by `DOCKER_HOST`
# — so a builder who could set that would be choosing the daemon their own
# deployment is created on, with the worker's access to it. That turns a setting
# into a privilege escalation, and it is created by containerising rather than
# found: with bare processes this name meant nothing at all.
RESERVED_PREFIXES = ("VD_", "PYTHON", "LD_", "DYLD_", "DOCKER")


class InvalidEnv(ValueError):
    """A setting was refused, in words a builder can act on."""


def _reserved(name: str) -> bool:
    return name in RESERVED or name.startswith(RESERVED_PREFIXES)


def _printable(value: str) -> bool:
    return not any(character < " " or character == "\x7f" for character in value)


def validate(env: Mapping[str, str]) -> dict[str, str]:
    """Return the settings as they will be stored, or say which one is wrong."""
    if len(env) > MAX_COUNT:
        raise InvalidEnv(f"That is more than {MAX_COUNT} settings.")

    checked: dict[str, str] = {}
    for name, value in env.items():
        if not NAME.match(name) or len(name) > MAX_NAME:
            raise InvalidEnv(
                f"`{name}` is not a usable name. A name starts with a letter or"
                " an underscore and contains only letters, digits and"
                " underscores."
            )
        if _reserved(name):
            raise InvalidEnv(
                f"`{name}` is set by us, not by you. The database your app talks"
                " to, who it thinks the request is for, and which code runs are"
                " all part of what we prove, so they are not yours to change."
            )
        value = str(value)
        if len(value) > MAX_VALUE:
            raise InvalidEnv(f"The value of `{name}` is longer than {MAX_VALUE} characters.")
        if not _printable(value):
            raise InvalidEnv(f"The value of `{name}` contains control characters.")
        checked[name] = value
    return checked


def safe(env: Mapping[str, str] | None) -> dict[str, str]:
    """The same rules, applied at injection time, dropping instead of raising.

    Belt and braces. `validate` guards the way in; this guards the way out, so
    a stored name that should never have been accepted still does nothing.
    """
    if not env:
        return {}
    return {
        name: str(value)
        for name, value in env.items()
        if NAME.match(name)
        and len(name) <= MAX_NAME
        and not _reserved(name)
        and len(str(value)) <= MAX_VALUE
        and _printable(str(value))
    }


def parse(text: str) -> dict[str, str]:
    """Read the `.env`-shaped paste a builder will reach for.

    Not a shell: no interpolation, no multi-line values, no escapes. Quotes are
    stripped because people type them, and `export ` is allowed because people
    copy it.
    """
    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise InvalidEnv(f"`{line}` is not a `NAME=value` line.")
        name, _, value = line.partition("=")
        name = name.strip()
        if name.startswith("export "):
            name = name[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[name] = value
    return env
